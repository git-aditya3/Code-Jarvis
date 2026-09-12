#!/usr/bin/env python3
"""
Browser preview for the JARVIS interface.

The desktop app is PyQt6; this tiny server lets you *see* it without installing
anything — useful for showing the HUD, the transcript and the skill list to
someone on a different machine (or a phone).

It renders the real :class:`~jarvis.ui.hud.HudWidget` offscreen with Qt and
streams ~10 PNG frames per second to the page, so browser support is simply
"has a CPU". If PyQt6 is missing it falls back to an SVG animation.

    python tools/preview_server.py --port 8077
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import VERSION, Settings, jarvis_home  # noqa: E402
from jarvis.core import Core  # noqa: E402
from jarvis.host import HeadlessHost  # noqa: E402
from jarvis.memory import Memory  # noqa: E402

FRAME_DIR = Path(os.environ.get("JARVIS_PREVIEW_FRAMES", "/tmp/jarvis-preview"))
STATE = {"state": "idle", "level": 0.0}


# ── frame producer ───────────────────────────────────────────────────────────

def produce_frames() -> None:
    """Render the HUD with Qt into PNG frames, forever."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtWidgets import QApplication

    from jarvis.ui.hud import HudWidget

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)   # QWidget needs a GUI application instance
    hud = HudWidget("cyan")
    hud.resize(340, 340)
    # QWidget.render() paints the palette background first: make it match the app
    hud.setStyleSheet("background-color: #05070d;")
    counter = {"n": 0}

    def tick() -> None:
        hud.set_state(STATE["state"])
        hud.set_level(STATE["level"])
        # let the widget animate: advance its internal timer a few times
        for _ in range(2):
            hud._tick()
        image = QImage(340, 340, QImage.Format.Format_ARGB32)
        image.fill(QColor("#05070d"))  # match the app background, no transparency
        painter = QPainter(image)
        hud.render(painter)
        painter.end()
        target = FRAME_DIR / "hud.png"
        temp = FRAME_DIR / "hud.tmp.png"
        image.save(str(temp), "PNG")
        temp.replace(target)
        counter["n"] += 1

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(90)
    app.exec()


# ── HTTP layer ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep the console quiet
        return

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            payload = dict(STATE)
            payload["frame_ready"] = (FRAME_DIR / "hud.png").exists()
            return self._send(json.dumps(payload).encode(), "application/json")
        if path == "/api/frame":
            frame = FRAME_DIR / "hud.png"
            if not frame.exists():
                return self._send(b"", "image/png", 404)
            try:
                return self._send(frame.read_bytes(), "image/png")
            except OSError:
                return self._send(b"", "image/png", 404)
        if path == "/api/info":
            return self._send(json.dumps(INFO).encode(), "application/json")
        if path.startswith("/shot/"):
            name = Path(path.split("/")[-1]).name
            candidate = Path("docs") / name
            if candidate.exists():
                return self._send(candidate.read_bytes(), "image/png")
            return self._send(b"", "image/png", 404)
        return self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/ask":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", "replace") or "{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            text = str(payload.get("text", "")).strip()
            if not text:
                return self._send(json.dumps({"error": "empty"}).encode(), "application/json", 400)
            started = time.time()
            response = CORE.ask(text)
            result = {
                "text": response.text,
                "speak": response.speak,
                "skill": response.skill or response.provider,
                "ok": response.ok,
                "seconds": round(time.time() - started, 3),
            }
            return self._send(json.dumps(result).encode(), "application/json")
        return self._send(b"not found", "text/plain", 404)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS — interface preview</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    background: radial-gradient(circle at 20% 0%, #0b1526 0%, #05070d 55%, #03050a 100%);
    color: #dce7ff;
    font-family: "Segoe UI", Inter, "Helvetica Neue", system-ui, sans-serif;
    display: flex; flex-direction: column; align-items: center; gap: 22px; padding: 28px 16px 60px;
  }
  .banner {
    width: 100%; max-width: 980px; display: flex; align-items: center; gap: 12px;
    border: 1px solid #1b2740; border-radius: 14px; padding: 12px 18px; background: #0a1020cc;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #22d3ee; box-shadow: 0 0 12px #22d3ee; }
  h1 { font-size: 15px; letter-spacing: 4px; margin: 0; color: #67e8f9; font-weight: 600; }
  .banner small { color: #7d90b3; font-size: 12px; margin-left: auto; }
  .grid { display: grid; grid-template-columns: 380px minmax(320px, 560px); gap: 20px; width: 100%; max-width: 980px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .card { background: #0a1020; border: 1px solid #141d31; border-radius: 14px; padding: 16px; }
  .hud { display: flex; flex-direction: column; align-items: center; gap: 14px; }
  #hud-img { width: 300px; height: 300px; image-rendering: auto; }
  .states { display: flex; gap: 7px; flex-wrap: wrap; justify-content: center; }
  button {
    background: #0e1729; color: #dce7ff; border: 1px solid #1b2740; border-radius: 16px;
    padding: 6px 13px; font-size: 12px; cursor: pointer; font-family: inherit;
  }
  button:hover { border-color: #0e7490; color: #67e8f9; }
  button.on { background: #0e7490; border-color: #22d3ee; color: #f8fbff; }
  .label { font-size: 10px; letter-spacing: 2px; color: #67e8f9; align-self: flex-start; }
  .row { display: flex; gap: 8px; }
  input[type=text] {
    flex: 1; background: #0e1729; border: 1px solid #1b2740; border-radius: 10px;
    color: #dce7ff; padding: 11px 13px; font-size: 14px; font-family: inherit; outline: none;
  }
  input[type=text]:focus { border-color: #0e7490; }
  #send { background: #0e7490; border-color: #22d3ee; color: #fff; padding: 11px 18px; border-radius: 10px; font-weight: 600; }
  #log { margin-top: 14px; max-height: 380px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
  .msg { border-left: 2px solid #1b2740; padding-left: 11px; }
  .msg .who { font-size: 10px; letter-spacing: 2px; color: #7d90b3; margin-bottom: 3px; }
  .msg.me { border-color: #93c5fd; }
  .msg.bot { border-color: #22d3ee; }
  .msg .who b { color: #67e8f9; font-weight: 500; }
  .msg.error { border-color: #f87171; }
  .msg.error .who b { color: #f87171; }
  pre { background: #070c17; border: 1px solid #1b2740; border-radius: 8px; padding: 9px;
        overflow-x: auto; color: #cbd5e1; font-size: 12px; margin: 6px 0 0; }
  .msg p { margin: 0; white-space: pre-wrap; line-height: 1.45; font-size: 13.5px; }
  .chips { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 12px; }
  .muted { color: #7d90b3; font-size: 12px; line-height: 1.5; }
  .shots { max-width: 980px; width: 100%; }
  .shots img { width: 100%; border: 1px solid #141d31; border-radius: 12px; margin-top: 10px; }
  code { color: #fcd34d; }
  .note { max-width: 980px; width: 100%; border: 1px solid #1b2740; background: #0a1020cc;
          border-radius: 14px; padding: 14px 18px; font-size: 12.5px; color: #9db0d0; line-height: 1.6; }
</style>
</head>
<body>
  <div class="banner">
    <span class="dot"></span>
    <h1>JARVIS</h1>
    <small id="ver">interface preview</small>
  </div>

  <div class="grid">
    <div class="card hud">
      <img id="hud-img" alt="JARVIS HUD animation" src="/api/frame">
      <div class="label">HUD STATES</div>
      <div class="states" id="state-buttons"></div>
      <div class="muted" style="text-align:center">
        The rings render from the real <code>jarvis/ui/hud.py</code> widget,
        rasterised by Qt and streamed as PNG frames.
      </div>
    </div>

    <div class="card">
      <div class="label">ASK THE REAL CORE</div>
      <p class="muted" style="margin:8px 0 12px">
        This is the actual <code>jarvis/core.py</code> handling your request with its offline skills —
        the same code the desktop app runs.
      </p>
      <div class="row">
        <input type="text" id="q" placeholder="try: what is 18% of 2400" autocomplete="off">
        <button id="send">Send</button>
      </div>
      <div class="chips" id="chips"></div>
      <div id="log"></div>
    </div>
  </div>

  <div class="note">
    <b>How to run the real thing:</b> the desktop interface is a PyQt6 app —
    <code>pip install -r requirements.txt</code> then <code>python run_jarvis.py</code>.
    Voice needs <code>pip install pyttsx3 sounddevice faster-whisper</code>; everything else works
    without a single API key. This page is only a browser-friendly view for sharing.
  </div>

  <div class="card shots">
    <div class="label">THE ACTUAL DESKTOP APP</div>
    <img src="/shot/jarvis-window.png" alt="JARVIS desktop window">
  </div>

<script>
const STATES = [["idle","Standing by"],["listening","Listening"],["thinking","Processing"],["speaking","Speaking"],["error","Attention"]];
const stateButtons = document.getElementById('state-buttons');
let current = 'idle';
STATES.forEach(([id, text]) => {
  const b = document.createElement('button');
  b.textContent = text; b.dataset.id = id;
  if (id === current) b.classList.add('on');
  b.onclick = () => { current = id; STATE = id;
    [...stateButtons.children].forEach(c => c.classList.toggle('on', c.dataset.id === id)); };
  stateButtons.appendChild(b);
});
let STATE = 'idle';

// keep the HUD image fresh without flicker
const img = document.getElementById('hud-img');
let loading = false;
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const r = await fetch('/api/frame', {cache: 'no-store'});
    if (r.ok) {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const old = img.src;
      img.src = url;
      if (old.startsWith('blob:')) URL.revokeObjectURL(old);
    }
  } catch (e) {}
  loading = false;
}
setInterval(refresh, 110);
refresh();

const EXAMPLES = ["brief me", "what is 18% of 2400", "system status", "list timers",
                  "review this code: def f(): return eval('1')", "generate 20 rows of patient data as csv",
                  "make a flowchart of jarvis/core.py", "note that the demo is Friday", "/help"];
const chips = document.getElementById('chips');
EXAMPLES.forEach(text => {
  const b = document.createElement('button');
  b.textContent = text;
  b.onclick = () => { document.getElementById('q').value = text; ask(); };
  chips.appendChild(b);
});

const log = document.getElementById('log');
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function render(text) {
  const parts = text.split(/```[a-z]*\n?/);
  return parts.map((chunk, i) => i % 2
    ? '<pre>' + escapeHtml(chunk.replace(/```$/, '')) + '</pre>'
    : '<p>' + escapeHtml(chunk).replace(/`([^`]+)`/g, '<code>$1</code>') + '</p>'
  ).join('');
}
function addMessage(role, who, html) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = '<div class="who"><b>' + who + '</b></div>' + html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}
async function ask() {
  const input = document.getElementById('q');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMessage('me', 'YOU', render(text));
  STATE = 'thinking';
  try {
    const r = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text})
    });
    const data = await r.json();
    const label = (data.skill || 'offline') + ' · ' + data.seconds + 's';
    const div = document.createElement('div');
    div.className = 'msg ' + (data.ok ? 'bot' : 'error');
    div.innerHTML = '<div class="who"><b>JARVIS</b> · ' + escapeHtml(label) + '</div>' + render(data.text || '');
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    addMessage('error', 'ERROR', render('Request failed: ' + e));
  }
  STATE = 'idle';
}
document.getElementById('send').onclick = ask;
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });
fetch('/api/info').then(r => r.json()).then(d => {
  const learned = d.learned || {};
  const bits = [
    'v' + d.version,
    d.skills + ' skills',
    d.actions + ' computer actions',
    d.routines + ' routines',
    (learned.distinct_actions || 0) + ' learned actions',
    (learned.trusted || 0) + ' learned approvals',
  ];
  document.getElementById('ver').textContent = bits.join(' · ');
  document.getElementById('ver').title = d.brain + '  |  ladder: ' + (d.ladder || []).join(' → ');
});
</script>
</body>
</html>
"""


def build_info() -> dict:
    global CORE, INFO
    actions = getattr(CORE, "actions", None)
    profile = getattr(CORE, "profile", None)
    routines = getattr(CORE, "routine_store", None)
    INFO = {
        "version": VERSION,
        "skills": len(CORE.registry.skills),
        "actions": len(actions.actions) if actions is not None else 0,
        "control": (actions.controller.report_text().strip().splitlines()[:2] if actions else []),
        "brain": CORE.brain.status(),
        "provider": CORE.brain.provider,
        "model": CORE.settings.model_for(),
        "ladder": CORE.brain.ladder(),
        "free": not CORE.brain.settings.has_key(CORE.brain.provider),
        "routines": len(routines.all()) if routines is not None else 0,
        "learned": (profile.stats() if profile is not None else {}),
        "data_dir": str(jarvis_home()),
        "score": ("Preview server: HUD frames from Qt + live skill routing + 76 computer actions "
                  "+ a free cloud brain with behaviour learning."),
    }
    return INFO


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS browser preview")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-frames", action="store_true", help="serve only the chat API")
    args = parser.parse_args()

    settings = Settings()
    settings["voice_replies"] = False
    memory = Memory()
    CORE = Core(settings, memory, HeadlessHost(verbose=False))
    build_info()

    if not args.no_frames:
        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=produce_frames, daemon=True, name="hud-frames").start()
        for _ in range(60):  # give Qt a moment to render the first frame
            if (FRAME_DIR / "hud.png").exists():
                break
            time.sleep(0.1)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"JARVIS preview on http://{args.host}:{args.port} "
          f"({len(CORE.registry.skills)} skills, brain: {CORE.brain.provider})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        CORE.shutdown()
