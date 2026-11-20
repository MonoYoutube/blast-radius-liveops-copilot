"""A local stand-in for a model provider, so tests never call a real API."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class ModelStub:
    """Serves POST requests with whatever `responder(path, body)` returns as (status, payload)."""

    def __init__(self, responder):
        self.seen = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.seen.append((self.path, self.headers.get("Authorization"), body))
                status, payload = responder(self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
