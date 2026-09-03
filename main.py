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
import socketserver
import socket
import os
import sys
import argparse
import html
import urllib.parse

DEFAULT_PORT = 8000


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


def parse_multipart(rfile, content_type, content_length):
    """
    Minimal multipart/form-data parser (avoids the deprecated cgi module).
    Returns (raw_bytes, filename) for the first file field found, or (None, None).
    """
    if "boundary=" not in content_type:
        return None, None
    boundary = content_type.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = ("--" + boundary).encode()

    body = rfile.read(content_length)
    parts = body.split(boundary_bytes)

    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_raw, data = part.split(b"\r\n\r\n", 1)
        headers_text = headers_raw.decode(errors="replace")
        if "filename=" not in headers_text:
            continue
        try:
            disp_line = next(
                l for l in headers_text.split("\r\n") if "Content-Disposition" in l
            )
        except StopIteration:
            continue
        filename = None
        for chunk in disp_line.split(";"):
            chunk = chunk.strip()
            if chunk.startswith("filename="):
                filename = chunk[len("filename="):].strip('"')
        if not filename:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return data, os.path.basename(filename)

    return None, None


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
</style>
</head>
<body>
<div class="wrap">
  <h1>&#128193; File Share</h1>
  <div class="subtitle">Anyone on this WiFi network can view this page.</div>

  <div class="card">
    <form id="uploadForm" enctype="multipart/form-data" method="post">
      <div id="dropzone">
        <div>Drag &amp; drop a file here, or</div>
        <label class="btn" for="fileInput">Choose File</label>
        <input id="fileInput" type="file" name="file">
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

  function uploadFile(file) {{
    const data = new FormData();
    data.append('file', file);
    status.textContent = 'Uploading ' + file.name + '...';
    fetch('/', {{ method: 'POST', body: data }})
      .then(() => window.location.reload())
      .catch(() => {{ status.textContent = 'Upload failed. Try again.'; }});
  }}

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

    def do_GET(self):
        if self.path == "/":
            self.send_upload_form()
        else:
            super().do_GET()

    def send_upload_form(self):
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
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length <= 0 or "multipart/form-data" not in content_type:
            self.send_response(400)
            self.end_headers()
            return

        data, filename = parse_multipart(self.rfile, content_type, content_length)

        if data is not None and filename and is_safe_filename(filename):
            filepath = os.path.join(self.directory, filename)
            with open(filepath, "wb") as f:
                f.write(data)
            print(f"  Received file: {filename} ({human_size(len(data))})")
        elif filename:
            print(f"  Rejected unsafe filename: {filename!r}")

        self.send_response(303)
        self.send_header("Location", "/")
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
        with socketserver.TCPServer(("0.0.0.0", args.port), handler) as httpd:
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