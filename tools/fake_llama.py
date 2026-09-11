"""
A stand-in for llama-server, implementing just enough of its HTTP surface to
exercise this program end to end: health, props, tokenize, apply-template and
a streaming /completion.

It also models the behaviour the whole design depends on -- a per-slot prompt
cache -- so the caching logic can be tested for real.  The server remembers the
prompt each slot last saw, and reports only the divergent tail as evaluated,
exactly as llama.cpp does.  A test can therefore assert that turn five costs
the same prompt work as turn one.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text)


class State:
    """Shared server state: the slot cache and a canned reply."""

    def __init__(self) -> None:
        self.slots: Dict[int, str] = {}
        self.lock = threading.Lock()
        self.reply = ("Address 0x27 answered on the first attempt. "
                      "All four rows are live.")
        self.reasoning = ""
        self.delay = 0.01
        self.requests: List[dict] = []
        self.n_ctx = 4096

    def common_prefix(self, slot: int, prompt: str) -> int:
        """Tokens reusable from the slot's cache, as llama.cpp computes it."""
        with self.lock:
            cached = self.slots.get(slot, "")
        old, new = tokenize(cached), tokenize(prompt)
        count = 0
        for a, b in zip(old, new):
            if a != b:
                break
            count += 1
        return count

    def store(self, slot: int, prompt: str) -> None:
        with self.lock:
            self.slots[slot] = prompt


STATE = State()


def render_template(messages: List[dict], add_generation_prompt: bool = True) -> str:
    """A ChatML-shaped template, which is what most GGUF models carry."""
    parts = []
    for message in messages:
        parts.append(f"<|im_start|>{message.get('role','user')}\n"
                     f"{message.get('content','')}<|im_end|>\n")
    if add_generation_prompt:
        parts.append("<|im_start|>assistant\n")
    return "".join(parts)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:      # quiet
        pass

    # -- helpers ------------------------------------------------------------

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"status": "ok"})
        elif self.path == "/props":
            self._json({
                "default_generation_settings": {"n_ctx": STATE.n_ctx},
                "total_slots": 1,
                "model_path": "/models/fake-3b-q4_k_m.gguf",
                "chat_template": "chatml",
                "build_info": "fake",
            })
        elif self.path == "/slots":
            self._json([{"id": 0, "state": 0}])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        payload = self._body()
        STATE.requests.append({"path": self.path, "payload": payload})

        if self.path == "/tokenize":
            self._json({"tokens": tokenize(str(payload.get("content", "")))})
            return
        if self.path == "/apply-template":
            prompt = render_template(payload.get("messages") or [],
                                     payload.get("add_generation_prompt", True))
            self._json({"prompt": prompt})
            return
        if self.path in ("/completion", "/v1/chat/completions"):
            self._completion(payload)
            return
        self._json({"error": "not found"}, 404)

    def _completion(self, payload: dict) -> None:
        prompt = payload.get("prompt")
        if prompt is None:
            prompt = render_template(payload.get("messages") or [])
        slot = int(payload.get("id_slot", 0))
        cache_prompt = bool(payload.get("cache_prompt", False))

        total = len(tokenize(prompt))
        cached = STATE.common_prefix(slot, prompt) if cache_prompt else 0
        evaluated = max(0, total - cached)
        STATE.store(slot, prompt)

        n_predict = payload.get("n_predict", payload.get("max_tokens", 256))
        if n_predict == 0:
            # Prefill: ingest and stop, leaving the cache warm.
            self._json({"content": "", "stop": True, "tokens_evaluated": evaluated,
                        "tokens_cached": cached,
                        "timings": {"prompt_n": evaluated, "prompt_ms": evaluated * 2.0,
                                    "predicted_n": 0, "predicted_ms": 0.0}})
            return

        if not payload.get("stream"):
            self._json({"content": STATE.reply, "stop": True,
                        "tokens_evaluated": evaluated, "tokens_cached": cached,
                        "timings": {"prompt_n": evaluated,
                                    "prompt_ms": evaluated * 2.0,
                                    "predicted_n": len(tokenize(STATE.reply)),
                                    "predicted_ms": 500.0}})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        text = STATE.reply
        if STATE.reasoning:
            text = f"<think>{STATE.reasoning}</think>{text}"
        STATE.store(slot, prompt + text)

        try:
            for chunk in _chunks(text):
                self._send_event({"content": chunk, "stop": False})
                time.sleep(STATE.delay)
            self._send_event({
                "content": "", "stop": True, "stopped_eos": True,
                "tokens_evaluated": evaluated, "tokens_cached": cached,
                "timings": {"prompt_n": evaluated, "prompt_ms": evaluated * 2.0,
                            "predicted_n": len(tokenize(text)),
                            "predicted_ms": max(1.0, len(tokenize(text)) * 40.0)},
            })
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_event(self, payload: dict) -> None:
        body = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()


def _chunks(text: str, size: int = 4):
    """Emit the reply in small pieces, the way a real stream arrives."""
    for start in range(0, len(text), size):
        yield text[start:start + size]


def serve(port: int = 8080, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="fake-llama")
    thread.start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="fake llama.cpp server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = serve(args.port)
    print(f"fake llama.cpp listening on http://127.0.0.1:{args.port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
