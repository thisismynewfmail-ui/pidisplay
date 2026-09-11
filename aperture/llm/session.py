"""
Conversation state and the worker that drives generation.

Responsibilities split three ways:

  * :class:`Conversation` is the record -- an ordered list of turns plus the
    system prompt.  It knows nothing about networks or threads.

  * :class:`ReasoningSplitter` separates a reasoning model's internal notes
    from its actual answer.  Some servers deliver these as a separate field;
    others inline them in ``<think>`` tags that can be split across streaming
    chunks.  Both are handled here so nothing above this layer has to care.

  * :class:`ChatEngine` runs generation on a worker thread and reports progress
    through a queue.  The UI thread never blocks on the network: it drains the
    queue each frame, which is what keeps the animations running while a reply
    is being produced.

Context management is the subtle part.  The KV cache is only reused while the
prompt's *prefix* is unchanged, so dropping old turns to make room invalidates
everything and forces a full re-read.  The policy here is therefore to trim
rarely and deeply rather than often and shallowly: when the window fills, drop
back to roughly sixty percent occupancy in one go.  Paying that cost once every
several turns is far cheaper than paying a smaller version of it every turn,
and the operator is told when it happens rather than silently losing history.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .client import LlamaClient, LlamaError, Timings, estimate_tokens

# Engine states, in the order a turn passes through them.
IDLE = "IDLE"
PREPARING = "PREPARING"
WAITING = "WAITING"        # prompt is being evaluated; no output yet
REASONING = "REASONING"    # model is emitting internal notes
STREAMING = "STREAMING"
ABORTING = "ABORTING"
ERROR = "ERROR"

USER = "user"
ASSISTANT = "assistant"
SYSTEM = "system"
NOTE = "note"              # UI-only: never sent to the model


@dataclass
class Turn:
    """One entry in the transcript."""

    role: str
    text: str = ""
    reasoning: str = ""
    timings: Optional[Timings] = None
    timestamp: float = field(default_factory=time.time)
    aborted: bool = False
    error: bool = False

    @property
    def visible(self) -> bool:
        return bool(self.text.strip())

    def as_message(self) -> Optional[Dict[str, str]]:
        """The form sent to the model, or None if this turn is display-only."""
        if self.role in (USER, ASSISTANT) and self.text.strip():
            return {"role": self.role, "content": self.text.strip()}
        return None


class Conversation:
    """The transcript, plus the system prompt that precedes it."""

    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt
        self.turns: List[Turn] = []
        self.dropped_turns = 0
        self.started_at = time.time()

    def add(self, turn: Turn) -> Turn:
        self.turns.append(turn)
        return turn

    def clear(self) -> None:
        self.turns.clear()
        self.dropped_turns = 0
        self.started_at = time.time()

    def messages(self, extra: Optional[List[Dict[str, str]]] = None
                 ) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if self.system_prompt.strip():
            out.append({"role": SYSTEM, "content": self.system_prompt.strip()})
        for turn in self.turns:
            message = turn.as_message()
            if message:
                out.append(message)
        if extra:
            out.extend(extra)
        return out

    @property
    def exchanges(self) -> int:
        return sum(1 for t in self.turns if t.role == USER)

    def last_user_index(self) -> int:
        for index in range(len(self.turns) - 1, -1, -1):
            if self.turns[index].role == USER:
                return index
        return -1

    def drop_oldest_exchange(self) -> bool:
        """Remove the oldest user turn and everything up to the next one."""
        first_user = next((i for i, t in enumerate(self.turns)
                           if t.role == USER), None)
        if first_user is None:
            return False
        end = len(self.turns)
        for index in range(first_user + 1, len(self.turns)):
            if self.turns[index].role == USER:
                end = index
                break
        del self.turns[first_user:end]
        self.dropped_turns += 1
        return True

    def transcript_text(self) -> str:
        lines = []
        for turn in self.turns:
            if turn.role == USER:
                lines.append(f"> {turn.text}")
            elif turn.role == ASSISTANT:
                lines.append(turn.text)
            elif turn.role == NOTE:
                lines.append(f"-- {turn.text}")
            lines.append("")
        return "\n".join(lines)


class ReasoningSplitter:
    """Routes streamed text into answer and reasoning channels.

    Reasoning models mark their internal notes with ``<think>`` ... ``</think>``.
    Those tags arrive one streaming chunk at a time and a chunk boundary can
    fall in the middle of a tag, so the splitter holds back any trailing text
    that could still turn out to be the start of one.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.in_reasoning = False
        self._buffer = ""

    def feed(self, chunk: str) -> Tuple[str, str]:
        """Return ``(answer_text, reasoning_text)`` for this chunk."""
        self._buffer += chunk
        answer: List[str] = []
        reasoning: List[str] = []

        while self._buffer:
            tag = self.CLOSE if self.in_reasoning else self.OPEN
            index = self._buffer.find(tag)
            if index >= 0:
                head, self._buffer = self._buffer[:index], self._buffer[index + len(tag):]
                (reasoning if self.in_reasoning else answer).append(head)
                self.in_reasoning = not self.in_reasoning
                continue

            # No complete tag.  Hold back only as much as could still become
            # one, and release the rest so output keeps flowing.
            hold = self._partial_tag_length(self._buffer, tag)
            if hold:
                release, self._buffer = self._buffer[:-hold], self._buffer[-hold:]
            else:
                release, self._buffer = self._buffer, ""
            if release:
                (reasoning if self.in_reasoning else answer).append(release)
            break

        return "".join(answer), "".join(reasoning)

    @staticmethod
    def _partial_tag_length(text: str, tag: str) -> int:
        limit = min(len(tag) - 1, len(text))
        for length in range(limit, 0, -1):
            if text.endswith(tag[:length]):
                return length
        return 0

    def flush(self) -> Tuple[str, str]:
        """Release anything still held back at end of stream."""
        pending, self._buffer = self._buffer, ""
        if not pending:
            return "", ""
        return ("", pending) if self.in_reasoning else (pending, "")


@dataclass
class EngineEvent:
    """A message from the worker thread to the UI."""

    kind: str            # state | delta | reasoning | done | error | note | ctx
    text: str = ""
    detail: str = ""
    state: str = ""
    timings: Optional[Timings] = None
    used: int = 0
    total: int = 0


@dataclass
class ContextUsage:
    """What the context gauge draws."""

    prompt_tokens: int = 0
    generated_tokens: int = 0
    window: int = 0
    reserve: int = 0

    @property
    def used(self) -> int:
        return self.prompt_tokens + self.generated_tokens

    @property
    def budget(self) -> int:
        return max(1, self.window - self.reserve)

    @property
    def fraction(self) -> float:
        return min(1.0, self.used / float(self.budget)) if self.budget else 0.0

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))


class ChatEngine:
    """Drives one conversation against one llama.cpp server."""

    #: Occupancy to trim back to when the window fills.  See module docstring.
    TRIM_TARGET = 0.60
    #: Pause in typing before a speculative prefill is worth starting.
    TYPE_PREFILL_IDLE = 0.9

    def __init__(self, client: LlamaClient, conversation: Conversation,
                 slot_id: int = 0):
        self.client = client
        self.conversation = conversation
        self.slot_id = slot_id
        self.events: "queue.Queue[EngineEvent]" = queue.Queue()

        self.state = IDLE
        self.last_error = ""
        self.last_detail = ""
        self.last_timings: Optional[Timings] = None
        self.usage = ContextUsage()
        self.pending: Optional[Turn] = None

        self.params: Dict[str, Any] = {}
        self.window = 4096
        self.reserve = 640
        #: Whether to warm the KV cache after each reply. See schedule_prefill.
        self.prefill_enabled = True

        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._prefill_thread: Optional[threading.Thread] = None
        self._prefilled_prompt = ""
        self._last_prompt = ""
        self._stream_started = 0.0
        self._first_token_at = 0.0

    # -- status -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.state in (PREPARING, WAITING, REASONING, STREAMING, ABORTING)

    @property
    def elapsed(self) -> float:
        return (time.monotonic() - self._stream_started) if self._stream_started else 0.0

    @property
    def time_to_first_token(self) -> float:
        if not self._first_token_at or not self._stream_started:
            return 0.0
        return self._first_token_at - self._stream_started

    def live_tokens_per_second(self) -> float:
        """Throughput measured from first token to now, for the live readout."""
        with self._lock:
            pending = self.pending
            text = (pending.text + pending.reasoning) if pending else ""
        if not self._first_token_at or not text:
            return 0.0
        elapsed = time.monotonic() - self._first_token_at
        if elapsed < 0.25:
            return 0.0
        return estimate_tokens(text) / elapsed

    def pending_text(self) -> Tuple[str, str]:
        with self._lock:
            if self.pending is None:
                return "", ""
            return self.pending.text, self.pending.reasoning

    # -- lifecycle ----------------------------------------------------------

    def configure(self, window: int, reserve: int, params: Dict[str, Any],
                  prefill: bool = True) -> None:
        self.prefill_enabled = bool(prefill)
        self.window = max(512, int(window))
        self.reserve = max(64, min(int(reserve), self.window // 2))
        self.params = dict(params)
        self.usage.window = self.window
        self.usage.reserve = self.reserve

    def _emit(self, event: EngineEvent) -> None:
        self.events.put(event)

    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit(EngineEvent("state", state=state))

    def drain(self, limit: int = 256) -> List[EngineEvent]:
        out: List[EngineEvent] = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # -- generation ---------------------------------------------------------

    def send(self, text: str) -> bool:
        """Queue *text* as a user turn and begin generating."""
        text = text.strip()
        if not text or self.busy:
            return False
        self.conversation.add(Turn(role=USER, text=text))
        return self._start()

    def regenerate(self) -> bool:
        """Discard the last reply and produce another for the same prompt."""
        if self.busy:
            return False
        turns = self.conversation.turns
        while turns and turns[-1].role != USER:
            turns.pop()
        if not turns:
            return False
        return self._start(reseed=True)

    def retry_after_error(self) -> bool:
        return self.regenerate()

    def _start(self, reseed: bool = False) -> bool:
        self._cancel.clear()
        self.last_error = ""
        self.last_detail = ""
        with self._lock:
            self.pending = Turn(role=ASSISTANT)
        self._stream_started = time.monotonic()
        self._first_token_at = 0.0
        self._set_state(PREPARING)
        self._worker = threading.Thread(target=self._run, args=(reseed,),
                                        daemon=True, name="inference")
        self._worker.start()
        return True

    def abort(self) -> None:
        if not self.busy:
            return
        self._cancel.set()
        self._set_state(ABORTING)

    def wait(self, timeout: float = 5.0) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)

    # -- worker -------------------------------------------------------------

    def _run(self, reseed: bool) -> None:
        try:
            prompt, messages = self._prepare_prompt()
        except LlamaError as exc:
            self._fail(exc.message, exc.detail)
            return
        except Exception as exc:                      # pragma: no cover
            self._fail("PROMPT BUILD FAILED", str(exc))
            return

        if self._cancel.is_set():
            self._finish(aborted=True)
            return

        params = dict(self.params)
        if reseed:
            params["seed"] = -1
        self._set_state(WAITING)

        splitter = ReasoningSplitter()
        got_output = False
        timings: Optional[Timings] = None
        stop_reason = ""

        try:
            if prompt is not None:
                stream = self.client.stream_completion(
                    prompt, params, slot_id=self.slot_id, cancel=self._cancel)
            else:
                stream = self.client.stream_chat(
                    messages, self._openai_params(params), cancel=self._cancel)

            for event in stream:
                if event.kind == "text":
                    answer, reasoning = splitter.feed(event.text)
                    got_output = self._absorb(answer, reasoning) or got_output
                elif event.kind == "reasoning":
                    got_output = self._absorb("", event.text) or got_output
                elif event.kind == "error":
                    self._fail(event.text or "ENGINE ERROR", event.detail)
                    return
                elif event.kind == "done":
                    timings = event.timings
                    stop_reason = event.stop_reason
                    break
        except LlamaError as exc:
            self._fail(exc.message, exc.detail)
            return
        except Exception as exc:                      # pragma: no cover
            self._fail("STREAM FAILED", str(exc))
            return

        answer, reasoning = splitter.flush()
        self._absorb(answer, reasoning)

        aborted = self._cancel.is_set() or stop_reason == "aborted"
        if not got_output and not aborted:
            self._absorb("[no output produced]", "")
        self._finish(aborted=aborted, timings=timings, stop_reason=stop_reason)

    def _absorb(self, answer: str, reasoning: str) -> bool:
        """Append streamed text to the pending turn and notify the UI."""
        if not answer and not reasoning:
            return False
        if not self._first_token_at:
            self._first_token_at = time.monotonic()
        with self._lock:
            if self.pending is None:
                return False
            if answer:
                self.pending.text += answer
            if reasoning:
                self.pending.reasoning += reasoning
        if reasoning and not answer:
            if self.state != REASONING:
                self._set_state(REASONING)
            self._emit(EngineEvent("reasoning", text=reasoning))
        if answer:
            if self.state != STREAMING:
                self._set_state(STREAMING)
            self._emit(EngineEvent("delta", text=answer))
        return True

    def _finish(self, aborted: bool = False, timings: Optional[Timings] = None,
                stop_reason: str = "") -> None:
        with self._lock:
            turn = self.pending
            self.pending = None
        if turn is not None:
            turn.aborted = aborted
            turn.timings = timings
            if turn.text.strip() or turn.reasoning.strip():
                self.conversation.add(turn)
        self.last_timings = timings
        if timings is not None:
            self.usage.generated_tokens += timings.predicted_tokens
        self._set_state(IDLE)
        self._emit(EngineEvent("done", timings=timings, detail=stop_reason,
                               text="aborted" if aborted else stop_reason))
        if not aborted and self.prefill_enabled:
            self.schedule_prefill()

    def _fail(self, message: str, detail: str = "") -> None:
        with self._lock:
            turn = self.pending
            self.pending = None
        # A partial reply is still worth keeping: losing half an answer to a
        # dropped connection helps nobody.
        if turn is not None and turn.text.strip():
            turn.aborted = True
            self.conversation.add(turn)
        self.last_error = message
        self.last_detail = detail
        self.state = ERROR
        self._emit(EngineEvent("error", text=message, detail=detail))

    # -- prompt construction ------------------------------------------------

    def _prepare_prompt(self) -> Tuple[Optional[str], List[Dict[str, str]]]:
        """Render the conversation, trimming it first if it will not fit.

        Returns ``(prompt, messages)``.  ``prompt`` is None when the server
        cannot render templates and the OpenAI-compatible route must be used.
        """
        for attempt in range(12):
            messages = self.conversation.messages()
            prompt = self.client.apply_template(messages)
            if prompt is None:
                # No server-side templating: fall back to message-based chat and
                # let the server handle its own window.
                self.usage.prompt_tokens = sum(
                    estimate_tokens(m["content"]) for m in messages)
                self._emit(EngineEvent("ctx", used=self.usage.used,
                                       total=self.usage.budget))
                return None, messages

            tokens = self.client.tokenize(prompt)
            budget = max(1, self.window - self.reserve)
            if tokens <= budget or attempt >= 11:
                self.usage.prompt_tokens = tokens
                self.usage.generated_tokens = 0
                self._last_prompt = prompt
                self._emit(EngineEvent("ctx", used=tokens, total=budget))
                return prompt, messages

            if not self._trim_to(budget):
                # Nothing left to drop: the single newest exchange is itself
                # bigger than the window.  Send it and let the server truncate,
                # having told the operator what is about to happen.
                self._emit(EngineEvent(
                    "note", text="MESSAGE EXCEEDS CONTEXT WINDOW"))
                self.usage.prompt_tokens = tokens
                return prompt, messages
        return None, self.conversation.messages()

    def _trim_to(self, budget: int) -> bool:
        """Drop the oldest exchanges until well under *budget*.

        Returns False when there is nothing further that can be dropped.
        """
        target = int(budget * self.TRIM_TARGET)
        dropped = 0
        while True:
            # Never drop the exchange currently being answered.
            if self.conversation.exchanges <= 1:
                break
            if not self.conversation.drop_oldest_exchange():
                break
            dropped += 1
            prompt = self.client.apply_template(self.conversation.messages())
            if prompt is None:
                break
            if self.client.tokenize(prompt) <= target:
                break

        if dropped:
            word = "EXCHANGE" if dropped == 1 else "EXCHANGES"
            self._emit(EngineEvent(
                "note", text=f"CONTEXT FULL: DROPPED {dropped} OLDEST {word}"))
            # The prefix changed, so the cached prefix is gone.  Say so on the
            # diagnostics screen rather than letting the next turn look slow
            # for no visible reason.
            self._prefilled_prompt = ""
            return True
        return False

    def _openai_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Translate native parameter names for the OpenAI-compatible route."""
        out = dict(params)
        if "n_predict" in out:
            out["max_tokens"] = out.pop("n_predict")
        out.pop("cache_prompt", None)
        return out

    # -- prompt cache priming ----------------------------------------------

    def schedule_prefill(self, partial_input: str = "") -> None:
        """Warm the KV cache in the background.

        Two occasions make this worthwhile.  After a reply, the cache holds the
        conversation but not the handful of template tokens that open the next
        user turn -- ingesting those now removes them from the next reply's
        critical path.  And while the operator is typing, the text so far is a
        prefix of what they will eventually send, so evaluating it early means
        the send costs only the characters typed since.

        Fire-and-forget by design: if it fails, or the operator sends before it
        finishes, the result is a normal turn with no prefill benefit.
        """
        if self.busy or not self.client.native_templating:
            return
        if self._prefill_thread is not None and self._prefill_thread.is_alive():
            return

        extra = [{"role": USER, "content": partial_input}] if partial_input else None
        try:
            messages = self.conversation.messages(extra)
        except Exception:
            return

        def _work() -> None:
            try:
                prompt = self.client.apply_template(messages)
                if not prompt or prompt == self._prefilled_prompt:
                    return
                if self.busy:
                    return
                self.client.prefill(prompt, slot_id=self.slot_id)
                self._prefilled_prompt = prompt
            except Exception:
                pass

        self._prefill_thread = threading.Thread(target=_work, daemon=True,
                                                name="prefill")
        self._prefill_thread.start()

    @property
    def cache_primed(self) -> bool:
        return bool(self._prefilled_prompt)

    # -- housekeeping -------------------------------------------------------

    def reset(self, system_prompt: Optional[str] = None) -> None:
        """Start a fresh conversation."""
        self.abort()
        self.wait(timeout=2.0)
        if system_prompt is not None:
            self.conversation.system_prompt = system_prompt
        self.conversation.clear()
        self.usage.prompt_tokens = 0
        self.usage.generated_tokens = 0
        self.last_error = ""
        self.last_timings = None
        self._prefilled_prompt = ""
        self.state = IDLE
        self.schedule_prefill()

    def export(self, directory: str) -> str:
        """Write the transcript to a timestamped file.  Returns the path.

        Two exports in the same second would otherwise land on the same name
        and the second would destroy the first, which is a poor result for a
        key whose whole purpose is not losing the conversation.
        """
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("transcript-%Y%m%d-%H%M%S")
        path = os.path.join(directory, f"{stamp}.txt")
        suffix = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"{stamp}-{suffix}.txt")
            suffix += 1
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# session started {time.ctime(self.conversation.started_at)}\n")
            handle.write(f"# exchanges {self.conversation.exchanges}\n\n")
            handle.write(self.conversation.transcript_text())
        return path
