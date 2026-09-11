"""
Framebuffer, differential renderer and CGRAM bank manager.

Everything above this module draws into an off-screen :class:`Frame` and calls
:meth:`Display.present`.  The renderer then works out the smallest set of
controller operations that turns the panel into that frame.

Why bother, for eighty characters?  Because the bus is slow and the UI is
animated.  A full repaint is ~11 ms at 400 kHz and ~45 ms at the 100 kHz
default; at 12 fps that is 13% and 54% of wall-clock time respectively, spent
rewriting characters that did not change.  Differential rendering takes a
typical frame down to a handful of runs, which keeps the token stream smooth
and leaves the bus free for the CGRAM reloads the animations depend on.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import glyphs as G
from .glyphs import GlyphBank
from .lcd import CharacterLCD

#: Merge two dirty runs separated by this many unchanged cells or fewer.
#: A cursor move and a character both cost six port bytes, so bridging a
#: single-cell gap is free and bridging two is a loss.
_MERGE_GAP = 1


class Frame:
    """An off-screen character buffer with clipping text primitives."""

    __slots__ = ("cols", "rows", "buf")

    def __init__(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows
        self.buf: List[List[str]] = [[" "] * cols for _ in range(rows)]

    def clear(self, ch: str = " ") -> None:
        for row in self.buf:
            for i in range(self.cols):
                row[i] = ch

    def clear_row(self, row: int, ch: str = " ") -> None:
        if 0 <= row < self.rows:
            line = self.buf[row]
            for i in range(self.cols):
                line[i] = ch

    def text(self, row: int, col: int, value: str) -> int:
        """Draw *value* at (row, col), clipped.  Returns cells written."""
        if not (0 <= row < self.rows) or not value:
            return 0
        line = self.buf[row]
        written = 0
        for offset, ch in enumerate(value):
            x = col + offset
            if x < 0:
                continue
            if x >= self.cols:
                break
            line[x] = ch
            written += 1
        return written

    def text_right(self, row: int, value: str, margin: int = 0) -> int:
        return self.text(row, self.cols - margin - len(value), value)

    def text_center(self, row: int, value: str) -> int:
        return self.text(row, max(0, (self.cols - len(value)) // 2), value)

    def row_text(self, row: int, value: str, fill: str = " ") -> None:
        """Replace an entire row, padding or truncating to width."""
        self.clear_row(row, fill)
        self.text(row, 0, value[:self.cols])

    def hline(self, row: int, ch: str, start: int = 0,
              end: Optional[int] = None) -> None:
        end = self.cols if end is None else end
        if not (0 <= row < self.rows):
            return
        line = self.buf[row]
        for x in range(max(0, start), min(self.cols, end)):
            line[x] = ch

    def get_row(self, row: int) -> str:
        return "".join(self.buf[row])

    def snapshot(self) -> List[str]:
        return [self.get_row(r) for r in range(self.rows)]

    def copy_from(self, other: "Frame") -> None:
        for r in range(self.rows):
            self.buf[r][:] = other.buf[r]


class RenderStats:
    """Rolling counters surfaced on the diagnostics screen."""

    __slots__ = ("frames", "port_bytes", "cells", "glyph_loads",
                 "last_frame_bytes", "_window_start", "_window_frames", "fps")

    def __init__(self) -> None:
        self.frames = 0
        self.port_bytes = 0
        self.cells = 0
        self.glyph_loads = 0
        self.last_frame_bytes = 0
        self._window_start = time.monotonic()
        self._window_frames = 0
        self.fps = 0.0

    def tick_frame(self, port_bytes: int, cells: int, glyph_loads: int) -> None:
        self.frames += 1
        self.port_bytes += port_bytes
        self.cells += cells
        self.glyph_loads += glyph_loads
        self.last_frame_bytes = port_bytes
        self._window_frames += 1
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed >= 1.0:
            self.fps = self._window_frames / elapsed
            self._window_frames = 0
            self._window_start = now


class Display:
    """Owns the panel, the back buffer and the CGRAM bank in use."""

    def __init__(self, lcd: CharacterLCD):
        self.lcd = lcd
        self.cols = lcd.cols
        self.rows = lcd.rows
        self.frame = Frame(self.cols, self.rows)
        self._shadow = Frame(self.cols, self.rows)
        self._shadow_valid = False
        self.stats = RenderStats()

        self._bank: Optional[GlyphBank] = None
        self._slot_patterns: List[Optional[List[int]]] = [None] * G.CGRAM_SLOTS
        self._caret: Optional[Tuple[int, int]] = None
        self._caret_blink = True
        self._pending_glyphs: Dict[int, List[int]] = {}

    # -- glyph bank management ---------------------------------------------

    def use_bank(self, bank: GlyphBank) -> None:
        """Make *bank* the active CGRAM contents.

        Only slots whose bitmap actually differs are rewritten, so re-asserting
        the current bank every frame (which screens do, because it keeps their
        draw code self-contained) costs nothing.
        """
        self._bank = bank
        for slot, pattern in enumerate(bank.patterns_in_order()):
            if bank.is_dynamic(bank.names[slot]):
                # Dynamic slots are owned by the screen; do not stomp on the
                # value it set for this frame.
                if self._slot_patterns[slot] is None:
                    self._queue_glyph(slot, pattern)
                continue
            self._queue_glyph(slot, pattern)

    def _queue_glyph(self, slot: int, pattern: Sequence[int]) -> None:
        pattern = list(pattern)
        if self._slot_patterns[slot] == pattern:
            return
        self._slot_patterns[slot] = pattern
        self._pending_glyphs[slot] = pattern

    def set_glyph(self, name: str, pattern: Sequence[int]) -> None:
        """Update a dynamic slot of the active bank."""
        if self._bank is None:
            raise RuntimeError("no glyph bank is active")
        self._queue_glyph(self._bank.slot(name), pattern)

    def g(self, name: str) -> str:
        """The character that renders *name* in the active bank."""
        if self._bank is None:
            raise RuntimeError("no glyph bank is active")
        return self._bank.char(name)

    # -- caret --------------------------------------------------------------

    def set_caret(self, row: Optional[int] = None, col: int = 0,
                  blinking: bool = True) -> None:
        """Park the controller's hardware cursor, or hide it with ``None``.

        The compose line uses this instead of drawing its own caret: the
        controller blinks it internally, which costs no bus traffic and stays
        perfectly steady no matter what the render loop is doing.
        """
        self._caret = None if row is None else (row, col)
        self._caret_blink = blinking

    # -- frame lifecycle ----------------------------------------------------

    def begin_frame(self) -> Frame:
        self.frame.clear()
        self._caret = None
        return self.frame

    def invalidate(self) -> None:
        """Force the next present to repaint every cell."""
        self._shadow_valid = False

    def reset_glyph_cache(self) -> None:
        self._slot_patterns = [None] * G.CGRAM_SLOTS
        self._pending_glyphs.clear()

    def present(self) -> int:
        """Push the back buffer to the panel.  Returns port bytes written."""
        lcd = self.lcd
        before_glyphs = len(self._pending_glyphs)

        # CGRAM first: loading a glyph clobbers the DDRAM address counter, so
        # doing it after the text would force every run to re-address.
        for slot in sorted(self._pending_glyphs):
            lcd.load_glyph(slot, self._pending_glyphs[slot])
        self._pending_glyphs.clear()

        cells = 0
        if not self._shadow_valid:
            lcd.clear()
            for row in range(self.rows):
                line = self.frame.get_row(row)
                lcd.write_at(row, 0, line)
                cells += len(line)
            self._shadow_valid = True
        else:
            for row in range(self.rows):
                for start, end in self._dirty_runs(row):
                    text = "".join(self.frame.buf[row][start:end])
                    lcd.write_at(row, start, text)
                    cells += len(text)

        self._shadow.copy_from(self.frame)

        if self._caret is not None:
            lcd.set_cursor_style(visible=False, blinking=self._caret_blink)
            lcd.set_cursor(self._caret[0], self._caret[1])
        else:
            lcd.set_cursor_style(visible=False, blinking=False)

        port_bytes = len(lcd._pending)
        lcd.flush()
        self.stats.tick_frame(port_bytes, cells, before_glyphs)
        return port_bytes

    def _dirty_runs(self, row: int) -> List[Tuple[int, int]]:
        """Half-open [start, end) spans of *row* that differ from the panel."""
        new = self.frame.buf[row]
        old = self._shadow.buf[row]
        runs: List[Tuple[int, int]] = []
        start = None
        for col in range(self.cols):
            if new[col] != old[col]:
                if start is None:
                    start = col
            elif start is not None:
                runs.append((start, col))
                start = None
        if start is not None:
            runs.append((start, self.cols))

        if len(runs) < 2:
            return runs
        merged = [runs[0]]
        for span in runs[1:]:
            prev_start, prev_end = merged[-1]
            if span[0] - prev_end <= _MERGE_GAP:
                merged[-1] = (prev_start, span[1])
            else:
                merged.append(span)
        return merged

    # -- panel passthrough --------------------------------------------------

    @property
    def backlight(self) -> bool:
        return self.lcd.backlight

    @backlight.setter
    def backlight(self, value: bool) -> None:
        self.lcd.backlight = value

    def close(self) -> None:
        self.lcd.close()
