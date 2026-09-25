#!/usr/bin/env python3
"""
share.py - Share files with anyone on the same WiFi network.

Usage:
    python3 share.py [folder] [--port 8000]

Then on any device connected to the SAME WiFi, open a browser and go to:
    http://<your-computer-ip>:8000

People can:
  - Click any file to download it
  - Drag & drop (or use "Choose File") to upload a file to you

Press Ctrl+C to stop sharing.
"""

import http.server
import socket
import os
import sys
import argparse
import html
import shutil
import urllib.parse
import uuid
import time
import json
import re
import threading

DEFAULT_PORT = 8000
ONLINE_WINDOW = 15  # seconds since last poll before a device is considered "offline"

DEVICES = {}          # device_id -> {"name", "ip", "last_seen"}
DEVICES_LOCK = threading.Lock()


def touch_device(device_id, ip):
    """Register a device or update its last-seen time / IP."""
    with DEVICES_LOCK:
        d = DEVICES.get(device_id)
        if d is None:
            DEVICES[device_id] = {"name": f"Device …{ip.split('.')[-1]}", "ip": ip, "last_seen": time.time()}
        else:
            d["ip"] = ip
            d["last_seen"] = time.time()


def get_local_ip():
    """Find this machine's LAN IP address (the one other devices can reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def human_size(num_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def is_safe_filename(name):
    """Reject empty names, path separators, and traversal attempts."""
    if not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return True


def unique_path(directory, filename):
    """If filename already exists in directory, append ' (1)', ' (2)', etc. until it doesn't."""
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base} ({counter}){ext}"
        counter += 1
    return os.path.join(directory, candidate)


def parse_multipart(rfile, content_type, content_length):
    """
    Minimal multipart/form-data parser (avoids the deprecated cgi module).
    Returns (file_bytes, filename, fields) for the first file field found
    plus a dict of any other (non-file) text fields, e.g. {"target": "..."}.
    """
    fields = {}
    if "boundary=" not in content_type:
        return None, None, fields
    boundary = content_type.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = ("--" + boundary).encode()

    body = rfile.read(content_length)
    parts = body.split(boundary_bytes)

    file_data, filename = None, None

    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, data = part.split(b"\r\n\r\n", 1)
        headers_text = headers_raw.decode(errors="replace")
        if "Content-Disposition" not in headers_text:
            continue
        try:
            disp_line = next(
                l for l in headers_text.split("\r\n") if "Content-Disposition" in l
            )
        except StopIteration:
            continue

        name, part_filename = None, None
        for chunk in disp_line.split(";"):
            chunk = chunk.strip()
            if chunk.startswith("name="):
                name = chunk[len("name="):].strip('"')
            elif chunk.startswith("filename="):
                part_filename = chunk[len("filename="):].strip('"')

        if data.endswith(b"\r\n"):
            data = data[:-2]

        if part_filename:
            if file_data is None:  # only keep the first file field
                file_data, filename = data, os.path.basename(part_filename)
        elif name:
            fields[name] = data.decode(errors="replace")

    return file_data, filename, fields


PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Share</title>
<style>
  :root {{
    --bg: #0f1115;
    --card: #171a21;
    --border: #262a33;
    --text: #e8e8ea;
    --muted: #8b90a0;
    --accent: #5b8def;
    --accent-hover: #4a7ade;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 24px 16px 60px;
  }}
  .wrap {{ max-width: 640px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 24px; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
  }}
  #dropzone {{
    border: 2px dashed var(--border);
    border-radius: 10px;
    padding: 28px 16px;
    text-align: center;
    color: var(--muted);
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }}
  #dropzone.drag {{ border-color: var(--accent); background: rgba(91,141,239,0.08); }}
  #dropzone input {{ display: none; }}
  .btn {{
    display: inline-block;
    background: var(--accent);
    color: white;
    border: none;
    border-radius: 8px;
    padding: 9px 18px;
    font-size: 0.9rem;
    cursor: pointer;
    margin-top: 10px;
  }}
  .btn:hover {{ background: var(--accent-hover); }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 4px;
    border-bottom: 1px solid var(--border);
  }}
  li:last-child {{ border-bottom: none; }}
  li a {{ color: var(--text); text-decoration: none; font-size: 0.95rem; }}
  li a:hover {{ color: var(--accent); }}
  .size {{ color: var(--muted); font-size: 0.8rem; white-space: nowrap; margin-left: 12px; }}
  .empty {{ color: var(--muted); font-style: italic; padding: 8px 4px; }}
  #status {{ margin-top: 10px; font-size: 0.85rem; color: var(--muted); }}
  .device-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 4px;
    border-bottom: 1px solid var(--border);
    font-size: 0.9rem;
  }}
  .device-row:last-child {{ border-bottom: none; }}
  .device-row .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: #3ddc84; flex-shrink: 0;
  }}
  .device-row .name {{ flex: 1; }}
  .device-row .ip {{ color: var(--muted); font-size: 0.78rem; }}
  .device-row .rename {{
    background: none; border: none; color: var(--muted);
    cursor: pointer; font-size: 0.85rem;
  }}
  #inboxBanner {{ margin-bottom: 16px; }}
  .inbox-item {{
    background: rgba(61,220,132,0.1);
    border: 1px solid #3ddc84;
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 8px;
    font-size: 0.9rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .inbox-item a {{ color: #3ddc84; font-weight: 600; text-decoration: none; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>&#128193; File Share</h1>
  <div class="subtitle">Anyone on this WiFi network can view this page.</div>

  <div id="inboxBanner"></div>

  <div class="card">
    <h3 style="margin-top:0;">Devices on this share</h3>
    <div id="deviceList"><div class="empty">Looking for devices...</div></div>
  </div>

  <div class="card">
    <form id="uploadForm" enctype="multipart/form-data" method="post">
      <div id="dropzone">
        <div>Drag &amp; drop a file here, or</div>
        <label class="btn" for="fileInput">Choose File</label>
        <input id="fileInput" type="file" name="file">
      </div>
      <div style="margin-top:12px; font-size:0.85rem; color:var(--muted);">
        Send to:
        <select id="targetSelect" style="margin-left:6px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 8px;">
          <option value="__all__">Everyone (shared folder)</option>
        </select>
      </div>
      <div id="status"></div>
    </form>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">Files available to download</h3>
    <ul>{file_links}</ul>
  </div>
</div>

<script>
  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('fileInput');
  const status = document.getElementById('status');

  const targetSelect = document.getElementById('targetSelect');

  function uploadFile(file) {{
    const data = new FormData();
    data.append('file', file);
    data.append('target', targetSelect.value);
    const label = targetSelect.options[targetSelect.selectedIndex].text;
    status.textContent = 'Sending ' + file.name + ' to ' + label + '...';
    fetch('/', {{ method: 'POST', body: data }})
      .then(() => window.location.reload())
      .catch(() => {{ status.textContent = 'Upload failed. Try again.'; }});
  }}

  // --- Live device list ---
  function refreshDevices() {{
    fetch('/devices').then(r => r.json()).then(devices => {{
      const list = document.getElementById('deviceList');
      if (!devices.length) {{
        list.innerHTML = '<div class="empty">No other devices have opened this page yet.</div>';
      }} else {{
        list.innerHTML = devices.map(d => `
          <div class="device-row">
            <span class="dot"></span>
            <span class="name">${{d.name}}${{d.self ? ' (you)' : ''}}</span>
            <span class="ip">${{d.ip}}</span>
            ${{d.self ? '<button class="rename" data-rename>rename</button>' : ''}}
          </div>`).join('');
        const renameBtn = list.querySelector('[data-rename]');
        if (renameBtn) renameBtn.addEventListener('click', renameSelf);
      }}
      const prev = targetSelect.value;
      targetSelect.innerHTML = '<option value="__all__">Everyone (shared folder)</option>' +
        devices.filter(d => !d.self).map(d => `<option value="${{d.id}}">${{d.name}}</option>`).join('');
      if ([...targetSelect.options].some(o => o.value === prev)) targetSelect.value = prev;
    }}).catch(() => {{}});
  }}

  function renameSelf() {{
    const name = prompt('Name this device:');
    if (!name) return;
    fetch('/rename', {{ method: 'POST', body: JSON.stringify({{ name }}) }}).then(refreshDevices);
  }}

  // --- Inbox: files sent directly to this device ---
  const seenInbox = new Set();
  function refreshInbox() {{
    fetch('/inbox-status').then(r => r.json()).then(items => {{
      const banner = document.getElementById('inboxBanner');
      banner.innerHTML = items.map(it => `
        <div class="inbox-item">
          <span>&#128229; Received <strong>${{it.name}}</strong> (${{it.size}})</span>
          <a href="${{it.url}}" download>Download</a>
        </div>`).join('');
      if (items.some(it => !seenInbox.has(it.name))) {{
        items.forEach(it => seenInbox.add(it.name));
      }}
    }}).catch(() => {{}});
  }}

  refreshDevices();
  refreshInbox();
  setInterval(refreshDevices, 4000);
  setInterval(refreshInbox, 4000);

  fileInput.addEventListener('change', () => {{
    if (fileInput.files.length) uploadFile(fileInput.files[0]);
  }});

  ['dragenter', 'dragover'].forEach(evt =>
    dropzone.addEventListener(evt, e => {{
      e.preventDefault();
      dropzone.classList.add('drag');
    }})
  );
  ['dragleave', 'drop'].forEach(evt =>
    dropzone.addEventListener(evt, e => {{
      e.preventDefault();
      dropzone.classList.remove('drag');
    }})
  );
  dropzone.addEventListener('drop', e => {{
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  }});
</script>
</body>
</html>
"""


class ShareHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files for download and adds a simple drag-and-drop upload page."""

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} -> {fmt % args}")

    def copyfile(self, source, outputfile):
        # Bigger chunks than the 16KB default reduce syscall overhead on large files.
        shutil.copyfileobj(source, outputfile, length=1024 * 1024)

    def get_device_id(self):
        """Read the device_id cookie, or mint a fresh one. Returns (id, is_new)."""
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("device_id="):
                candidate = part[len("device_id="):]
                if re.fullmatch(r"[0-9a-f]{32}", candidate):
                    return candidate, False
        return uuid.uuid4().hex, True

    def do_GET(self):
        device_id, is_new = self.get_device_id()
        touch_device(device_id, self.client_address[0])

        if self.path == "/":
            self.send_upload_form(device_id, is_new)
        elif self.path == "/devices":
            self.send_devices_json(device_id, is_new)
        elif self.path == "/inbox-status":
            self.send_inbox_status(device_id, is_new)
        else:
            super().do_GET()

    def set_device_cookie(self, device_id):
        self.send_header("Set-Cookie", f"device_id={device_id}; Path=/; Max-Age=86400")

    def send_upload_form(self, device_id, is_new):
        entries = []
        for f in sorted(os.listdir(self.directory)):
            full = os.path.join(self.directory, f)
            if os.path.isfile(full):
                size = human_size(os.path.getsize(full))
                safe_name = html.escape(f)
                url_name = urllib.parse.quote(f)
                entries.append(
                    f'<li><a href="/{url_name}">{safe_name}</a>'
                    f'<span class="size">{size}</span></li>'
                )
        file_links = "".join(entries) or '<li class="empty">No files yet</li>'

        page = PAGE_TEMPLATE.format(file_links=file_links)
        encoded = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if is_new:
            self.set_device_cookie(device_id)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, obj, device_id, is_new):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if is_new:
            self.set_device_cookie(device_id)
        self.end_headers()
        self.wfile.write(body)

    def send_devices_json(self, self_id, is_new):
        now = time.time()
        with DEVICES_LOCK:
            online = [
                {"id": did, "name": d["name"], "ip": d["ip"], "self": did == self_id}
                for did, d in DEVICES.items()
                if now - d["last_seen"] <= ONLINE_WINDOW
            ]
        online.sort(key=lambda d: (not d["self"], d["name"]))
        self._send_json(online, self_id, is_new)

    def send_inbox_status(self, device_id, is_new):
        inbox_dir = os.path.join(self.directory, ".inbox", device_id)
        items = []
        if os.path.isdir(inbox_dir):
            for f in sorted(os.listdir(inbox_dir)):
                full = os.path.join(inbox_dir, f)
                if os.path.isfile(full):
                    items.append({
                        "name": f,
                        "url": f"/.inbox/{device_id}/{urllib.parse.quote(f)}",
                        "size": human_size(os.path.getsize(full)),
                    })
        self._send_json(items, device_id, is_new)

    def do_POST(self):
        device_id, is_new = self.get_device_id()
        touch_device(device_id, self.client_address[0])

        if self.path == "/rename":
            self.handle_rename(device_id, is_new)
            return

        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length <= 0 or "multipart/form-data" not in content_type:
            self.send_response(400)
            self.end_headers()
            return

        data, filename, fields = parse_multipart(self.rfile, content_type, content_length)
        target = fields.get("target", "").strip()

        if data is not None and filename and is_safe_filename(filename):
            if target and target != "__all__":
                # Direct send: goes only into that device's private inbox.
                inbox_dir = os.path.join(self.directory, ".inbox", target)
                os.makedirs(inbox_dir, exist_ok=True)
                filepath = unique_path(inbox_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(data)
                print(f"  Sent '{os.path.basename(filepath)}' directly to device {target[:8]} ({human_size(len(data))})")
            else:
                filepath = unique_path(self.directory, filename)
                with open(filepath, "wb") as f:
                    f.write(data)
                print(f"  Received file: {os.path.basename(filepath)} ({human_size(len(data))})")
        elif filename:
            print(f"  Rejected unsafe filename: {filename!r}")

        self.send_response(303)
        self.send_header("Location", "/")
        if is_new:
            self.set_device_cookie(device_id)
        self.end_headers()

    def handle_rename(self, device_id, is_new):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            new_name = str(payload.get("name", "")).strip()[:40]
        except Exception:
            new_name = ""
        if new_name:
            with DEVICES_LOCK:
                if device_id in DEVICES:
                    DEVICES[device_id]["name"] = html.escape(new_name)
        self.send_response(204)
        if is_new:
            self.set_device_cookie(device_id)
        self.end_headers()


def make_handler(directory):
    def handler(*args, **kwargs):
        return ShareHandler(*args, directory=directory, **kwargs)
    return handler


def main():
    parser = argparse.ArgumentParser(description="Share files with anyone on the same WiFi network.")
    parser.add_argument("folder", nargs="?", default=os.getcwd(), help="Folder to share (default: current directory)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to serve on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    directory = os.path.abspath(args.folder)
    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a valid folder.")
        sys.exit(1)

    ip = get_local_ip()
    handler = make_handler(directory)

    try:
        with http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler) as httpd:
            print("=" * 50)
            print(" File sharing is running!")
            print(f" Sharing folder: {directory}")
            print(f" On any device connected to the same WiFi, open:")
            print(f"   http://{ip}:{args.port}")
            print("=" * 50)
            print("Press Ctrl+C to stop.\n")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.")
    except OSError as e:
        print(f"Error: could not start server on port {args.port} ({e}).")
        print("Try a different port with --port <number>.")
        sys.exit(1)


if __name__ == "__main__":
    main()