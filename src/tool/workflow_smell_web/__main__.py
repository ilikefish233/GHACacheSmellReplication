"""Run with: python -m workflow_smell_web"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workflow_smell_web.service import ScanError, scan_github


ASSETS = Path(__file__).parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path not in FILES:
            self.send_error(404)
            return
        filename, content_type = FILES[self.path]
        payload = (ASSETS / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path != "/api/scan":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096:
                raise ScanError("The request body must be between 1 byte and 4 KB.", 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("repository", "workflow_path")):
                raise ScanError("Provide a repository name and workflow file path.")
            self._json(200, scan_github(data["repository"], data["workflow_path"]))
        except ScanError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Invalid request format."})
        except Exception:
            logging.exception("Workflow scan failed")
            self._json(500, {"error": "An internal error occurred while scanning. Check the server logs."})


def main() -> None:
    parser = argparse.ArgumentParser(description="Web tool for detecting 11 GitHub Actions cache smell types")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Open http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
