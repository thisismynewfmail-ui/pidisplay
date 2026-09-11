"""
Streaming client for a llama.cpp server.

Written against the standard library on purpose.  This runs on a Raspberry Pi
that may have no compiler and no network at install time; every dependency is
one more thing that can turn a five-minute setup into an afternoon.

The important design decision is *which endpoint to talk to*.  The
OpenAI-compatible ``/v1/chat/completions`` route is convenient, but the native
``/completion`` route accepts ``cache_prompt`` and ``id_slot``, which is how
this program avoids re-reading the entire conversation on every turn.  So the
client prefers the native route -- rendering the chat template server-side via
``/apply-template`` so the model still sees exactly the format it expects --
and falls back to the OpenAI route only if the server is too old to offer
``/apply-template``.

How the prompt cache actually works, since the whole design depends on it:
llama-server keeps the evaluated KV cache in a *slot*.  When a request arrives
with ``cache_prompt`` set, the server compares the new prompt against the
tokens that slot already holds and only evaluates the divergent tail.  Because
a conversation grows by appending, the divergent tail is just the newest
message -- so turn twenty costs the same prompt-side work as turn two.  Pinning
every request to one slot id is what keeps that true; letting the server pick a
slot round-robin would scatter the conversation across slots and re-read the
history each time.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

USER_AGENT = "aperture-terminal/1.0"


class LlamaError(RuntimeError):
    """Any failure talking to the server, with a message fit for a 20x4 panel."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail or message


@dataclass
class Timings:
    """Per-request timing, as reported by the server."""

    prompt_tokens: int = 0          # tokens actually evaluated this request
    prompt_ms: float = 0.0
    predicted_tokens: int = 0
    predicted_ms: float = 0.0
    cached_tokens: int = 0          # tokens served from the KV cache
    truncated: bool = False

    @property
    def tokens_per_second(self) -> float:
        if self.predicted_ms <= 0:
            return 0.0
        return self.predicted_tokens * 1000.0 / self.predicted_ms

    @property
    def prompt_tokens_per_second(self) -> float:
        if self.prompt_ms <= 0:
            return 0.0
        return self.prompt_tokens * 1000.0 / self.prompt_ms

    @property
    def time_to_first_token(self) -> float:
        return self.prompt_ms / 1000.0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cached_tokens + self.prompt_tokens
        return (self.cached_tokens / total) if total else 0.0


@dataclass
class StreamEvent:
    """One item from a streaming completion."""

    kind: str                       # text | reasoning | done | error | meta
    text: str = ""
    timings: Optional[Timings] = None
    detail: str = ""
    stop_reason: str = ""


@dataclass
class ServerProps:
    """What the server says about itself."""

    n_ctx: int = 0
    n_slots: int = 1
    model_path: str = ""
    chat_template: str = ""
    build: str = ""
    supports_apply_template: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def model_name(self) -> str:
        import os
        return os.path.basename(self.model_path) if self.model_path else ""


class LlamaClient:
    """Talks to one llama.cpp server."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.props: Optional[ServerProps] = None
        self._template_supported: Optional[bool] = None
        self._lock = threading.Lock()

    # -- plumbing -----------------------------------------------------------

    def _request(self, path: str, payload: Optional[dict] = None,
                 method: Optional[str] = None, timeout: Optional[float] = None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            url, data=data, headers=headers,
            method=method or ("POST" if payload is not None else "GET"))
        return urllib.request.urlopen(
            request, timeout=self.timeout if timeout is None else timeout)

    def _json(self, path: str, payload: Optional[dict] = None,
              timeout: Optional[float] = None) -> Any:
        try:
            with self._request(path, payload, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            raise LlamaError(f"HTTP {exc.code}", detail) from exc
        except urllib.error.URLError as exc:
            raise LlamaError("NO ROUTE TO SERVER", str(exc.reason)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LlamaError("SERVER TIMED OUT", str(exc)) from exc
        except OSError as exc:
            raise LlamaError("CONNECTION FAILED", str(exc)) from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LlamaError("BAD RESPONSE", str(exc)) from exc

    # -- introspection ------------------------------------------------------

    def health(self, timeout: float = 3.0) -> bool:
        """True when the server is up and a model is loaded."""
        try:
            with self._request("/health", timeout=timeout) as response:
                if response.status != 200:
                    return False
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 503 is the documented "loading model" response.
            return False if exc.code == 503 else False
        except Exception:
            return False
        return str(body.get("status", "ok")).lower() in ("ok", "no slot available")

    def wait_until_ready(self, deadline: float,
                         on_tick: Optional[Callable[[float], bool]] = None,
                         interval: float = 0.35) -> bool:
        """Poll ``/health`` until ready or *deadline*.

        *on_tick* is called with elapsed seconds between polls and may return
        False to abort -- that is what lets the boot screen stay animated and
        cancellable while a multi-gigabyte model is being mapped in.
        """
        start = time.monotonic()
        while time.monotonic() < deadline:
            if self.health(timeout=2.0):
                return True
            if on_tick is not None and not on_tick(time.monotonic() - start):
                return False
            time.sleep(interval)
        return False

    def fetch_props(self) -> ServerProps:
        raw = self._json("/props", timeout=10.0)
        defaults = raw.get("default_generation_settings") or {}
        props = ServerProps(
            n_ctx=int(defaults.get("n_ctx") or raw.get("n_ctx") or 0),
            n_slots=int(raw.get("total_slots") or 1),
            model_path=raw.get("model_path") or defaults.get("model") or "",
            chat_template=raw.get("chat_template") or "",
            build=str(raw.get("build_info") or ""),
            raw=raw,
        )
        self.props = props
        return props

    def slots(self) -> List[dict]:
        """Slot state, when the server exposes it (``--slots``)."""
        try:
            data = self._json("/slots", timeout=5.0)
        except LlamaError:
            return []
        return data if isinstance(data, list) else []

    def tokenize(self, text: str) -> int:
        """Token count for *text*.  Falls back to an estimate on failure."""
        if not text:
            return 0
        try:
            data = self._json("/tokenize", {"content": text}, timeout=30.0)
        except LlamaError:
            return estimate_tokens(text)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return len(tokens) if isinstance(tokens, list) else estimate_tokens(text)

    def apply_template(self, messages: List[dict],
                       add_generation_prompt: bool = True) -> Optional[str]:
        """Render *messages* with the model's own chat template, server-side.

        Rendering server-side rather than guessing the format locally is the
        whole reason this works with any GGUF: the template travels inside the
        model file, and the server already knows how to apply it.
        """
        if self._template_supported is False:
            return None
        payload = {"messages": messages}
        if not add_generation_prompt:
            payload["add_generation_prompt"] = False
        try:
            data = self._json("/apply-template", payload, timeout=30.0)
        except LlamaError as exc:
            if "404" in exc.message or "501" in exc.message:
                self._template_supported = False
                return None
            raise
        self._template_supported = True
        prompt = data.get("prompt") if isinstance(data, dict) else None
        return prompt if isinstance(prompt, str) else None

    @property
    def native_templating(self) -> bool:
        return self._template_supported is not False

    # -- generation ---------------------------------------------------------

    def stream_completion(self, prompt: str, params: Dict[str, Any],
                          slot_id: int = 0,
                          cancel: Optional[threading.Event] = None
                          ) -> Iterator[StreamEvent]:
        """Stream ``/completion``, reusing the KV cache in *slot_id*."""
        payload = dict(params)
        payload.update({
            "prompt": prompt,
            "stream": True,
            # The two flags this entire program is built around.
            "cache_prompt": True,
            "id_slot": slot_id,
        })
        yield from self._stream("/completion", payload, cancel, _parse_native)

    def stream_chat(self, messages: List[dict], params: Dict[str, Any],
                    cancel: Optional[threading.Event] = None
                    ) -> Iterator[StreamEvent]:
        """Fallback path for servers without ``/apply-template``."""
        payload = dict(params)
        payload.update({
            "messages": messages,
            "stream": True,
            "cache_prompt": True,
        })
        payload.pop("n_predict", None)
        yield from self._stream("/v1/chat/completions", payload, cancel,
                                _parse_openai)

    def prefill(self, prompt: str, slot_id: int = 0,
                timeout: float = 300.0) -> Optional[Timings]:
        """Ingest *prompt* into the slot's KV cache without generating.

        Used to hide prompt-evaluation latency behind the operator's typing.
        ``n_predict: 0`` makes the server do the prefix work and stop, leaving
        the cache warm for the request that follows.
        """
        payload = {
            "prompt": prompt,
            "n_predict": 0,
            "stream": False,
            "cache_prompt": True,
            "id_slot": slot_id,
        }
        try:
            data = self._json("/completion", payload, timeout=timeout)
        except LlamaError:
            return None
        return _timings_from(data) if isinstance(data, dict) else None

    def _stream(self, path: str, payload: dict,
                cancel: Optional[threading.Event],
                parser: Callable[[dict], List[StreamEvent]]
                ) -> Iterator[StreamEvent]:
        url = f"{self.base_url}{path}"
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")

        response = None
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            yield StreamEvent("error", detail=_read_error(exc),
                              text=f"HTTP {exc.code}")
            return
        except urllib.error.URLError as exc:
            yield StreamEvent("error", text="NO ROUTE TO SERVER",
                              detail=str(exc.reason))
            return
        except OSError as exc:
            yield StreamEvent("error", text="CONNECTION FAILED", detail=str(exc))
            return

        # Closing the response from the cancelling thread is what actually
        # interrupts a blocked read; checking a flag between lines only helps
        # while tokens are flowing.
        closed = threading.Event()
        watcher = None
        if cancel is not None:
            def _watch() -> None:
                while not closed.wait(0.05):
                    if cancel.is_set():
                        try:
                            response.close()
                        except Exception:
                            pass
                        return
            watcher = threading.Thread(target=_watch, daemon=True,
                                       name="stream-cancel")
            watcher.start()

        try:
            for raw_line in response:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("done", stop_reason="aborted")
                    return
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    yield StreamEvent("done", stop_reason="eos")
                    return
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    error = chunk["error"]
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    yield StreamEvent("error", text="SERVER ERROR",
                                      detail=str(message))
                    return
                for event in parser(chunk):
                    yield event
                    if event.kind == "done":
                        return
        except (socket.timeout, TimeoutError):
            yield StreamEvent("error", text="STREAM TIMED OUT",
                              detail="no data within the configured timeout")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if cancel is not None and cancel.is_set():
                yield StreamEvent("done", stop_reason="aborted")
            else:
                yield StreamEvent("error", text="STREAM INTERRUPTED",
                                  detail=str(exc))
        finally:
            closed.set()
            if watcher is not None:
                watcher.join(timeout=0.2)
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:200]
    error = parsed.get("error", parsed)
    if isinstance(error, dict):
        return str(error.get("message", error))[:200]
    return str(error)[:200]


def _timings_from(chunk: dict) -> Timings:
    timings = chunk.get("timings") or {}
    # ``tokens_evaluated`` counts the prompt tokens this request had to run
    # through the model.  Subtracting it from the full prompt length is how the
    # UI reports cache effectiveness.
    prompt_n = int(timings.get("prompt_n", chunk.get("tokens_evaluated", 0)) or 0)
    cached = int(chunk.get("tokens_cached", 0) or 0)
    if not cached:
        prompt_total = int(chunk.get("prompt_n_total", 0) or 0)
        if prompt_total > prompt_n:
            cached = prompt_total - prompt_n
    return Timings(
        prompt_tokens=prompt_n,
        prompt_ms=float(timings.get("prompt_ms", 0.0) or 0.0),
        predicted_tokens=int(timings.get("predicted_n",
                                         chunk.get("tokens_predicted", 0)) or 0),
        predicted_ms=float(timings.get("predicted_ms", 0.0) or 0.0),
        cached_tokens=cached,
        truncated=bool(chunk.get("truncated", False)),
    )


def _parse_native(chunk: dict) -> List[StreamEvent]:
    events: List[StreamEvent] = []
    reasoning = chunk.get("reasoning_content")
    if reasoning:
        events.append(StreamEvent("reasoning", text=str(reasoning)))
    content = chunk.get("content")
    if content:
        events.append(StreamEvent("text", text=str(content)))
    if chunk.get("stop"):
        reason = "eos"
        if chunk.get("stopped_word"):
            reason = "stop-word"
        elif chunk.get("stopped_limit"):
            reason = "length"
        elif chunk.get("truncated"):
            reason = "truncated"
        events.append(StreamEvent("done", timings=_timings_from(chunk),
                                  stop_reason=reason))
    return events


def _parse_openai(chunk: dict) -> List[StreamEvent]:
    events: List[StreamEvent] = []
    choices = chunk.get("choices") or []
    if not choices:
        if chunk.get("timings"):
            events.append(StreamEvent("done", timings=_timings_from(chunk)))
        return events
    choice = choices[0]
    delta = choice.get("delta") or choice.get("message") or {}
    reasoning = delta.get("reasoning_content")
    if reasoning:
        events.append(StreamEvent("reasoning", text=str(reasoning)))
    content = delta.get("content")
    if content:
        events.append(StreamEvent("text", text=str(content)))
    finish = choice.get("finish_reason")
    if finish:
        timings = _timings_from(chunk) if chunk.get("timings") else Timings()
        usage = chunk.get("usage") or {}
        if usage and not timings.predicted_tokens:
            timings.predicted_tokens = int(usage.get("completion_tokens", 0) or 0)
            timings.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        events.append(StreamEvent("done", timings=timings, stop_reason=str(finish)))
    return events


def estimate_tokens(text: str) -> int:
    """Rough token count for when the server cannot be asked.

    Used only for gauges before the first successful ``/tokenize``; roughly
    four characters per token holds well enough for English prose.
    """
    return max(1, (len(text) + 3) // 4)
