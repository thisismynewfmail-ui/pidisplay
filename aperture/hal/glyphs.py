"""
CGRAM glyph engine for HD44780-class character displays.

The HD44780 provides eight user-definable 5x8 characters (CGRAM slots 0-7).
Eight glyphs is not many, so this module treats CGRAM as a small, reloadable
*register file* rather than a fixed icon set:

  * A :class:`GlyphBank` is a named, ordered set of up to eight patterns.  The
    display reprograms CGRAM only when the active bank changes, so switching
    between (say) the chat view and the settings menu costs one burst of I2C
    traffic instead of one per frame.

  * Individual slots may be marked *dynamic*.  A dynamic slot is rewritten in
    place whenever its pattern changes, which is how every animation in this
    program works: instead of cycling through several static characters we
    redraw the single character's bitmap.  Reprogramming one slot costs nine
    controller writes -- about the same as drawing nine text characters -- so a
    12 fps animation is essentially free next to a full screen repaint.

Patterns are represented as a list of eight integers, one per pixel row, each
holding five significant bits.  Bit 4 (0x10) is the leftmost pixel column.

Characters are emitted as codes 8..15 rather than 0..7.  The controller mirrors
CGRAM into both ranges, and avoiding code 0 keeps the patterns usable inside
ordinary Python strings without NUL-termination surprises anywhere down the
stack.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence

# Public constants -----------------------------------------------------------

CGRAM_SLOTS = 8
#: CGRAM characters are emitted in the 8..15 alias range (see module docstring).
CODE_BASE = 8

#: Character-ROM (A00) codepoints that are worth naming.  These cost no CGRAM.
ROM_FULL_BLOCK = "\xff"      # solid 5x8 block
ROM_RIGHT_ARROW = "\x7e"     # ->
ROM_LEFT_ARROW = "\x7f"      # <-
ROM_DEGREE = "\xdf"
ROM_DOT = "\xa5"             # centred dot (katakana middle dot)


def code(index: int) -> str:
    """Return the printable character that renders CGRAM slot *index*."""
    if not 0 <= index < CGRAM_SLOTS:
        raise ValueError(f"CGRAM slot out of range: {index}")
    return chr(CODE_BASE + index)


def _rows(*bits: str) -> List[int]:
    """Build a pattern from eight five-character strings of '.'/'#'.

    Writing the bitmaps as little ASCII pictures keeps them reviewable; a wall
    of 0b01110 literals does not.
    """
    if len(bits) != 8:
        raise ValueError("a 5x8 glyph needs exactly 8 rows")
    out: List[int] = []
    for row in bits:
        if len(row) != 5:
            raise ValueError(f"glyph row must be 5 columns: {row!r}")
        value = 0
        for col, ch in enumerate(row):
            if ch not in ".#":
                raise ValueError(f"glyph pixels must be '.' or '#': {row!r}")
            if ch == "#":
                value |= 1 << (4 - col)
        out.append(value)
    return out


# --------------------------------------------------------------------------
# Static patterns
# --------------------------------------------------------------------------

#: Continuation rail drawn in the left gutter beneath a wrapped machine turn.
RAIL_DOT = _rows(
    "..#..",
    ".....",
    "..#..",
    ".....",
    "..#..",
    ".....",
    "..#..",
    ".....",
)

#: Continuation rail for an operator turn -- solid, to contrast with the above.
RAIL_SOLID = _rows(
    "..#..",
    "..#..",
    "..#..",
    "..#..",
    "..#..",
    "..#..",
    "..#..",
    ".....",
)

#: Left half of a cell, used to give horizontal gauges half-cell resolution.
HALF_BLOCK = _rows(
    "###..",
    "###..",
    "###..",
    "###..",
    "###..",
    "###..",
    "###..",
    "###..",
)

ARROW_UP = _rows(
    ".....",
    "..#..",
    ".###.",
    "#####",
    "..#..",
    "..#..",
    "..#..",
    ".....",
)

ARROW_DOWN = _rows(
    ".....",
    "..#..",
    "..#..",
    "..#..",
    "#####",
    ".###.",
    "..#..",
    ".....",
)

TRI_LEFT = _rows(
    ".....",
    "...#.",
    "..##.",
    ".###.",
    "..##.",
    "...#.",
    ".....",
    ".....",
)

TRI_RIGHT = _rows(
    ".....",
    ".#...",
    ".##..",
    ".###.",
    ".##..",
    ".#...",
    ".....",
    ".....",
)

CHECK = _rows(
    ".....",
    ".....",
    "....#",
    "...##",
    "#.##.",
    "###..",
    ".#...",
    ".....",
)

CROSS = _rows(
    ".....",
    "#...#",
    ".#.#.",
    "..#..",
    ".#.#.",
    "#...#",
    ".....",
    ".....",
)

LOCK = _rows(
    ".###.",
    "#...#",
    "#...#",
    "#####",
    "#.#.#",
    "#.#.#",
    "#####",
    ".....",
)

ANTENNA = _rows(
    "....#",
    "....#",
    "..#.#",
    "..#.#",
    "#.#.#",
    "#.#.#",
    "#.#.#",
    ".....",
)

BLUETOOTH = _rows(
    "..#..",
    "..##.",
    "#.#.#",
    ".###.",
    ".###.",
    "#.#.#",
    "..##.",
    "..#..",
)

CHEVRON = _rows(
    ".....",
    ".#...",
    ".##..",
    ".###.",
    ".##..",
    ".#...",
    ".....",
    ".....",
)

CARET = _rows(
    ".....",
    "#....",
    "##...",
    "###..",
    "####.",
    "###..",
    "##...",
    "#....",
)

WARN = _rows(
    ".....",
    "..#..",
    "..#..",
    ".###.",
    ".###.",
    "#####",
    "#####",
    ".....",
)

PLUG = _rows(
    ".#.#.",
    ".#.#.",
    "#####",
    "#####",
    ".###.",
    "..#..",
    "..#..",
    ".....",
)


# --------------------------------------------------------------------------
# Pattern generators (for dynamic slots)
# --------------------------------------------------------------------------

#: A five-phase iris.  Phase 0 is shut, phase 4 is wide open.  Used as the
#: machine's state glyph: shut while idle, breathing while it is working.
_IRIS = [
    _rows(".....", ".###.", "#####", "#####", "#####", ".###.", ".....", "....."),
    _rows(".....", ".###.", "#####", "##.##", "#####", ".###.", ".....", "....."),
    _rows(".....", ".###.", "##.##", "#...#", "##.##", ".###.", ".....", "....."),
    _rows(".....", ".###.", "#...#", "#...#", "#...#", ".###.", ".....", "....."),
    _rows(".....", ".#.#.", "#...#", "#...#", "#...#", ".#.#.", ".....", "....."),
]

#: Ping-pong order so the iris breathes instead of snapping shut every cycle.
IRIS_CYCLE = (0, 1, 2, 3, 4, 3, 2, 1)


def iris(phase: int) -> List[int]:
    """Iris bitmap for animation *phase* (any integer; wraps automatically)."""
    return _IRIS[IRIS_CYCLE[phase % len(IRIS_CYCLE)]]


_SPINNER = [
    _rows(".....", ".....", ".....", "#####", ".....", ".....", ".....", "....."),
    _rows(".....", "....#", "...#.", "..#..", ".#...", "#....", ".....", "....."),
    _rows(".....", "..#..", "..#..", "..#..", "..#..", "..#..", ".....", "....."),
    _rows(".....", "#....", ".#...", "..#..", "...#.", "....#", ".....", "....."),
]


def spinner(phase: int) -> List[int]:
    """Four-phase rotating bar."""
    return _SPINNER[phase % len(_SPINNER)]


def vbar(level: int) -> List[int]:
    """A bottom-anchored vertical bar filled to *level* of 8 pixel rows."""
    level = max(0, min(8, level))
    return [0b11111 if row >= (8 - level) else 0 for row in range(8)]


def equalizer(phase: int, seed: int = 0) -> List[int]:
    """A five-column bar meter that churns -- the 'working' animation.

    Each of the cell's five pixel columns rises and falls on its own period, so
    the cell never repeats over any interval a person would notice while still
    being completely deterministic (no RNG, no allocation per frame).
    """
    periods = (5, 7, 4, 9, 6)
    offsets = (0, 3, 5, 1, 4)
    heights = []
    for i in range(5):
        p = periods[i]
        t = (phase + offsets[i] + seed) % (2 * p)
        heights.append(t if t < p else 2 * p - t)
    rows = []
    for row in range(8):
        value = 0
        for col in range(5):
            # Scale each column's 0..period range onto 1..7 pixels tall.
            h = 1 + (heights[col] * 6) // max(1, periods[col])
            if row >= 8 - h:
                value |= 1 << (4 - col)
        rows.append(value)
    return rows


def marquee(phase: int, density: int = 2) -> List[int]:
    """A horizontally scrolling dot field used as an activity rail."""
    rows = []
    for row in range(8):
        value = 0
        if row in (3, 4):
            for col in range(5):
                if (col + phase + row) % (density + 2) == 0:
                    value |= 1 << (4 - col)
        rows.append(value)
    return rows


def scroll_segment(cell_row: int, thumb_start: float, thumb_size: float,
                   cells: int, track: bool = True) -> List[int]:
    """One cell of a proportional vertical scrollbar.

    The scrollbar is drawn down a column of *cells* characters; each character
    contributes eight pixel rows, giving ``cells * 8`` positions of resolution.
    ``thumb_start`` and ``thumb_size`` are fractions of the whole track.

    ``track`` draws a faint one-pixel rail behind the thumb so an empty region
    still reads as a scrollbar rather than as blank screen.
    """
    total_px = cells * 8
    start_px = int(round(thumb_start * total_px))
    size_px = max(2, int(round(thumb_size * total_px)))
    if start_px + size_px > total_px:
        start_px = total_px - size_px
    start_px = max(0, start_px)

    rows = []
    for row in range(8):
        absolute = cell_row * 8 + row
        if start_px <= absolute < start_px + size_px:
            rows.append(0b01110)
        elif track:
            rows.append(0b00100 if absolute % 2 == 0 else 0b00000)
        else:
            rows.append(0)
    return rows


def hgauge_cells(fraction: float, cells: int) -> List[int]:
    """Split *fraction* across *cells* characters at half-cell resolution.

    Returns one value per cell: 0 empty, 1 half full, 2 full.  Half-cell
    resolution costs a single CGRAM slot (:data:`HALF_BLOCK`) and doubles the
    apparent precision of every gauge on the display.
    """
    fraction = max(0.0, min(1.0, fraction))
    halves = int(round(fraction * cells * 2))
    # Never show a completely empty gauge for a non-zero value, and never show
    # a full gauge for anything short of the real maximum: an operator reading
    # a context meter needs to trust both ends of it.
    if fraction > 0.0 and halves == 0:
        halves = 1
    if fraction < 1.0 and halves >= cells * 2:
        halves = cells * 2 - 1
    out = []
    for i in range(cells):
        remaining = halves - i * 2
        out.append(2 if remaining >= 2 else (1 if remaining == 1 else 0))
    return out


# --------------------------------------------------------------------------
# Banks
# --------------------------------------------------------------------------

class GlyphBank:
    """A named, ordered set of at most eight CGRAM patterns.

    Slots whose name appears in *dynamic* start out blank and are expected to
    be filled in each frame by the owner via :meth:`Display.set_glyph`.
    """

    __slots__ = ("name", "_order", "_patterns", "_dynamic")

    def __init__(self, name: str, patterns: Dict[str, Sequence[int]],
                 dynamic: Iterable[str] = ()):
        if len(patterns) > CGRAM_SLOTS:
            raise ValueError(
                f"bank {name!r} declares {len(patterns)} glyphs; "
                f"the controller has {CGRAM_SLOTS}"
            )
        self.name = name
        self._order = list(patterns.keys())
        self._patterns = {k: list(v) for k, v in patterns.items()}
        self._dynamic = set(dynamic)
        unknown = self._dynamic - set(self._order)
        if unknown:
            raise ValueError(f"bank {name!r} marks unknown glyphs dynamic: {unknown}")

    def __contains__(self, glyph: str) -> bool:
        return glyph in self._patterns

    @property
    def names(self) -> List[str]:
        return list(self._order)

    def slot(self, glyph: str) -> int:
        try:
            return self._order.index(glyph)
        except ValueError:
            raise KeyError(
                f"glyph {glyph!r} is not in bank {self.name!r}; "
                f"available: {', '.join(self._order)}"
            ) from None

    def char(self, glyph: str) -> str:
        """The printable character that renders *glyph* while this bank is up."""
        return code(self.slot(glyph))

    def pattern(self, glyph: str) -> List[int]:
        return self._patterns[glyph]

    def is_dynamic(self, glyph: str) -> bool:
        return glyph in self._dynamic

    def patterns_in_order(self) -> List[List[int]]:
        return [self._patterns[n] for n in self._order]


_BLANK = [0] * 8

#: Chat transcript.  Three of the eight slots are a proportional scrollbar and
#: one is the animated state indicator in the status row.
BANK_CHAT = GlyphBank(
    "chat",
    {
        "state": iris(0),
        "rail": RAIL_DOT,
        "rail_user": RAIL_SOLID,
        "half": HALF_BLOCK,
        "activity": _BLANK,
        "sb0": _BLANK,
        "sb1": _BLANK,
        "sb2": _BLANK,
    },
    dynamic=("state", "activity", "sb0", "sb1", "sb2"),
)

#: Menus, settings pages and every other scrolling list.
#:
#: Three of the eight slots go to the proportional scrollbar. That is a large
#: share, but a list on a three-row viewport is unusable without one -- there
#: is otherwise no way to tell a three-item menu from the middle of a thirty-
#: item one. The up and down arrow glyphs it replaces are not missed, and the
#: gauge half-block is not needed on a page that draws no gauges.
BANK_MENU = GlyphBank(
    "menu",
    {
        "cursor": CHEVRON,
        "left": TRI_LEFT,
        "right": TRI_RIGHT,
        "check": CHECK,
        "spin": _BLANK,
        "sb0": _BLANK,
        "sb1": _BLANK,
        "sb2": _BLANK,
    },
    dynamic=("spin", "sb0", "sb1", "sb2"),
)

#: Boot stages and modal dialogs: status iconography, no lists.
BANK_SYSTEM = GlyphBank(
    "system",
    {
        "state": iris(0),
        "half": HALF_BLOCK,
        "check": CHECK,
        "cross": CROSS,
        "warn": WARN,
        "wifi": ANTENNA,
        "bt": BLUETOOTH,
        "spin": _BLANK,
    },
    dynamic=("state", "spin"),
)

#: The Bluetooth device list, which needs both a scrollbar and radio icons.
BANK_DEVICES = GlyphBank(
    "devices",
    {
        "cursor": CHEVRON,
        "bt": BLUETOOTH,
        "check": CHECK,
        "warn": WARN,
        "spin": _BLANK,
        "sb0": _BLANK,
        "sb1": _BLANK,
        "sb2": _BLANK,
    },
    dynamic=("spin", "sb0", "sb1", "sb2"),
)

#: Scrollbar slot names, in top-to-bottom order.
SCROLLBAR_SLOTS = ("sb0", "sb1", "sb2")

BANKS = {b.name: b for b in (BANK_CHAT, BANK_MENU, BANK_SYSTEM, BANK_DEVICES)}
