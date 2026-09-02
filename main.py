#!/usr/bin/env python3
"""
share.py - Share files with anyone on the same WiFi network.

Usage:
    python3 share.py

Then on any device connected to the SAME WiFi, open a browser and go to:
    http://<your-computer-ip>:8000

You'll see a file listing of the current folder. People can:
  - Click any file to download it
  - Use the "Choose File" button at the top to upload a file to you

Press Ctrl+C to stop sharing.
"""

import http.server
import socketserver
import socket
import os
import cgi

PORT = 8000
UPLOAD_DIR = os.getcwd()  # files are served/uploaded from the current folder


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


class ShareHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files for download and adds a simple upload page."""

    def do_GET(self):
        if self.path == "/":
            self.send_upload_form()
        else:
            super().do_GET()

    def send_upload_form(self):
        files = os.listdir(UPLOAD_DIR)
        file_links = "".join(
            f'<li><a href="/{f}">{f}</a></li>'
            for f in files
            if os.path.isfile(os.path.join(UPLOAD_DIR, f))
        )
        html = f"""
        <html>
        <head><title>File Share</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 40px auto;">
            <h2>📁 Shared Folder</h2>
            <form enctype="multipart/form-data" method="post">
                <input type="file" name="file">
                <input type="submit" value="Upload">
            </form>
            <hr>
            <h3>Files available to download:</h3>
            <ul>{file_links or "<li><em>No files yet</em></li>"}</ul>
        </body>
        </html>
        """
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST"},
        )
        if "file" in form:
            file_item = form["file"]
            if file_item.filename:
                filepath = os.path.join(UPLOAD_DIR, os.path.basename(file_item.filename))
                with open(filepath, "wb") as f:
                    f.write(file_item.file.read())
                print(f"Received file: {file_item.filename}")

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


if __name__ == "__main__":
    os.chdir(UPLOAD_DIR)
    ip = get_local_ip()
    with socketserver.TCPServer(("0.0.0.0", PORT), ShareHandler) as httpd:
        print("=" * 50)
        print(" File sharing is running!")
        print(f" On any device connected to the same WiFi, open:")
        print(f"   http://{ip}:{PORT}")
        print("=" * 50)
        print("Press Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")