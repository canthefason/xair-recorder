#!/usr/bin/env python3
"""Minimal, dependency-free web control page for the X Air recorder.

Serves a one-page UI with Start/Stop buttons plus a JSON /status endpoint,
so recording can be controlled from a phone or laptop joined to the
device's own WiFi access point.
"""
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import recorder

HOST = "0.0.0.0"
PORT = 8080

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo.svg"

PAGE = """<!doctype html>
<title>X Air Recorder</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: sans-serif; max-width: 420px; margin: 2rem auto; text-align: center; }
  #logo { max-width: 220px; max-height: 140px; margin-bottom: 0.5rem; }
  button { font-size: 1.5rem; padding: 1rem 2rem; margin: 1rem; border-radius: 0.5rem; border: none; color: white; }
  button:disabled { background: #9e9e9e; opacity: 0.6; }
  #start:not(:disabled) { background: #2e7d32; }
  #stop:not(:disabled) { background: #c62828; }
  .net { background: #455a64; font-size: 1rem; padding: 0.6rem 1.2rem; }
  #status { font-size: 1rem; text-align: left; margin-top: 1rem; }
  .row { display: flex; justify-content: space-between; gap: 1rem; padding: 0.35rem 0; border-bottom: 1px solid #ddd; }
  .row span:first-child { color: #666; }
  .row span:last-child { font-weight: 600; word-break: break-all; text-align: right; }
  h2 { margin-top: 1.5rem; font-size: 1.1rem; text-align: left; }
  #recordings { text-align: left; }
  .rec-row { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; padding: 0.5rem 0; border-bottom: 1px solid #ddd; }
  .rec-row .meta { font-size: 0.85rem; color: #666; }
  .rec-row .actions { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; }
  .rec-row .actions a.dl-btn,
  .rec-row .actions button.del,
  .rec-row .actions button.split-btn {
    display: inline-block;
    box-sizing: border-box;
    text-decoration: none;
    border: none;
    color: white !important;
    padding: 0.4rem 0.8rem !important;
    border-radius: 0.4rem;
    font-size: 0.9rem !important;
    line-height: 1.2;
    font-family: inherit;
    margin: 0;
    cursor: pointer;
  }
  .rec-row .actions a.dl-btn { background: #1565c0 !important; }
  .rec-row .actions button.split-btn { background: #6a1b9a !important; }
  .rec-row .actions button.split-btn:disabled { background: #b39ddb !important; cursor: default; }
  .rec-row .actions button.del { background: #c62828 !important; }
  .channels-list { margin: 0 0 0.6rem 0; padding-left: 0.6rem; border-left: 2px solid #ddd; }
  .channels-list .ch-row { display: flex; justify-content: space-between; padding: 0.2rem 0; font-size: 0.85rem; }
  .channels-list a { color: #1565c0; text-decoration: none; }
</style>
<img id="logo" src="/logo.svg" alt="X Air Recorder" onerror="this.style.display='none'">
<h1>X Air Recorder</h1>
<button id="start" onclick="post('/start')" disabled>Start</button>
<button id="stop" onclick="post('/stop')" disabled>Stop</button>
<hr>
<button class="net" onclick="switchNetwork('venue')">Switch to Venue AP</button>
<button class="net" onclick="switchNetwork('home')">Switch to Home WiFi</button>
<div id="status">loading...</div>
<h2>Recordings</h2>
<div id="recordings">loading...</div>
<script>
async function post(path) {
  const res = await fetch(path, { method: 'POST' });
  const data = await res.json();
  if (!res.ok) alert(data.error || 'request failed');
  refresh();
}
function switchNetwork(mode) {
  const label = mode === 'venue' ? 'Venue AP' : 'Home WiFi';
  if (!confirm(`Switch wlan0 to ${label}? This page will disconnect if you are` +
               ` connected over the network being switched away from.`)) return;
  fetch(`/${mode}-mode`, { method: 'POST' })
    .then(r => r.json())
    .then(d => alert(d.message || d.error || 'switching...'))
    .catch(() => alert('Request sent, but connection dropped as expected. Reconnect to the new network.'));
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function renderStatus(data) {
  const rows = [];
  rows.push(['Recording', data.recording ? '🔴 Recording' : '⚪ Idle']);
  if (data.recording) {
    rows.push(['File', data.file.split('/').pop()]);
    rows.push(['Elapsed', data.elapsed_seconds.toFixed(1) + ' s']);
  }
  rows.push(['Free space', data.free_minutes.toFixed(1) + ' min']);
  rows.push(['Network', data.network_mode]);
  document.getElementById('status').innerHTML =
    rows.map(([k, v]) => `<div class="row"><span>${escapeHtml(k)}</span><span>${escapeHtml(v)}</span></div>`).join('');
}
async function refresh() {
  const res = await fetch('/status');
  const data = await res.json();
  renderStatus(data);
  document.getElementById('start').disabled = data.recording;
  document.getElementById('stop').disabled = !data.recording;
}
function formatBytes(n) {
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}
function formatDate(ts) {
  return new Date(ts * 1000).toLocaleString();
}
let splitResults = {};  // recording name -> array of relative channel file paths
function renderChannelList(name) {
  const container = document.querySelector(`.channels-list[data-for="${CSS.escape(name)}"]`);
  const files = splitResults[name];
  if (!container || !files) return;
  container.innerHTML = files.map(f => {
    const label = f.split('/').pop();
    return `<div class="ch-row"><span>${escapeHtml(label)}</span><a href="/recordings/${encodeURIComponent(f)}" download>Download</a></div>`;
  }).join('');
}
async function refreshRecordings() {
  const res = await fetch('/recordings');
  const files = await res.json();
  const el = document.getElementById('recordings');
  if (!files.length) {
    el.innerHTML = '<div class="meta">No recordings yet.</div>';
    return;
  }
  el.innerHTML = files.map(f => `
    <div class="rec-row">
      <span>${escapeHtml(f.name)}<br><span class="meta">${formatBytes(f.size_bytes)} &middot; ${formatDate(f.modified)}</span></span>
      <span class="actions">
        <a class="dl-btn" href="/recordings/${encodeURIComponent(f.name)}" download>Download</a>
        <button class="split-btn" data-name="${escapeHtml(f.name)}">Split channels</button>
        <button class="del" data-name="${escapeHtml(f.name)}">Delete</button>
      </span>
    </div>
    <div class="channels-list" data-for="${escapeHtml(f.name)}"></div>
  `).join('');
  Object.keys(splitResults).forEach(renderChannelList);
}
document.getElementById('recordings').addEventListener('click', async (e) => {
  const name = e.target.dataset.name;
  if (!name) return;
  if (e.target.matches('.split-btn')) {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = 'Splitting...';
    try {
      const res = await fetch(`/recordings/${encodeURIComponent(name)}/split`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) { alert(data.error || 'split failed'); return; }
      splitResults[name] = data.files;
      renderChannelList(name);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Split channels';
    }
    return;
  }
  if (!e.target.matches('.del')) return;
  if (!confirm(`Delete ${name}? This cannot be undone.`)) return;
  const res = await fetch(`/recordings/${encodeURIComponent(name)}`, { method: 'DELETE' });
  const data = await res.json();
  if (!res.ok) alert(data.error || 'delete failed');
  refreshRecordings();
});
refresh();
refreshRecordings();
setInterval(refresh, 2000);
setInterval(refreshRecordings, 5000);
</script>
"""


def _switch_network_safe(mode):
    try:
        recorder.switch_network(mode)
    except recorder.RecorderError as exc:
        print(f"network switch to {mode} failed: {exc}")


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            self._json(recorder.status())
        elif self.path == "/recordings":
            self._json(recorder.list_recordings())
        elif self.path.startswith("/recordings/"):
            self._serve_recording(unquote(self.path[len("/recordings/"):]))
        elif self.path == "/logo.svg":
            if LOGO_PATH.exists():
                body = LOGO_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def _serve_recording(self, rel_path):
        # resolve_recording_path allows a top-level recording OR a file inside
        # its "<name>-channels" split folder, but nothing outside RECORDINGS_DIR.
        try:
            file_path = recorder.resolve_recording_path(rel_path)
        except recorder.RecorderError as exc:
            self._json({"error": str(exc)}, 404)
            return
        size = file_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with file_path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_POST(self):
        try:
            if self.path == "/start":
                self._json(recorder.start())
            elif self.path == "/stop":
                self._json(recorder.stop())
            elif self.path in ("/venue-mode", "/home-mode"):
                mode = "venue" if self.path == "/venue-mode" else "home"
                self._json({
                    "status": "switching",
                    "message": f"Switching to {mode} network in 2s - reconnect there.",
                })
                self.wfile.flush()
                # Delay the actual switch so this response has time to reach the
                # client before wlan0 (possibly the very link carrying it) drops.
                threading.Timer(2.0, _switch_network_safe, args=(mode,)).start()
            elif self.path.startswith("/recordings/") and self.path.endswith("/split"):
                name = unquote(self.path[len("/recordings/"):-len("/split")])
                self._json({"files": recorder.split_recording(name)})
            else:
                self._json({"error": "not found"}, 404)
        except recorder.RecorderError as exc:
            self._json({"error": str(exc)}, 400)

    def do_DELETE(self):
        if self.path.startswith("/recordings/"):
            name = unquote(self.path[len("/recordings/"):])
            try:
                recorder.delete_recording(name)
                self._json({"status": "deleted"})
            except recorder.RecorderError as exc:
                self._json({"error": str(exc)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        # Default BaseHTTPRequestHandler logging goes to stderr, which
        # systemd/journalctl captures - keep it so start/stop/errors are
        # traceable after the fact.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"X Air recorder control listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
