"""
The chat view.

Layout, and why it is this way.  There are eighty character cells in total and
they have to carry a status readout, a scrollable transcript, a scrollbar, a
speaker indication and a text editor.  The allocation is:

    row 0   status: state glyph, a context-sensitive readout, context gauge
    rows1-3 transcript viewport
    col 0   speaker rail (who is talking, and where their turn began)
    col 19  proportional scrollbar, present only when there is more to see

leaving eighteen columns of text per row.

The transcript and the editor share row 3 rather than each owning one
permanently.  Three rows of transcript is fifty percent more reading area than
two, and reading is what the operator does most of; but the instant they type a
character, row 3 becomes the editor and stays there until the line is sent or
abandoned.  Nothing is hidden by this -- the editor only ever covers the oldest
of the three visible rows, and scrolling still reaches everything.

Speakers are distinguished by the left rail rather than by "YOU:" and "BOT:"
header lines.  A header line costs an entire row out of three; the rail costs
one column out of twenty and is legible at a glance.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Sequence, Tuple

from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent
from ..hal import keys as K
from ..llm import session as S
from ..llm.session import EngineEvent
from .screen import Screen
from .widgets import Activity, TextField, Ticker, draw_scrollbar
from . import text as T

#: Rail markers.  Plain characters where a plain character reads better than a
#: custom glyph; CGRAM slots where it does not.
RAIL_USER_HEAD = ">"
RAIL_NOTE = "-"
RAIL_ERROR = "!"
RAIL_REASONING = "~"

READ = "READ"
COMPOSE = "COMPOSE"


def _rate_text(rate: float) -> str:
    """Throughput, in at most ten columns.

    Fast hosts push past 100 tok/s, at which point the decimal is both
    meaningless and too wide, so it is dropped rather than truncating the unit.
    """
    if rate >= 100:
        return f"{rate:.0f} tok/s"
    return f"{rate:.1f} tok/s"


class ChatScreen(Screen):
    """Transcript, streaming output and the compose line."""

    bank = G.BANK_CHAT
    title = "TERMINAL"

    #: Transcript rows when the editor is hidden / shown.
    ROWS_READ = 3
    ROWS_COMPOSE = 2

    def __init__(self, app):
        super().__init__(app)
        self.field = TextField()
        self.activity = Activity(fps=8.0)
        self.ticker = Ticker(interval=3.0)
        self.mode = COMPOSE          # a fresh terminal shows the prompt

        self.text_width = self.display.cols - 2      # rail + scrollbar gutter
        self.lines: List[Tuple[str, str]] = []       # (rail, text), committed
        self._folded = 0                             # turns already wrapped
        self._pending: Optional[T.StreamWrapper] = None
        self._pending_reasoning: Optional[T.StreamWrapper] = None

        self.scroll = 0
        self.follow = True
        self._last_total = 0
        self._type_pause = 0.0
        self._prefill_sent = True
        self._last_status = ""
        self._log_path = ""
        self._logging_failed = False

    # -- lifecycle ---------------------------------------------------------

    def on_enter(self) -> None:
        self.activity.reset()

    def on_reveal(self) -> None:
        self.app.display.invalidate()

    @property
    def view_rows(self) -> int:
        return self.ROWS_COMPOSE if self.mode == COMPOSE else self.ROWS_READ

    # -- transcript assembly ------------------------------------------------

    def _fold_new_turns(self) -> None:
        """Wrap any conversation turns that are not yet in the line buffer."""
        turns = self.app.conversation.turns
        show_reasoning = bool(self.app.config.get("chat.show_reasoning", False))
        while self._folded < len(turns):
            turn = turns[self._folded]
            self._folded += 1
            self.lines.extend(self._wrap_turn(turn, show_reasoning))

    def _wrap_turn(self, turn: S.Turn, show_reasoning: bool
                   ) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        if turn.role == S.USER:
            head, tail = RAIL_USER_HEAD, self.g("rail_user")
            body = turn.text
        elif turn.role == S.NOTE:
            head = tail = (RAIL_ERROR if turn.error else RAIL_NOTE)
            body = turn.text.upper()
        else:
            head = tail = self.g("rail")
            if show_reasoning and turn.reasoning.strip():
                for index, line in enumerate(T.wrap(turn.reasoning.strip(),
                                                    self.text_width)):
                    out.append((RAIL_REASONING, line))
            body = turn.text
            if turn.aborted and body.strip():
                body = body.rstrip() + " [stopped]"

        if not body.strip():
            return out
        for index, line in enumerate(T.wrap(body.strip(), self.text_width)):
            out.append((head if index == 0 else tail, line))
        return out

    def _pending_lines(self) -> List[Tuple[str, str]]:
        """Lines for the reply currently streaming, if any."""
        out: List[Tuple[str, str]] = []
        if self._pending_reasoning is not None:
            for line in self._pending_reasoning.lines():
                if line:
                    out.append((RAIL_REASONING, line))
        if self._pending is not None:
            rail_head = self.g("rail")
            for index, line in enumerate(self._pending.lines()):
                if index == 0 and not line and len(self._pending.lines()) == 1:
                    continue
                out.append((rail_head, line))
        return out

    def all_lines(self) -> List[Tuple[str, str]]:
        self._fold_new_turns()
        lines = list(self.lines)
        lines.extend(self._pending_lines())
        if self._thinking_line() is not None:
            lines.append(self._thinking_line())
        return lines

    def _thinking_line(self) -> Optional[Tuple[str, str]]:
        """The in-transcript activity indicator.

        Placed where the reply itself will appear, rather than in a status
        corner, so the operator's eye is already in the right place when the
        first token lands.
        """
        engine = self.app.engine
        if engine.state not in (S.PREPARING, S.WAITING, S.REASONING):
            return None
        if engine.state == S.REASONING and self._pending_reasoning is not None:
            return None
        meter = self.activity.equalizer(self.display, "activity")
        elapsed = engine.elapsed
        if engine.state == S.PREPARING:
            label = "PREPARING"
        elif engine.state == S.REASONING:
            label = "REASONING"
        else:
            label = "READING PROMPT" if elapsed < 1.5 else "COMPOSING"
        body = f"{meter} {label} {elapsed:.0f}s"
        return (self.g("rail"), T.fit(body, self.text_width))

    def invalidate_lines(self) -> None:
        """Re-wrap everything: needed when the reasoning setting changes."""
        self.lines = []
        self._folded = 0
        self.app.display.invalidate()

    # -- engine events ------------------------------------------------------

    def consume(self, events: Sequence[EngineEvent]) -> None:
        show_reasoning = bool(self.app.config.get("chat.show_reasoning", False))
        for event in events:
            if event.kind == "state":
                if event.state in (S.PREPARING, S.WAITING):
                    self.activity.reset()
                    self._pending = T.StreamWrapper(self.text_width)
                    self._pending_reasoning = None
            elif event.kind == "delta":
                if self._pending is None:
                    self._pending = T.StreamWrapper(self.text_width)
                self._pending.append(event.text)
            elif event.kind == "reasoning":
                if show_reasoning:
                    if self._pending_reasoning is None:
                        self._pending_reasoning = T.StreamWrapper(self.text_width)
                    self._pending_reasoning.append(event.text)
            elif event.kind == "done":
                # The engine appends the finished turn to the conversation
                # before emitting this, so dropping the streaming buffers here
                # hands rendering back to the committed-line path with no gap.
                self._pending = None
                self._pending_reasoning = None
                self._log_exchange()
            elif event.kind == "note":
                self.app.conversation.add(S.Turn(role=S.NOTE, text=event.text))
            elif event.kind == "error":
                self._pending = None
                self._pending_reasoning = None
                self.app.conversation.add(
                    S.Turn(role=S.NOTE, text=event.text, error=True))
                if event.detail:
                    self.app.notify(T.ellipsis(event.detail.upper(), 20), 3.0)

    # -- input --------------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        engine = self.app.engine

        if event.ctrl and event.char:
            return self._on_control(event.char)

        if event.key == K.ESC:
            if engine.busy:
                engine.abort()
                self.app.notify("GENERATION HALTED")
                return True
            if self.mode == COMPOSE and not self.field.empty:
                self.field.clear()
                return True
            if self.mode == COMPOSE:
                self.mode = READ
                self.app.display.invalidate()
                return True
            return False

        if event.key == K.ENTER:
            return self._on_enter()

        if event.key in (K.UP, K.DOWN, K.PGUP, K.PGDN):
            step = {K.UP: -1, K.DOWN: 1,
                    K.PGUP: -self.view_rows, K.PGDN: self.view_rows}[event.key]
            self._scroll_by(step)
            return True

        if event.key == K.HOME:
            if self.mode == COMPOSE and not self.field.empty:
                self.field.home()
            else:
                self.scroll = 0
                self.follow = False
            return True

        if event.key == K.END:
            if self.mode == COMPOSE and not self.field.empty:
                self.field.end()
            else:
                self.follow = True
            return True

        if event.key == K.LEFT:
            if self.mode == COMPOSE:
                self.field.move(-1)
                return True
            return False

        if event.key == K.RIGHT:
            if self.mode == COMPOSE:
                self.field.move(1)
                return True
            return False

        if event.key == K.BACKSPACE:
            self._enter_compose()
            if not self.field.backspace() and self.field.empty:
                self.mode = READ
                self.app.display.invalidate()
            self._touch_typing()
            return True

        if event.key == K.DELETE:
            if self.mode == COMPOSE:
                self.field.delete()
                self._touch_typing()
                return True
            return False

        if event.key == K.F2:
            self.app.confirm("CLEAR CONVERSATION?", self._new_conversation)
            return True
        if event.key == K.F5:
            return self._regenerate()
        if event.key == K.F8:
            self._toggle_reasoning()
            return True
        if event.key == K.F9:
            self._export()
            return True

        if event.is_char():
            self._enter_compose()
            self.field.insert(event.char)
            self._touch_typing()
            return True

        return False

    def _on_control(self, char: str) -> bool:
        if char == "u":
            self.field.clear()
            return True
        if char == "w":
            self.field.delete_word()
            return True
        if char == "a":
            self.field.home()
            return True
        if char == "e":
            self.field.end()
            return True
        if char == "l":
            self.app.display.invalidate()
            self.app.display.reset_glyph_cache()
            return True
        if char == "k":
            self.field.text = self.field.text[:self.field.cursor]
            return True
        return False

    def _on_enter(self) -> bool:
        if self.mode == READ:
            self._enter_compose()
            return True
        if self.field.empty:
            self.mode = READ
            self.app.display.invalidate()
            return True
        if self.app.engine.busy:
            self.app.notify("BUSY: ESC HALTS")
            return True
        if not self.app.engine_ready:
            self.app.notify("NO ENGINE: SEE F7", 3.0)
            return True

        message = self.field.text.strip()
        self.field.clear()
        self.mode = READ
        self.follow = True
        self.app.display.invalidate()
        if not self.app.engine.send(message):
            self.app.notify("COULD NOT SEND")
        return True

    def _enter_compose(self) -> None:
        if self.mode != COMPOSE:
            self.mode = COMPOSE
            self.follow = True
            self.app.display.invalidate()

    def _touch_typing(self) -> None:
        self._type_pause = time.monotonic()
        self._prefill_sent = False

    def _scroll_by(self, step: int) -> None:
        total = len(self.all_lines())
        maximum = max(0, total - self.view_rows)
        if self.follow:
            self.scroll = maximum
        self.scroll = max(0, min(maximum, self.scroll + step))
        # Returning to the bottom re-arms autoscroll; that is the only way back
        # into following, and it matches how every terminal behaves.
        self.follow = (self.scroll >= maximum)

    # -- commands -----------------------------------------------------------

    def _new_conversation(self) -> None:
        self.app.engine.reset(self.app._system_prompt())
        self.lines = []
        self._folded = 0
        self._pending = None
        self._pending_reasoning = None
        self.scroll = 0
        self.follow = True
        self.mode = COMPOSE
        self.field.clear()
        self._log_path = ""
        self.app.display.invalidate()
        self.app.notify("CONTEXT CLEARED")

    def _regenerate(self) -> bool:
        if self.app.engine.busy:
            self.app.notify("ENGINE BUSY")
            return True
        if not self.app.engine_ready:
            self.app.notify("ENGINE OFFLINE")
            return True
        # Dropping the old reply from the line buffer keeps the transcript
        # honest: only the answer being kept is shown.
        turns = self.app.conversation.turns
        while turns and turns[-1].role != S.USER:
            turns.pop()
        self.invalidate_lines()
        if not self.app.engine.regenerate():
            self.app.notify("NOTHING TO REDO")
        else:
            self.follow = True
        return True

    def _toggle_reasoning(self) -> None:
        value = not bool(self.app.config.get("chat.show_reasoning", False))
        self.app.config.set("chat.show_reasoning", value)
        self.invalidate_lines()
        self.app.notify("REASONING " + ("SHOWN" if value else "HIDDEN"))

    def _transcript_dir(self) -> str:
        base = self.app.state_dir or os.path.join(self.app.project_root, "state")
        return os.path.join(base, "transcripts")

    def _log_exchange(self) -> None:
        """Append the exchange that just finished to the running session log.

        Appending as each turn completes, rather than writing the whole
        transcript at exit, means an unplanned power cut costs at most the
        reply in flight -- which for an appliance with no shutdown button is
        the failure mode worth designing for.
        """
        if not bool(self.app.config.get("chat.save_history", True)):
            return
        turns = self.app.conversation.turns
        if not turns or turns[-1].role != S.ASSISTANT:
            return
        prompt = ""
        for turn in reversed(turns[:-1]):
            if turn.role == S.USER:
                prompt = turn.text
                break
        try:
            os.makedirs(self._transcript_dir(), exist_ok=True)
            with open(self._session_log_path(), "a", encoding="utf-8") as handle:
                handle.write(f"> {prompt}\n{turns[-1].text.strip()}\n\n")
        except OSError:
            # Losing the log must never interrupt the conversation.
            self._logging_failed = True

    def _session_log_path(self) -> str:
        if not self._log_path:
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.localtime(self.app.conversation.started_at))
            self._log_path = os.path.join(self._transcript_dir(),
                                          f"session-{stamp}.txt")
        return self._log_path

    def _export(self) -> None:
        try:
            path = self.app.engine.export(self._transcript_dir())
        except OSError as exc:
            self.app.notify("EXPORT FAILED")
            self.app.notify(str(exc)[:20], 3.0)
            return
        self.app.notify(f"SAVED {os.path.basename(path)[:14]}", 3.0)

    # -- update -------------------------------------------------------------

    @property
    def autoscroll(self) -> bool:
        return bool(self.app.config.get("chat.autoscroll", True))

    def update(self, dt: float) -> None:
        total = len(self.all_lines())
        self._last_total = total
        # With autoscroll off the viewport stays put as output arrives; END
        # still jumps to the bottom, so nothing becomes unreachable.
        if self.follow and self.autoscroll:
            self.scroll = max(0, total - self.view_rows)
        elif self.follow:
            self.scroll = min(self.scroll, max(0, total - self.view_rows))

        self._maybe_prefill()
        self._update_ticker()

    def _maybe_prefill(self) -> None:
        """Speculatively evaluate the line being typed, once typing pauses.

        The text in the editor is a prefix of what will be sent, so the server
        can ingest it early and the eventual send costs only the tokens added
        since.  Only worth doing after a real pause -- firing on every
        keystroke would keep the CPU busy competing with nothing.
        """
        if not bool(self.app.config.get("engine.type_prefill", False)):
            return
        if self._prefill_sent or self.app.engine.busy or not self.app.engine_ready:
            return
        if self.field.empty or len(self.field.text) < 12:
            return
        if time.monotonic() - self._type_pause < S.ChatEngine.TYPE_PREFILL_IDLE:
            return
        self._prefill_sent = True
        self.app.engine.schedule_prefill(self.field.text.strip())

    def _update_ticker(self) -> None:
        engine = self.app.engine
        items: List[str] = []
        if engine.state == S.IDLE:
            if self.app.model_info is not None:
                items.append(self.app.model_info.stem)
            if engine.last_timings is not None:
                items.append(_rate_text(engine.last_timings.tokens_per_second))
                if engine.last_timings.cached_tokens:
                    items.append(
                        f"{T.format_count(engine.last_timings.cached_tokens)} reused")
            exchanges = self.app.conversation.exchanges
            if exchanges:
                items.append(f"{exchanges} turn" +
                             ("s" if exchanges != 1 else ""))
            if not items:
                items.append("READY")
        self.ticker.set(items)

    # -- drawing ------------------------------------------------------------

    def draw(self, frame: Frame) -> None:
        self._draw_status(frame)
        lines = self.all_lines()
        rows = self.view_rows
        total = len(lines)
        maximum = max(0, total - rows)
        self.scroll = max(0, min(self.scroll, maximum))

        for row_index in range(rows):
            line_index = self.scroll + row_index
            row = 1 + row_index
            if line_index >= total:
                continue
            rail, body = lines[line_index]
            frame.text(row, 0, rail)
            frame.text(row, 1, T.fit(body, self.text_width))

        if total > rows:
            draw_scrollbar(self.display, frame, self.scroll, rows, total,
                           column=frame.cols - 1, top=1)

        if self.mode == COMPOSE:
            self._draw_compose(frame)
        elif self.app.engine.busy:
            self._draw_halt_hint(frame)

    def _draw_status(self, frame: Frame) -> None:
        engine = self.app.engine
        display = self.display

        # Column 0: the state glyph. Shut and still when idle, breathing while
        # the model is working -- the one element that says "alive" from across
        # a bench without reading a word of it.
        if engine.busy:
            frame.text(0, 0, self.activity.iris(display, "state"))
        else:
            frame.text(0, 0, self.activity.still(display, "state", phase=0))

        # Columns 2-11 carry the readout, 12-15 the context gauge, 16-19 the
        # percentage. A readout too long for ten columns scrolls rather than
        # being cut: a truncated model name identifies nothing.
        label = self._status_label()
        frame.text(0, 2, T.marquee(label, 10, self.activity.elapsed, speed=2.5))

        usage = engine.usage
        bar = T.progress_bar(usage.fraction, 4, G.ROM_FULL_BLOCK,
                             display.g("half"))
        frame.text(0, 12, bar)
        frame.text(0, 16, T.fit(f"{usage.percent}%", 4, align="right"))

    def _status_label(self) -> str:
        engine = self.app.engine
        if engine.state == S.ERROR:
            return "FAULT"
        if engine.state == S.ABORTING:
            return "HALTING"
        if engine.state == S.PREPARING:
            return "PREP"
        if engine.state == S.WAITING:
            return f"READ {engine.elapsed:.0f}s"
        if engine.state == S.REASONING:
            return "THINKING"
        if engine.state == S.STREAMING:
            rate = engine.live_tokens_per_second()
            return _rate_text(rate) if rate > 0 else "WRITING"
        if not self.app.engine_ready:
            return "OFFLINE"
        if not self.follow:
            total = len(self.all_lines())
            return f"^{self.scroll + 1}/{total}"
        return self.ticker.current() or "READY"

    def _draw_compose(self, frame: Frame) -> None:
        row = frame.rows - 1
        width = frame.cols - 1
        visible, caret, cut_left, cut_right = self.field.render(width)

        if self.app.engine.busy:
            prompt = self.activity.spinner(self.display, "activity")
        elif cut_left:
            prompt = G.ROM_LEFT_ARROW
        else:
            prompt = ">"

        frame.clear_row(row)
        frame.text(row, 0, prompt)
        frame.text(row, 1, visible)
        # Only flag hidden text to the right when the caret is not sitting in
        # the cell the flag would occupy.
        if cut_right and caret < width - 1:
            frame.text(row, frame.cols - 1, G.ROM_RIGHT_ARROW)

        self.display.set_caret(row, 1 + caret, blinking=True)

    def _draw_halt_hint(self, frame: Frame) -> None:
        """While reading, show how to stop a run-on reply."""
        if self.app.engine.elapsed < 4.0:
            return
        row = frame.rows - 1
        lines = self.all_lines()
        if len(lines) > self.view_rows:
            return          # do not cover transcript that is actually in use
        frame.row_text(row, T.fit("ESC HALTS", frame.cols, align="center"))

    @property
    def hide_caret(self) -> bool:                     # type: ignore[override]
        return self.mode != COMPOSE
