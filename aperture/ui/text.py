"""
Text layout for an eighteen-column column.

Two things here are load-bearing.

:func:`wrap` is ordinary greedy word wrapping, with the one refinement that
matters at this width: a word longer than the column is broken rather than
allowed to overhang, because at eighteen columns "authentication" is not an
edge case.

:class:`StreamWrapper` is the incremental version, and it is what makes token
streaming look right.  Re-wrapping the whole reply on every token would be both
wasteful and visibly wrong -- the text would reflow under the reader as each
token lands.  Instead only the final, still-growing line is re-wrapped; every
line above it is already committed and never moves again.  That is exactly how
a terminal behaves, and it is why the transcript reads as text being typed
rather than as a paragraph being repeatedly re-rendered.
"""

from __future__ import annotations

from typing import List, Tuple

#: Characters a line may be broken after when no space is available.
_BREAK_AFTER = "-/\\,.;:)]}>"


def sanitise(text: str) -> str:
    """Map text onto what a HD44780 can actually display.

    The character ROM is ASCII plus katakana; anything else renders as a random
    glyph or a black box.  Models emit smart quotes, dashes and the occasional
    emoji regardless of instructions, so those are folded to their ASCII
    equivalents here rather than being allowed onto the panel.
    """
    if text.isascii() and "\t" not in text:
        return text
    out = []
    for ch in text:
        replacement = _TRANSLATIONS.get(ch)
        if replacement is not None:
            out.append(replacement)
        elif ch == "\t":
            out.append("  ")
        elif ch == "\n":
            out.append(ch)
        elif ord(ch) < 0x20:
            continue
        elif ord(ch) < 0x7F:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


_TRANSLATIONS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    "…": "...", " ": " ", "•": "-", "·": "-",
    "°": "\xdf",          # degree sign exists in the character ROM
    "×": "x", "÷": "/", "→": "->", "←": "<-",
    "≤": "<=", "≥": ">=", "≠": "!=",
    "½": "1/2", "¼": "1/4", "¾": "3/4",
    "€": "EUR", "£": "GBP", "¥": "YEN",
}


def wrap(text: str, width: int) -> List[str]:
    """Wrap *text* to *width*, honouring explicit newlines."""
    if width <= 0:
        return []
    lines: List[str] = []
    for paragraph in sanitise(text).split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(_wrap_paragraph(paragraph, width))
    return _strip_trailing_blanks(lines)


def _strip_trailing_blanks(lines: List[str]) -> List[str]:
    """Drop blank lines from the end, keeping interior paragraph breaks.

    Models routinely end a reply with one or two newlines. On a viewport three
    rows tall, honouring those would spend a third of the visible transcript on
    nothing, so trailing blanks are suppressed while blank lines *between*
    paragraphs are kept -- they are doing real work.
    """
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines if lines else [""]


def _wrap_paragraph(paragraph: str, width: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in paragraph.split(" "):
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # A word too long for the column gets split at a punctuation break if
        # there is one nearby, and hard-split otherwise.
        while len(word) > width:
            cut = _break_point(word, width)
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def _break_point(word: str, width: int) -> int:
    for index in range(width - 1, max(0, width - 6), -1):
        if word[index - 1] in _BREAK_AFTER:
            return index
    return width


class StreamWrapper:
    """Incrementally wrapped text: append characters, read back lines."""

    def __init__(self, width: int):
        self.width = width
        self._committed: List[str] = []
        self._tail = ""          # the line still being built
        self._raw: List[str] = []

    def __len__(self) -> int:
        return len(self._committed) + (1 if self._tail or not self._committed else 0)

    @property
    def raw(self) -> str:
        return "".join(self._raw)

    def lines(self) -> List[str]:
        """Every line, including the incomplete final one."""
        if self._tail:
            return self._committed + [self._tail]
        return _strip_trailing_blanks(list(self._committed))

    def append(self, text: str) -> int:
        """Add *text*.  Returns how many new complete lines resulted."""
        if not text:
            return 0
        self._raw.append(text)
        before = len(self._committed)
        for index, chunk in enumerate(sanitise(text).split("\n")):
            if index:
                # An explicit newline closes the current line unconditionally.
                self._committed.append(self._tail)
                self._tail = ""
            if chunk:
                self._absorb(chunk)
        return len(self._committed) - before

    def _absorb(self, chunk: str) -> None:
        # Re-wrapping only the live line keeps this O(width) per token rather
        # than O(reply length), and guarantees committed lines never move.
        merged = self._tail + chunk
        if not merged.strip():
            # Whitespace at the start of a fresh line is a word break that has
            # already done its job; wrapping drops it, so drop it here too.
            self._tail = ""
            return

        # Word wrapping discards trailing spaces, which is right for a finished
        # line and wrong for one still being typed: dropping the space that a
        # token ended with would weld it to the next token. So the pending
        # line keeps its trailing space until a following character settles
        # whether it is a word break or the end of the line.
        trailing = len(merged) - len(merged.rstrip(" "))
        wrapped = _wrap_paragraph(merged, self.width)
        self._committed.extend(wrapped[:-1])
        tail = wrapped[-1]
        if trailing:
            room = self.width - len(tail)
            if room > 0:
                tail += " " * min(trailing, room)
            else:
                # The line is exactly full and the next character is a space,
                # so whatever follows must begin a new line. Committing now is
                # what batch wrapping would do, and leaving the space off the
                # end without committing would weld the next word on.
                self._committed.append(tail)
                tail = ""
        self._tail = tail

    def set_width(self, width: int) -> None:
        if width == self.width:
            return
        self.width = width
        text = self.raw
        self._committed = []
        self._tail = ""
        self._raw = []
        if text:
            self.append(text)

    def reset(self, text: str = "") -> None:
        self._committed = []
        self._tail = ""
        self._raw = []
        if text:
            self.append(text)


def ellipsis(text: str, width: int) -> str:
    """Truncate to *width*, marking the cut so nothing looks silently lost."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "\x7e"      # ROM right-arrow


def fit(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    """Truncate or pad *text* to exactly *width*."""
    text = ellipsis(text, width)
    padding = width - len(text)
    if padding <= 0:
        return text
    if align == "right":
        return fill * padding + text
    if align == "center":
        left = padding // 2
        return fill * left + text + fill * (padding - left)
    return text + fill * padding


def pair(left: str, right: str, width: int, gap: str = " ") -> str:
    """Lay out a label and a value on one row, value flush right.

    The value wins when space is short: on a settings row the current value is
    what the operator is looking at, and a truncated label is still recognisable
    where a truncated number is actively misleading.
    """
    right = right[:width]
    room = width - len(right) - 1
    if room <= 0:
        return fit(right, width, align="right")
    return fit(ellipsis(left, room), room) + gap + right


def marquee(text: str, width: int, elapsed: float, speed: float = 3.0,
            pause: float = 1.2, gap: str = "   ") -> str:
    """Scroll *text* through a *width*-wide window.

    Pauses at the start so a label can be read before it begins to move, which
    matters when the thing scrolling is the name of the setting about to be
    changed.
    """
    if len(text) <= width:
        return fit(text, width)
    loop = text + gap
    travel = len(loop)
    cycle = pause + travel / speed
    position = elapsed % cycle
    if position < pause:
        offset = 0
    else:
        offset = int((position - pause) * speed) % travel
    doubled = loop + loop
    return doubled[offset:offset + width]


def progress_bar(fraction: float, width: int, full: str, half: str,
                 empty: str = " ") -> str:
    """Render a horizontal gauge using half-cell resolution glyphs."""
    from ..hal.glyphs import hgauge_cells
    cells = hgauge_cells(fraction, width)
    return "".join(full if c == 2 else (half if c == 1 else empty) for c in cells)


def scroll_window(total: int, visible: int, offset: int) -> Tuple[float, float]:
    """Thumb position and size, as fractions of the scrollbar track."""
    if total <= visible or total <= 0:
        return 0.0, 1.0
    size = max(visible / float(total), 0.12)
    span = max(1, total - visible)
    start = (offset / float(span)) * (1.0 - size)
    return max(0.0, min(1.0 - size, start)), size


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def format_count(value: int) -> str:
    """Compact a token count to at most four characters."""
    if value < 1000:
        return str(value)
    if value < 10000:
        return f"{value / 1000:.1f}K"
    if value < 1000000:
        return f"{value // 1000}K"
    return f"{value / 1000000:.1f}M"
