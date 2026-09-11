"""
A terminal mirror of the panel, for development without the hardware.

This is not a mock of the UI: it renders the output of the real
:class:`~aperture.hal.emulator.HD44780Emulator`, which is itself driven by the
real driver.  What appears in the terminal is what the panel would show,
including custom glyphs, the hardware cursor and the backlight state.

Two fidelities are offered.  Pixel mode draws every one of the 100x32 dots
using half-block characters, which is the only way to check that a custom glyph
actually reads as the thing it is meant to be.  Text mode draws the twenty by
four character grid with CGRAM slots shown as bracketed numbers, for terminals
too narrow for the real thing.

Output goes through plain ANSI rather than curses so that the ordinary stdin
keyboard reader keeps working unchanged -- the simulator needs no special input
path, which means the key handling under test is the key handling that ships.
"""

from __future__ import annotations

import shutil
import sys
from typing import List, Optional

from .hal.emulator import HD44780Emulator

_UPPER_HALF = "▀"
_FULL = "█"
_EMPTY = " "

#: Pixel mode needs this many columns: 20 cells x 5 dots, plus a frame.
PIXEL_WIDTH = 20 * 5 + 4


class TerminalSimulator:
    """Mirrors an emulator into the terminal, redrawing only on change."""

    def __init__(self, emulator: HD44780Emulator, mode: str = "auto",
                 stream=None):
        self.emulator = emulator
        self.stream = stream or sys.stdout
        self.mode = self._resolve_mode(mode)
        self._last: Optional[str] = None
        self._drawn = False

    @staticmethod
    def _resolve_mode(mode: str) -> str:
        if mode in ("pixel", "text"):
            return mode
        try:
            width = shutil.get_terminal_size().columns
        except OSError:
            width = 80
        return "pixel" if width >= PIXEL_WIDTH else "text"

    # -- rendering ----------------------------------------------------------

    def render(self) -> None:
        """Redraw if anything changed.  Cheap to call every frame."""
        body = self._compose()
        if body == self._last:
            return
        self._last = body
        try:
            # Home the cursor and overwrite rather than clearing, so the panel
            # does not flicker between frames.
            self.stream.write("\x1b[H" + body)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _compose(self) -> str:
        emulator = self.emulator
        lines: List[str] = []
        lit = emulator.backlight
        width = PIXEL_WIDTH if self.mode == "pixel" else 24

        header = " APERTURE TERMINAL -- simulated panel "
        lines.append(_centre(header, width))
        lines.append("+" + "-" * (width - 2) + "+")

        if self.mode == "pixel":
            lines.extend(self._pixel_rows())
        else:
            lines.extend(self._text_rows())

        lines.append("+" + "-" * (width - 2) + "+")
        status = (f" backlight {'on ' if lit else 'off'}  "
                  f"bytes {emulator.bytes_written}  "
                  f"cmds {emulator.commands} ")
        lines.append(_centre(status, width))
        lines.append(_centre(" F7 settings   F1 keys   F10 quit ", width))
        # Clear to end of line on every row so shorter frames do not leave
        # debris from longer ones behind.
        return "\r\n".join(line + "\x1b[K" for line in lines) + "\x1b[J"

    def _pixel_rows(self) -> List[str]:
        bitmap = self.emulator.pixel_screen()
        rows: List[str] = []
        for top in range(0, len(bitmap), 2):
            upper = bitmap[top]
            lower = bitmap[top + 1] if top + 1 < len(bitmap) else [0] * len(upper)
            cells = []
            for x in range(len(upper)):
                high, low = upper[x], lower[x]
                if high and low:
                    cells.append(_FULL)
                elif high:
                    cells.append(_UPPER_HALF)
                elif low:
                    cells.append("▄")
                else:
                    cells.append(_EMPTY)
            rows.append("| " + "".join(cells) + " |")
        return rows

    def _text_rows(self) -> List[str]:
        rows = []
        for line in self.emulator.text_screen():
            shown = []
            for ch in line:
                code = ord(ch)
                if code < 16:
                    shown.append(str(code % 8))
                elif code == 0xFF:
                    shown.append(_FULL)
                elif 0x20 <= code < 0x7F:
                    shown.append(ch)
                else:
                    shown.append("·")
            rows.append("| " + "".join(shown) + " |")
        return rows

    # -- terminal management ------------------------------------------------

    def enter(self) -> None:
        try:
            self.stream.write("\x1b[?25l\x1b[2J\x1b[H")   # hide cursor, clear
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def leave(self) -> None:
        try:
            self.stream.write("\x1b[?25h\r\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass


def _centre(text: str, width: int) -> str:
    if len(text) >= width:
        return text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def ascii_frame(emulator: HD44780Emulator) -> str:
    """A plain 20x4 rendering, for logs and the test-suite."""
    border = "+" + "-" * emulator.cols + "+"
    rows = [border]
    for line in emulator.readable_screen(placeholder="@"):
        rows.append("|" + line + "|")
    rows.append(border)
    return "\n".join(rows)
