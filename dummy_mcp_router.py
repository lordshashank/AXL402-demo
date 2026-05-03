"""Fake MCP router for local testing.

Implements the /route endpoint AXL forwards inbound MCP envelopes to.
Returns a hardcoded JSON-RPC tools/list response so we can see the full
gate -> dispatch -> response path complete cleanly.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


FAKE_TOOLS = {
    "jsonrpc": "2.0",
    "id": None,
    "result": {
        "tools": [
            {"name": "echo", "description": "Echo back input."},
            {"name": "search", "description": "Pretend search."},
        ]
    },
}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        envelope = json.loads(body)
        inner_req = envelope.get("request", {})
        if isinstance(inner_req, str):
            inner_req = json.loads(inner_req)
        resp_id = inner_req.get("id") if isinstance(inner_req, dict) else None
        inner = dict(FAKE_TOOLS)
        inner["id"] = resp_id
        out = json.dumps({"response": inner, "error": ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a, **k):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9103), H).serve_forever()
