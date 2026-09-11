"""
Modal dialogs and informational screens.

Modals set ``allow_global_keys = False``: a confirmation prompt that can be
escaped by pressing the settings key leaves the caller's callback dangling, and
on a device with one screen there is nowhere for that state to go.  Every modal
therefore has exactly two exits and both report a result.
"""

from __future__ import annotations

import time
from typing import List, Sequence, Tuple

from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent
from ..hal import keys as K
from ..llm import session as S
from ..services import net as net_service
from ..services import stats as stats_service
from .screen import Screen
from .widgets import Activity, ListView, TextField, draw_title
from . import text as T


class ConfirmScreen(Screen):
    """A yes/no question.  Returns True or False."""

    bank = G.BANK_SYSTEM
    allow_global_keys = False

    def __init__(self, app, question: str, detail: str = "",
                 danger: bool = False, yes: str = "YES", no: str = "NO"):
        super().__init__(app)
        self.question = question
        self.detail = detail
        self.danger = danger
        self.labels = (yes, no)
        # Default to "no" for anything destructive: the muscle-memory Enter
        # press should not be the one that clears a conversation.
        self.choice = 1 if danger else 0
        self.activity = Activity(fps=4.0)

    def on_key(self, event: KeyEvent) -> bool:
        if event.key in (K.LEFT, K.RIGHT, K.UP, K.DOWN, K.TAB):
            self.choice = 1 - self.choice
            return True
        if event.key == K.ENTER:
            self.close(self.choice == 0)
            return True
        if event.key == K.ESC:
            self.close(False)
            return True
        if event.is_char():
            lowered = event.char.lower()
            if lowered == "y":
                self.close(True)
                return True
            if lowered == "n":
                self.close(False)
                return True
        return True                    # modal: swallow everything else

    def draw(self, frame: Frame) -> None:
        marker = self.g("warn") if self.danger else self.g("check")
        frame.text(0, 0, marker)

        # The question wraps rather than truncating. A confirmation whose text
        # is cut off is a confirmation nobody can safely answer, and callers
        # should not have to count columns to ask one.
        question = T.wrap(self.question.upper(), frame.cols - 2)
        frame.text(0, 2, question[0])
        rows_used = 1
        for line in question[1:2]:
            frame.text(rows_used, 0, T.fit(line, frame.cols))
            rows_used += 1

        if self.detail:
            for line in T.wrap(self.detail, frame.cols)[:frame.rows - 1 - rows_used]:
                frame.text(rows_used, 0, T.fit(line, frame.cols))
                rows_used += 1

        row = frame.rows - 1
        yes, no = self.labels
        left = f"[{yes}]" if self.choice == 0 else f" {yes} "
        right = f"[{no}]" if self.choice == 1 else f" {no} "
        frame.row_text(row, T.fit(f"{left}  {right}", frame.cols, align="center"))


class TextEntryScreen(Screen):
    """A single-line text prompt.  Returns the string, or None if cancelled."""

    bank = G.BANK_SYSTEM
    allow_global_keys = False
    hide_caret = False

    def __init__(self, app, title: str, initial: str = "", secret: bool = False,
                 hint: str = "", allow_empty: bool = True):
        super().__init__(app)
        self.title = title
        self.field = TextField(initial)
        self.secret = secret
        self.hint = hint
        self.allow_empty = allow_empty
        self.reveal = False

    def on_key(self, event: KeyEvent) -> bool:
        if event.ctrl and event.char:
            if event.char == "u":
                self.field.clear()
            elif event.char == "w":
                self.field.delete_word()
            elif event.char == "a":
                self.field.home()
            elif event.char == "e":
                self.field.end()
            return True
        if event.key == K.ENTER:
            if not self.allow_empty and self.field.empty:
                self.app.notify("VALUE REQUIRED")
                return True
            self.close(self.field.text)
            return True
        if event.key == K.ESC:
            self.close(None)
            return True
        if event.key == K.BACKSPACE:
            self.field.backspace()
            return True
        if event.key == K.DELETE:
            self.field.delete()
            return True
        if event.key == K.LEFT:
            self.field.move(-1)
            return True
        if event.key == K.RIGHT:
            self.field.move(1)
            return True
        if event.key == K.HOME:
            self.field.home()
            return True
        if event.key == K.END:
            self.field.end()
            return True
        if event.key == K.F2 and self.secret:
            # Letting the operator check a password before committing it beats
            # a failed connection attempt and no way to tell why.
            self.reveal = not self.reveal
            return True
        if event.is_char():
            self.field.insert(event.char)
            return True
        return True

    def draw(self, frame: Frame) -> None:
        draw_title(frame, self.title)
        if self.hint:
            frame.text(1, 0, T.fit(self.hint, frame.cols))
        else:
            frame.text(1, 0, T.fit("ENTER=OK  ESC=CANCEL", frame.cols))
        if self.secret:
            frame.text(2, 0, T.fit("F2 REVEALS TEXT", frame.cols))

        width = frame.cols - 1
        if self.secret and not self.reveal:
            masked = TextField("*" * len(self.field.text))
            masked.cursor = self.field.cursor
            masked.offset = self.field.offset
            visible, caret, cut_left, cut_right = masked.render(width)
        else:
            visible, caret, cut_left, cut_right = self.field.render(width)

        row = frame.rows - 1
        frame.text(row, 0, G.ROM_LEFT_ARROW if cut_left else ">")
        frame.text(row, 1, visible)
        if cut_right and caret < width - 1:
            frame.text(row, frame.cols - 1, G.ROM_RIGHT_ARROW)
        self.display.set_caret(row, 1 + caret, blinking=True)


class ListPickerScreen(Screen):
    """Pick one item from a list.  Returns the item, or None."""

    bank = G.BANK_MENU

    def __init__(self, app, title: str, items: Sequence,
                 label=lambda item: str(item), value=lambda item: "",
                 empty_text: str = "NOTHING FOUND"):
        super().__init__(app)
        self.title = title
        self.items = list(items)
        self.label = label
        self.value = value
        self.empty_text = empty_text
        self.list = ListView(rows=3)
        self.list.set_count(len(self.items))

    def set_items(self, items: Sequence) -> None:
        self.items = list(items)
        self.list.set_count(len(self.items))

    def on_key(self, event: KeyEvent) -> bool:
        if event.key == K.UP:
            self.list.move(-1)
            return True
        if event.key == K.DOWN:
            self.list.move(1)
            return True
        if event.key == K.PGUP:
            self.list.page(-1)
            return True
        if event.key == K.PGDN:
            self.list.page(1)
            return True
        if event.key == K.HOME:
            self.list.home()
            return True
        if event.key == K.END:
            self.list.end()
            return True
        if event.key == K.ENTER:
            if self.items:
                self.close(self.items[self.list.index])
            return True
        if event.key == K.ESC:
            self.close(None)
            return True
        return False

    def draw(self, frame: Frame) -> None:
        counter = f"{self.list.index + 1}/{len(self.items)}" if self.items else ""
        draw_title(frame, self.title, counter)
        if not self.items:
            frame.text(2, 0, T.fit(self.empty_text, frame.cols, align="center"))
            return
        self.list.draw(self.display, frame,
                       lambda index, width: (self.label(self.items[index]),
                                             self.value(self.items[index])))


class MessageScreen(Screen):
    """A scrollable block of read-only text."""

    bank = G.BANK_MENU

    def __init__(self, app, title: str, body: str, rows: int = 3):
        super().__init__(app)
        self.title = title
        self.lines = T.wrap(body, app.display.cols - 1)
        self.list = ListView(rows=rows, wrap_around=False)
        self.list.set_count(len(self.lines))

    def on_key(self, event: KeyEvent) -> bool:
        if event.key in (K.UP, K.DOWN):
            self.list.move(-1 if event.key == K.UP else 1)
            return True
        if event.key in (K.PGUP, K.PGDN):
            self.list.page(-1 if event.key == K.PGUP else 1)
            return True
        if event.key in (K.ESC, K.ENTER):
            self.close(None)
            return True
        return False

    def draw(self, frame: Frame) -> None:
        draw_title(frame, self.title)
        offset = self.list.offset
        for row in range(3):
            index = offset + row
            if index < len(self.lines):
                frame.text(1 + row, 0, T.fit(self.lines[index], frame.cols - 1))
        if len(self.lines) > 3:
            from .widgets import draw_scrollbar
            draw_scrollbar(self.display, frame, offset, 3, len(self.lines),
                           column=frame.cols - 1, top=1)


#: Every binding, in the order the help screen lists them.  This is the single
#: source of truth for the key map -- the README is generated from it, so the
#: documentation cannot drift from the program.
KEYMAP: List[Tuple[str, str]] = [
    ("TYPE", "compose a message"),
    ("ENTER", "send / open compose"),
    ("ESC", "halt reply, clear line, or leave compose"),
    ("UP DOWN", "scroll transcript"),
    ("PGUP PGDN", "scroll by a page"),
    ("HOME END", "jump to start / resume following"),
    ("LEFT RIGHT", "move the caret"),
    ("F1", "this help"),
    ("F2", "clear conversation"),
    ("F3", "choose model"),
    ("F5", "regenerate last reply"),
    ("F6", "diagnostics"),
    ("F7", "settings (F4 also works)"),
    ("F8", "show or hide reasoning"),
    ("F9", "export transcript"),
    ("F10", "shut down"),
    ("F12", "backlight"),
    ("CTRL+U", "clear the line"),
    ("CTRL+W", "delete the previous word"),
    ("CTRL+A/E", "start / end of line"),
    ("CTRL+L", "force a full repaint"),
    ("CTRL+C", "quit immediately"),
]


class HelpScreen(Screen):
    """The key map, scrollable."""

    bank = G.BANK_MENU
    title = "KEYS"

    def __init__(self, app):
        super().__init__(app)
        self.list = ListView(rows=3, wrap_around=False)
        self.list.set_count(len(KEYMAP))

    def on_key(self, event: KeyEvent) -> bool:
        if event.key in (K.UP, K.DOWN):
            self.list.move(-1 if event.key == K.UP else 1)
            return True
        if event.key in (K.PGUP, K.PGDN):
            self.list.page(-1 if event.key == K.PGUP else 1)
            return True
        if event.key in (K.ESC, K.ENTER, K.F1):
            self.close(None)
            return True
        return False

    def draw(self, frame: Frame) -> None:
        draw_title(frame, "KEY MAP", f"{self.list.index + 1}/{len(KEYMAP)}")
        # Two lines per entry would only fit one entry per screen, so the key
        # is drawn on the row and its description scrolls beside it when the
        # row is selected.
        self.list.draw(self.display, frame,
                       lambda index, width: (KEYMAP[index][1], KEYMAP[index][0]))


class DiagnosticsScreen(Screen):
    """Live instrumentation: engine, cache, host, display and input."""

    # The menu bank, not the system one: this screen is a scrolling list and
    # needs the scrollbar slots more than it needs status iconography.
    bank = G.BANK_MENU
    title = "DIAGNOSTICS"

    PAGES = ("ENGINE", "CACHE", "HOST", "PANEL", "INPUT", "ENGINE LOG")

    def __init__(self, app):
        super().__init__(app)
        self.page = 0
        self.list = ListView(rows=3, wrap_around=False)
        self.activity = Activity(fps=6.0)
        self._stats = stats_service.sample()
        self._sampled = 0.0
        self._rows: List[str] = []

    def on_key(self, event: KeyEvent) -> bool:
        if event.key in (K.LEFT, K.RIGHT):
            self.page = (self.page + (1 if event.key == K.RIGHT else -1)) % len(self.PAGES)
            self.list.home()
            return True
        if event.key in (K.UP, K.DOWN):
            self.list.move(-1 if event.key == K.UP else 1)
            return True
        if event.key in (K.PGUP, K.PGDN):
            self.list.page(-1 if event.key == K.PGUP else 1)
            return True
        if event.key in (K.ESC, K.F6):
            self.close(None)
            return True
        return False

    def update(self, dt: float) -> None:
        now = time.monotonic()
        if now - self._sampled > 1.0:
            self._stats = stats_service.sample()
            self._sampled = now
        self._rows = self._build_rows()
        self.list.set_count(len(self._rows))

    def _build_rows(self) -> List[str]:
        app = self.app
        engine = app.engine
        page = self.PAGES[self.page]

        if page == "ENGINE":
            rows = [
                f"STATE {engine.state}",
                f"LINK {'UP' if app.engine_ready else 'DOWN'}",
                f"URL {app.config.base_url.replace('http://','')}",
            ]
            if app.model_info is not None:
                rows.append(f"MODEL {app.model_info.stem}")
                if app.model_info.train_context:
                    rows.append(f"NATIVE CTX {app.model_info.train_context}")
            if app.server is not None:
                rows.append("PROC " + ("RUNNING" if app.server.running else "STOPPED"))
                if app.server.running:
                    rows.append(f"UPTIME {T.format_duration(app.server.uptime)}")
            else:
                rows.append("PROC EXTERNAL")
            props = app.client.props
            if props is not None:
                rows.append(f"SERVER CTX {props.n_ctx}")
                rows.append(f"SLOTS {props.n_slots}")
            if engine.last_error:
                rows.append(f"ERROR {engine.last_error}")
            return rows

        if page == "CACHE":
            usage = engine.usage
            timings = engine.last_timings
            rows = [
                f"WINDOW {usage.window}",
                f"RESERVED {usage.reserve}",
                f"PROMPT {usage.prompt_tokens}",
                f"USED {usage.used} ({usage.percent}%)",
                f"PRIMED {'YES' if engine.cache_primed else 'NO'}",
                f"DROPPED {app.conversation.dropped_turns}",
            ]
            if timings is not None:
                rows += [
                    f"REUSED {timings.cached_tokens}",
                    f"EVALUATED {timings.prompt_tokens}",
                    f"HIT {int(timings.cache_hit_ratio * 100)}%",
                    f"TTFT {timings.time_to_first_token:.2f}s",
                    f"PROMPT {timings.prompt_tokens_per_second:.0f} t/s",
                    f"GEN {timings.tokens_per_second:.1f} t/s",
                ]
            else:
                rows.append("NO TURN YET")
            return rows

        if page == "HOST":
            stats = self._stats
            rows = [
                f"BOARD {stats.model}",
                f"TEMP {stats.cpu_temp:.1f}{G.ROM_DEGREE}C" if stats.cpu_temp
                else "TEMP N/A",
                f"LOAD {stats.load1:.2f}",
                f"MEM {stats.mem_used_mb}/{stats.mem_total_mb} MB",
                f"CPU {stats.cpu_mhz} MHz" if stats.cpu_mhz else "CPU N/A",
                f"UP {stats.uptime_text()}",
            ]
            free = stats_service.disk_free_gib(self.app.config.models_dir or "/")
            rows.append(f"DISK FREE {free:.1f} GiB")
            if stats.throttled:
                rows.append(f"POWER {stats.throttled}")
            status = net_service.status()
            rows.append(f"NET {status.summary()}")
            if status.ip:
                rows.append(f"IP {status.ip}")
            return rows

        if page == "PANEL":
            stats = self.display.stats
            lcd = self.display.lcd
            transport = getattr(lcd.transport, "address", None)
            rows = [
                f"GEOMETRY {self.display.cols}x{self.display.rows}",
                f"ADDR 0x{transport:02X}" if transport is not None else "ADDR SIM",
                f"BUS {getattr(lcd.transport, 'bus_number', 'n/a')}",
                f"FPS {stats.fps:.1f}",
                f"FRAMES {stats.frames}",
                f"LAST {stats.last_frame_bytes} B",
                f"CELLS {stats.cells}",
                f"GLYPH LOADS {stats.glyph_loads}",
                f"TOTAL {T.format_count(stats.port_bytes)} B",
                f"BACKLIGHT {'ON' if self.display.backlight else 'OFF'}",
            ]
            return rows

        if page == "INPUT":
            hub = self.app.keyboard
            rows = [f"SOURCES {hub.device_count}"]
            for description in hub.describe_sources():
                rows.append(description)
            for device in hub.devices:
                rows.append(f"{'GRABBED' if device.grabbed else 'SHARED'} "
                            f"{device.name}")
            if hub.permission_error:
                rows.append("PERMISSION DENIED ON INPUT DEVICES")
            if hub.last_error:
                rows.append(hub.last_error)
            if not hub.has_input:
                rows.append("NO KEYBOARD DETECTED")
            return rows

        # ENGINE LOG
        if self.app.server is not None:
            lines = self.app.server.tail(60)
            return lines or ["NO OUTPUT"]
        return ["ENGINE NOT MANAGED BY THIS PROGRAM"]

    def draw(self, frame: Frame) -> None:
        label = self.PAGES[self.page]
        draw_title(frame, label, f"{self.page + 1}/{len(self.PAGES)}")
        if not self._rows:
            frame.text(2, 0, T.fit("NO DATA", frame.cols, align="center"))
            return
        self.list.draw(self.display, frame,
                       lambda index, width: (self._rows[index], ""),
                       cursor_glyph="spin")
        # The cursor glyph slot doubles as a live activity indicator so it is
        # obvious the page is sampling rather than frozen.
        self.display.set_glyph("spin", G.spinner(self.activity.phase))


def farewell_frame(app) -> None:
    """Leave a deliberate final frame rather than whatever was on screen.

    A character panel holds its last image until it loses power.  Stopping the
    program without doing this leaves half a conversation frozen on the bench
    indefinitely, which looks exactly like a crash.
    """
    display = app.display
    display.use_bank(G.BANK_SYSTEM)
    frame = display.begin_frame()
    frame.text_center(0, "SESSION ENDED")
    exchanges = app.conversation.exchanges
    frame.text_center(1, f"{exchanges} EXCHANGE" + ("S" if exchanges != 1 else ""))
    uptime = time.monotonic() - app.started_at
    frame.text_center(2, f"UPTIME {T.format_duration(uptime)}")
    frame.text_center(3, "PANEL IDLE")
    display.set_caret(None)
    display.present()
