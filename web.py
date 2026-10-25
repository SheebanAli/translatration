import os, pathlib, ssl, time, json
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
load_dotenv()

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, render_template_string, request, jsonify

web = Flask(__name__)

ALLOWED_AUD = ["translation-service", "mobile-app"]

API_HOST    = os.getenv("BIND_HOST", "127.0.0.1")
if API_HOST == "0.0.0.0":
    API_HOST = "127.0.0.1"
API_PORT    = int(os.getenv("BIND_PORT", "8500"))
API_BASE    = os.getenv("API_BASE", f"https://{API_HOST}:{API_PORT}")
API_URL     = os.getenv("API_URL", f"{API_BASE}/translate")
HEALTH_URL  = os.getenv("HEALTH_URL", f"{API_BASE}/health")

CERTS_DIR   = pathlib.Path(__file__).resolve().parent / "certs"
CA_CERT     = CERTS_DIR / "ca.crt"
CLIENT_CERT = CERTS_DIR / "client.crt"
CLIENT_KEY  = CERTS_DIR / "client.key"


class MTLSAdapter(HTTPAdapter):
    """Custom HTTPAdapter for mTLS with local CA certificate ignoring hostname mismatch."""
    def __init__(self, ca_path=None, **kwargs):
        self.ca_path = ca_path
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.ca_path and os.path.exists(self.ca_path):
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_path))
            ctx.check_hostname = False
            kwargs["ssl_context"] = ctx
            kwargs["assert_hostname"] = False
        super().init_poolmanager(*args, **kwargs)


def get_session():
    s = requests.Session()
    ca = str(CA_CERT) if CA_CERT.is_file() else None
    s.mount("https://", MTLSAdapter(ca_path=ca))
    if CLIENT_CERT.is_file() and CLIENT_KEY.is_file():
        s.cert = (str(CLIENT_CERT), str(CLIENT_KEY))
    if ca:
        s.verify = ca
    else:
        s.verify = False
    return s


http_session = get_session()

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Vault JWT Translator</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f0f2f5; display: flex;
           justify-content: center; align-items: flex-start; min-height: 100vh; padding: 30px 10px; }
    .container { background: #fff; border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,0.1);
                 padding: 30px; width: 100%; max-width: 680px; }
    h1 { font-size: 1.4rem; color: #333; margin-bottom: 6px; }
    p.sub { font-size: 0.85rem; color: #888; margin-bottom: 24px; }
    label { display: block; font-size: 0.88rem; font-weight: 600; color: #444; margin-bottom: 5px; }
    select, input[type=text], input[type=number], textarea {
      width: 100%; padding: 9px 12px; border: 1px solid #ccc; border-radius: 6px;
      font-size: 0.95rem; outline: none; transition: border 0.2s;
    }
    select:focus, input:focus, textarea:focus { border-color: #4a90e2; }
    textarea { resize: vertical; min-height: 90px; }
    .field { margin-bottom: 18px; }
    .hint { font-size: 0.78rem; color: #999; margin-top: 4px; }
    button { width: 100%; padding: 11px; background: #4a90e2; color: #fff;
             border: none; border-radius: 6px; font-size: 1rem; cursor: pointer;
             transition: background 0.2s; }
    button:hover { background: #357abd; }
    button:disabled { background: #aaa; cursor: not-allowed; }

    /* live clock bar */
    .clock-bar { display: flex; gap: 14px; flex-wrap: wrap; align-items: center;
                 background: #1e1e1e; border-radius: 8px; padding: 10px 16px;
                 margin-bottom: 22px; font-family: "Courier New", monospace; font-size: 0.83rem; }
    .clk-label { color: #dcdcaa; font-weight: bold; white-space: nowrap; }
    .clk-val   { color: #4ec9b0; font-weight: bold; white-space: nowrap; }
    .clk-sep   { color: #444; }
    .clk-pulse { width: 8px; height: 8px; border-radius: 50%; background: #4ec9b0;
                 display: inline-block; animation: blink 1s step-start infinite; margin-right: 6px; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }

    /* backend status badge */
    .status-bar { display: flex; align-items: center; gap: 10px; font-size: 0.82rem;
                  background: #1e1e1e; border-radius: 8px; padding: 8px 16px;
                  margin-bottom: 18px; font-family: "Courier New", monospace; }
    .badge { padding: 2px 10px; border-radius: 12px; font-weight: bold; font-size: 0.8rem; }
    .badge-ok      { background: #1a3a1a; color: #4ec9b0; }
    .badge-loading { background: #3a3a1a; color: #dcdcaa; }
    .badge-err     { background: #3a1a1a; color: #f44747; }
    .badge-off     { background: #333;    color: #888; }

    .error-box { background: #fde8e8; color: #9b1c1c; border: 1px solid #f8b4b4;
                 border-radius: 6px; padding: 12px 14px; margin-top: 18px; font-size: 0.9rem;
                 word-break: break-word; }
  </style>
  <script>
    function pad(n){ return String(n).padStart(2,'0'); }

    var serverUnix = 0;
    var fetchedAt  = 0;

    function currentServerUnix() {
      if (serverUnix === 0) return 0;
      var elapsed = Math.floor((performance.now() - fetchedAt) / 1000);
      return serverUnix + elapsed;
    }

    function fetchServerTime() {
      fetch('/servertime')
        .then(function(r){ return r.json(); })
        .then(function(d){
          serverUnix = d.unix;
          fetchedAt  = performance.now();
          updateDisplay();
        })
        .catch(function(){});
    }

    function updateDisplay() {
      var unix = currentServerUnix();
      if (unix === 0) return;
      var dt    = new Date(unix * 1000);
      var human = dt.getFullYear() + '-' + pad(dt.getMonth()+1) + '-' + pad(dt.getDate()) +
                  '  ' + pad(dt.getHours()) + ':' + pad(dt.getMinutes()) + ':' + pad(dt.getSeconds());
      var hEl = document.getElementById('live-human');
      var uEl = document.getElementById('live-unix');
      if (hEl) hEl.textContent = human;
      if (uEl) uEl.textContent = unix;
      var iatEl = document.getElementById('iat');
      if (iatEl && iatEl.dataset.auto !== 'false') { iatEl.value = unix; }
    }

    // ── Health / backend status polling ──────────────────────────────
    function fetchHealth() {
      fetch('/proxyhealth')
        .then(function(r){ return r.json(); })
        .then(function(d){ renderStatus(d); })
        .catch(function(){ renderStatus(null); });
    }

    function renderStatus(d) {
      var el = document.getElementById('backend-status');
      if (!el) return;
      if (!d || d.error) {
        var msg = (d && d.error) ? d.error : 'OFFLINE';
        el.innerHTML = '<span class="clk-label">API</span> <span class="badge badge-err">' + msg + '</span>';
        return;
      }
      var backend = (d.backend || '?').toUpperCase();
      var ready   = d.translator_ready;
      var err     = d.translator_error;
      var badgeCls = err ? 'badge-err' : (ready ? 'badge-ok' : 'badge-loading');
      var badgeTxt = err ? 'ERROR'      : (ready ? 'READY'    : 'LOADING…');
      el.innerHTML =
        '<span class="clk-label">BACKEND</span> ' +
        '<span class="badge ' + badgeCls + '">' + backend + ' — ' + badgeTxt + '</span>' +
        (err ? ' <span style="color:#f44747;font-size:0.75rem">' + err + '</span>' : '');
    }

    document.addEventListener('DOMContentLoaded', function() {
      var iatEl = document.getElementById('iat');
      if (iatEl) {
        iatEl.dataset.auto = 'true';
        iatEl.addEventListener('input', function(){ this.dataset.auto = 'false'; });
      }
      fetchServerTime();
      setInterval(fetchServerTime, 1000);
      setInterval(updateDisplay, 250);

      fetchHealth();
      setInterval(fetchHealth, 4000);
    });

    function stampAndSubmit() {
      var iatEl = document.getElementById('iat');
      if (iatEl) { iatEl.value = currentServerUnix(); }
      var btn = document.getElementById('submit-btn');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Translating… (local model may take 1-2 min on first call)';
      }
      return true;
    }
  </script>
</head>
<body>
<div class="container">
  <h1>🔐 Vault JWT Translator</h1>
  <p class="sub">Translate Urdu addresses to Roman Urdu via Vault-issued JWT.</p>

  <!-- ── Live Clock Bar ── -->
  <div class="clock-bar">
    <span><span class="clk-pulse"></span><span class="clk-label">TIME</span></span>
    <span class="clk-val" id="live-human">—</span>
    <span class="clk-sep">|</span>
    <span class="clk-label">UNIX</span>
    <span class="clk-val" id="live-unix">—</span>
  </div>

  <!-- ── Backend Status Bar ── -->
  <div class="status-bar">
    <span id="backend-status"><span class="clk-label">BACKEND</span> <span class="badge badge-off">checking…</span></span>
  </div>

  <form method="POST" action="/" id="main-form" onsubmit="return stampAndSubmit();">

    <div class="field">
      <label for="aud">Audience (aud)</label>
      <select name="aud" id="aud">
        {% for a in auds %}
          <option value="{{ a }}" {{ 'selected' if a == form_aud else '' }}>{{ a }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="field">
      <label for="iat">IAT (Unix timestamp)</label>
      <input type="number" name="iat" id="iat"
             value="{{ form_iat }}" placeholder="e.g. {{ now }}" required />
      <div class="hint">Auto-updates from the live clock above. Edit manually to override. Must be within ±{{ skew }}s of server time.</div>
    </div>

    <div class="field">
      <label for="urdu">Urdu Address</label>
      <textarea name="urdu" id="urdu" placeholder="اردو پتہ یہاں لکھیں…" required>{{ form_urdu }}</textarea>
    </div>

    <div class="field">
      <label for="roman_urdu">Translation Output</label>
      <textarea id="roman_urdu" placeholder="Translation will appear here…" readonly>{{ form_roman_urdu }}</textarea>
    </div>

    <button type="submit" id="submit-btn">Translate →</button>
  </form>

  {% if error_msg %}
  <div class="error-box">⚠️ {{ error_msg }}</div>
  {% endif %}
</div>
</body>
</html>
"""


@web.route("/servertime")
def servertime():
    """Return the server's current unix time so the browser can sync IAT."""
    return jsonify({"unix": int(time.time())})


@web.route("/proxyhealth")
def proxyhealth():
    """Proxy /health from the FastAPI backend so the browser can poll it (avoids CORS)."""
    try:
        r = http_session.get(HEALTH_URL, timeout=3)
        return jsonify(r.json()), r.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "API offline"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@web.route("/", methods=["GET", "POST"])
def index():
    skew = int(os.getenv("IAT_SKEW_SECONDS", "30"))
    now  = int(time.time())

    error_msg = ""
    form_aud  = ALLOWED_AUD[0]
    form_iat  = str(now)
    form_urdu = ""
    form_roman_urdu = ""

    if request.method == "POST":
        form_aud  = request.form.get("aud",  ALLOWED_AUD[0])
        form_iat  = request.form.get("iat",  str(now))
        form_urdu = request.form.get("urdu", "")

        payload = {
            "aud":  form_aud,
            "iat":  int(form_iat),
            "urdu": form_urdu,
        }

        # Local LLM can take several minutes on first call — use a generous timeout.
        try:
            resp = http_session.post(API_URL, json=payload, timeout=360)
            if resp.ok:
                data   = resp.json()
                result = {
                    "aud":        data.get("aud"),
                    "iat":        data.get("iat"),
                    "roman_urdu": data.get("roman_urdu"),
                    "backend":    data.get("backend", "?"),
                }
                form_roman_urdu = result["roman_urdu"] or ""
            else:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                result = {
                    "error":       f"HTTP {resp.status_code} — {detail}",
                    "http_status": resp.status_code,
                    "iat_sent":    payload["iat"],
                    "server_time": int(time.time()),
                }
        except requests.exceptions.ConnectionError:
            result = {"error": f"Cannot connect to API (is the FastAPI server running on {API_BASE}?)"}
        except requests.exceptions.Timeout:
            result = {"error": "Request timed out — the local model may still be loading. Try again in a moment."}
        except Exception as e:
            result = {"error": str(e)}

        if "error" in result:
            error_msg = result["error"]

        form_iat = str(int(time.time()))   # refresh iat after submit

    return render_template_string(
        HTML,
        auds=ALLOWED_AUD,
        form_aud=form_aud,
        form_iat=form_iat,
        form_urdu=form_urdu,
        form_roman_urdu=form_roman_urdu,
        now=now,
        skew=skew,
        error_msg=error_msg,
    )


if __name__ == "__main__":
    web.run(host="0.0.0.0", port=5001, debug=True)
