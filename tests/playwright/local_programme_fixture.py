"""Disposable static fixture for the real browser engine (no station state)."""

import argparse
import json
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import soundfile as sf

from tests.playwright.local_programme_measure import signal

STATIC = Path(__file__).resolve().parents[2] / "src/autodj/static"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    sf.write(args.root / "reference.wav", signal(44100, 30), 44100, subtype="FLOAT")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC), **kw)

        def do_GET(self):
            if urlsplit(self.path).path == "/audit":
                content = re.sub(
                    r"<script\b[^>]*>.*?</script>",
                    "",
                    (STATIC / "index.html").read_text(encoding="utf-8"),
                    flags=re.DOTALL,
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif urlsplit(self.path).path == "/api/audio":
                content = (args.root / "reference.wav").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                super().do_GET()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/shutdown":
                self.send_response(200)
                self.end_headers()
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            time.sleep(0.5)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(json.dumps({"port": server.server_port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
