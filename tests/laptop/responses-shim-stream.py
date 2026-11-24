#!/usr/bin/env python3
"""Streaming Responses requests must be rejected explicitly (not 502 json.loads)."""
import http.client
import importlib.machinery
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "scripts/laptop/shims/responses_shim.py"


def load_shim():
    loader = importlib.machinery.SourceFileLoader("responses_shim", str(SHIM))
    spec = importlib.util.spec_from_loader("responses_shim", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def main() -> int:
    shim = load_shim()
    shim.Handler.upstream = "http://127.0.0.1:1"  # must not be contacted
    server = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"model": "fixture", "input": "hi", "stream": True})
        conn.request("POST", "/v1/responses", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        if response.status != 400 or "stream" not in data.get("error", {}).get("param", ""):
            print(f"FAIL: expected 400 stream rejection, got {response.status} {data}")
            return 1
        print("PASS: streaming /v1/responses rejected with 400")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
