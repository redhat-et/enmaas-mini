#!/usr/bin/env python3
"""Minimal shim: POST /v1/responses -> POST /v1/chat/completions.

ONLY for laptop dev with llm-katan when a harness insists on /v1/responses.
Not started by default; see shims/README.md.  Translates the Responses API
to chat/completions with best-effort fidelity and returns a Responses-shaped
envelope so that gateway routing does not need to be reconfigured.

Usage:
  python3 scripts/laptop/shims/responses_shim.py \
    --listen 0.0.0.0:18090 --upstream http://pricetag-mock:8000

The compose template wires 18090 as an extra endpoint in the openai filter
chain when ENABLE_RESPONSES_SHIM=1.  Health: GET /health -> 200.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def responses_to_chat(body: dict) -> dict:
    """Map a Responses request to a chat/completions request."""
    model = body.get("model", "fixture")
    instruction = body.get("instructions") or body.get("instruction") or ""
    items = body.get("input", [])
    # Responses `input` may be string, list of messages, or list of content parts
    if isinstance(items, str):
        messages = []
        if instruction:
            messages.append({"role": "system", "content": instruction})
        messages.append({"role": "user", "content": items})
    elif isinstance(items, list):
        messages = []
        if instruction:
            messages.append({"role": "system", "content": instruction})
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            # Responses items may have {"type":"message","content":[{"type":"input_text","text":"..."}]}
            content = item.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
                content = text
            elif not isinstance(content, str):
                content = str(content)
            # Skip function_call_output etc. for shim purposes
            if item.get("type") == "function_call_output":
                content = item.get("output", content)
                role = "tool"
            messages.append({"role": role, "content": content})
        if not messages:
            messages = [{"role": "user", "content": "probe"}]
    else:
        messages = [{"role": "user", "content": str(items)}]

    chat: dict = {"model": model, "messages": messages, "max_tokens": body.get("max_output_tokens", 32)}
    if body.get("stream"):
        chat["stream"] = True
        chat["stream_options"] = {"include_usage": True}
    # Preserve tools if present
    if body.get("tools"):
        chat["tools"] = body["tools"]
    return chat


def chat_to_responses(chat_result: dict, request_model: str) -> dict:
    """Wrap a chat/completions result in a Responses envelope."""
    choice = (chat_result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    usage = chat_result.get("usage") or {}
    if tool_calls:
        tc = tool_calls[0]
        fn = tc.get("function") or {}
        output = [
            {
                "id": tc.get("id", "fc_fixture"),
                "type": "function_call",
                "call_id": tc.get("id", "call_fixture"),
                "name": fn.get("name", "add"),
                "arguments": fn.get("arguments", '{"a":2,"b":3}'),
                "status": "completed",
            }
        ]
    else:
        output = [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text or "mock answer", "annotations": []}],
            }
        ]
    return {
        "id": chat_result.get("id", "resp_shim"),
        "object": "response",
        "created_at": chat_result.get("created", 1),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": request_model,
        "output": output,
        "parallel_tool_calls": False,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 2),
            "output_tokens": usage.get("completion_tokens", 3),
            "total_tokens": usage.get("total_tokens", 5),
        },
    }


class Handler(BaseHTTPRequestHandler):
    upstream: str = "http://pricetag-mock:8000"

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write(f"[responses-shim] {fmt % args}\n")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            body = json.dumps({"status": "ok", "shim": "responses"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v1/models":
            self._proxy("GET", path, None)
            return
        self.send_error(404, "unsupported path for shim")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return

        if path == "/v1/responses":
            chat_body = responses_to_chat(body)
            upstream_body = self._proxy_json("POST", "/v1/chat/completions", chat_body)
            if upstream_body is None:
                return
            # Handle streaming: passthrough with minimal translation would be more work;
            # for now return non-streaming response even if stream was requested.
            result = json.loads(upstream_body)
            shaped = chat_to_responses(result, body.get("model", "fixture"))
            data = json.dumps(shaped).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # Passthrough for any other POST
        self._proxy("POST", path, raw)

    def _proxy(self, method: str, path: str, data: bytes | None):
        url = self.upstream.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        # Forward auth
        for h in ("Authorization", "X-Api-Key", "x-api-key"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                self.send_response(resp.status)
                ctype = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, f"upstream error: {e}")

    def _proxy_json(self, method: str, path: str, body: dict) -> bytes | None:
        url = self.upstream.rstrip("/") + path
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        for h in ("Authorization", "X-Api-Key", "x-api-key"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return None
        except Exception as e:
            self.send_error(502, f"upstream error: {e}")
            return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--listen", default="0.0.0.0:18090", help="host:port to listen on")
    p.add_argument("--upstream", default="http://pricetag-mock:8000", help="llm-katan base URL")
    args = p.parse_args()
    host, port_s = args.listen.rsplit(":", 1)
    port = int(port_s)
    Handler.upstream = args.upstream
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"responses-shim listening on {host}:{port} -> upstream {args.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    main()
