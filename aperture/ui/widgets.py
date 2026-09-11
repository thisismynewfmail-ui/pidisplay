"""
Reusable interface components sized for a 20x4 panel.

Every widget here draws into a :class:`~aperture.hal.display.Frame` and returns
nothing; state lives in the widget so screens stay declarative.  The recurring
constraint is that there are three usable content rows, so each widget has an
opinion about what to sacrifice first when space runs out, and each one says so
where it makes that choice.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence, Tuple

from ..hal import glyphs as G
from ..hal.display import Display, Frame
from . import text as T


class ListView:
    """A vertically scrolling list with a selection cursor.

    The viewport is usually three rows, which is small enough that a scrollbar
    is not optional -- without one there is no way to tell a three-item list
    from the middle of a thirty-item one.
    """

    def __init__(self, rows: int = 3, wrap_around: bool = True):
        self.rows = rows
        self.wrap_around = wrap_around
        self.index = 0
        self.offset = 0
        self.count = 0
        self._selected_since = time.monotonic()

    # -- selection ----------------------------------------------------------

    def set_count(self, count: int) -> None:
        self.count = max(0, count)
        if self.index >= self.count:
            self.index = max(0, self.count - 1)
        self._clamp_offset()

    def move(self, delta: int) -> bool:
        """Move the cursor.  Returns False when it could not move."""
        if self.count == 0:
            return False
        target = self.index + delta
        if target < 0 or target >= self.count:
            if not self.wrap_around:
                target = max(0, min(self.count - 1, target))
                if target == self.index:
                    return False
            else:
                target %= self.count
        self.select(target)
        return True

    def select(self, index: int) -> None:
        if self.count == 0:
            self.index = 0
            return
        index = max(0, min(self.count - 1, index))
        if index != self.index:
            self._selected_since = time.monotonic()
        self.index = index
        self._clamp_offset()

    def page(self, direction: int) -> bool:
        return self.move(direction * self.rows)

    def home(self) -> None:
        self.select(0)

    def end(self) -> None:
        self.select(self.count - 1)

    def _clamp_offset(self) -> None:
        # Keep one row of lookahead where there is room: seeing the next item
        # is most of what makes a three-row list navigable.
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + self.rows:
            self.offset = self.index - self.rows + 1
        self.offset = max(0, min(self.offset, max(0, self.count - self.rows)))

    @property
    def selection_age(self) -> float:
        return time.monotonic() - self._selected_since

    @property
    def visible_range(self) -> range:
        return range(self.offset, min(self.count, self.offset + self.rows))

    @property
    def scrollable(self) -> bool:
        return self.count > self.rows

    # -- drawing ------------------------------------------------------------

    def draw(self, display: Display, frame: Frame,
             render: Callable[[int, int], Tuple[str, str]],
             top: int = 1, cursor_glyph: str = "cursor",
             scrollbar: bool = True) -> None:
        """Draw the list.

        *render* is called as ``render(item_index, width)`` and returns
        ``(label, value)``.  The value is drawn flush right and is never
        truncated in favour of the label -- see :func:`ui.text.pair`.
        """
        gutter = 1
        bar = 1 if (scrollbar and self.scrollable) else 0
        width = frame.cols - gutter - bar

        for row_offset, index in enumerate(self.visible_range):
            row = top + row_offset
            selected = (index == self.index)
            label, value = render(index, width)

            if selected:
                frame.text(row, 0, display.g(cursor_glyph))
                # Only the selected row scrolls a long label: several rows
                # moving at once is unreadable.
                body = T.marquee(label, width - (len(value) + 1 if value else 0),
                                 self.selection_age) if value else \
                       T.marquee(label, width, self.selection_age)
                line = (T.fit(body, width - len(value) - 1) + " " + value
                        if value else T.fit(body, width))
            else:
                line = T.pair(label, value, width) if value else T.fit(label, width)

            frame.text(row, gutter, line)

        if bar:
            draw_scrollbar(display, frame, self.offset, self.rows, self.count,
                           column=frame.cols - 1, top=top)


def draw_scrollbar(display: Display, frame: Frame, offset: int, visible: int,
                   total: int, column: int, top: int = 1,
                   slots: Sequence[str] = G.SCROLLBAR_SLOTS) -> None:
    """A proportional scrollbar drawn down one column.

    Each character cell contributes eight pixel rows, so three cells give
    twenty-four positions of resolution -- enough that the thumb visibly creeps
    rather than jumping between three states.  The cells are dynamic CGRAM
    slots, redrawn only when the scroll position changes.

    A bank with no room for those three slots gets a coarse scrollbar built
    from character-ROM glyphs instead.  Degrading is deliberate: a screen
    should never fail to draw because of how its bank is allocated.
    """
    rows = min(visible, len(slots))
    start, size = T.scroll_window(total, visible, offset)

    if not _bank_has(display, slots[:rows]):
        _draw_rom_scrollbar(frame, start, size, rows, column, top)
        return

    for cell in range(rows):
        pattern = G.scroll_segment(cell, start, size, rows)
        display.set_glyph(slots[cell], pattern)
        frame.text(top + cell, column, display.g(slots[cell]))


def _bank_has(display: Display, names: Sequence[str]) -> bool:
    bank = display._bank
    return bank is not None and all(name in bank for name in names)


def _draw_rom_scrollbar(frame: Frame, start: float, size: float, rows: int,
                        column: int, top: int) -> None:
    first = int(start * rows)
    last = max(first, int((start + size) * rows - 1e-6))
    for cell in range(rows):
        if first <= cell <= last:
            mark = G.ROM_FULL_BLOCK
        elif cell == 0:
            mark = "^"
        elif cell == rows - 1:
            mark = "v"
        else:
            mark = ":"
        frame.text(top + cell, column, mark)


class TextField:
    """A single-line editor with a horizontally scrolling window.

    The caret is the controller's own hardware cursor rather than a drawn
    character: it blinks on the panel's clock, costs no bus traffic, and cannot
    tear against a streaming redraw.  :meth:`render` therefore returns the
    screen column the caller should park it at.
    """

    def __init__(self, text: str = "", limit: int = 1024):
        self.text = text
        self.cursor = len(text)
        self.offset = 0
        self.limit = limit

    # -- editing ------------------------------------------------------------

    def insert(self, chunk: str) -> bool:
        chunk = T.sanitise(chunk)
        if not chunk or len(self.text) + len(chunk) > self.limit:
            return False
        self.text = self.text[:self.cursor] + chunk + self.text[self.cursor:]
        self.cursor += len(chunk)
        return True

    def backspace(self) -> bool:
        if self.cursor == 0:
            return False
        self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1
        return True

    def delete(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        return True

    def delete_word(self) -> bool:
        """Ctrl+W: rub out the word before the caret."""
        if self.cursor == 0:
            return False
        index = self.cursor
        while index > 0 and self.text[index - 1] == " ":
            index -= 1
        while index > 0 and self.text[index - 1] != " ":
            index -= 1
        self.text = self.text[:index] + self.text[self.cursor:]
        self.cursor = index
        return True

    def clear(self) -> bool:
        if not self.text:
            return False
        self.text = ""
        self.cursor = 0
        self.offset = 0
        return True

    def move(self, delta: int) -> bool:
        target = max(0, min(len(self.text), self.cursor + delta))
        if target == self.cursor:
            return False
        self.cursor = target
        return True

    def move_word(self, direction: int) -> bool:
        index = self.cursor
        if direction < 0:
            while index > 0 and self.text[index - 1] == " ":
                index -= 1
            while index > 0 and self.text[index - 1] != " ":
                index -= 1
        else:
            length = len(self.text)
            while index < length and self.text[index] != " ":
                index += 1
            while index < length and self.text[index] == " ":
                index += 1
        if index == self.cursor:
            return False
        self.cursor = index
        return True

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def set(self, value: str) -> None:
        self.text = T.sanitise(value)[:self.limit]
        self.cursor = len(self.text)
        self.offset = 0

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    # -- drawing ------------------------------------------------------------

    def render(self, width: int) -> Tuple[str, int, bool, bool]:
        """Return ``(visible_text, caret_column, cut_left, cut_right)``.

        ``caret_column`` is relative to the start of the visible text.  The two
        flags say whether text is hidden off either edge, so the caller can
        mark it -- at this width the operator otherwise has no way to know the
        line continues.
        """
        if width <= 0:
            return "", 0, False, False
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor > self.offset + width - 1:
            self.offset = self.cursor - width + 1
        self.offset = max(0, min(self.offset, max(0, len(self.text))))

        visible = self.text[self.offset:self.offset + width]
        caret = self.cursor - self.offset
        cut_left = self.offset > 0
        cut_right = self.offset + width < len(self.text)
        return visible, max(0, min(width - 1, caret)), cut_left, cut_right


class Activity:
    """Drives the animated state glyph and any activity rail.

    Animation phase is derived from wall-clock time rather than a frame
    counter, so the motion stays at a constant real-world rate whether the
    render loop is keeping up or the bus is saturated by a long reply.
    """

    def __init__(self, fps: float = 8.0):
        self.fps = fps
        self.started = time.monotonic()

    def reset(self) -> None:
        self.started = time.monotonic()

    @property
    def phase(self) -> int:
        return int((time.monotonic() - self.started) * self.fps)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def iris(self, display: Display, slot: str = "state") -> str:
        display.set_glyph(slot, G.iris(self.phase))
        return display.g(slot)

    def spinner(self, display: Display, slot: str = "spin") -> str:
        display.set_glyph(slot, G.spinner(self.phase))
        return display.g(slot)

    def equalizer(self, display: Display, slot: str = "activity",
                  seed: int = 0) -> str:
        display.set_glyph(slot, G.equalizer(self.phase, seed))
        return display.g(slot)

    def marquee_rail(self, display: Display, slot: str = "activity") -> str:
        display.set_glyph(slot, G.marquee(self.phase))
        return display.g(slot)

    def still(self, display: Display, slot: str = "state", phase: int = 0) -> str:
        display.set_glyph(slot, G.iris(phase))
        return display.g(slot)


class Toast:
    """A transient one-row message overlaid on the bottom of the screen."""

    def __init__(self) -> None:
        self.message = ""
        self.until = 0.0
        self.sticky = False

    def show(self, message: str, seconds: float = 2.0) -> None:
        self.message = message.upper()
        self.until = time.monotonic() + seconds
        self.sticky = False

    def pin(self, message: str) -> None:
        self.message = message.upper()
        self.sticky = True

    def clear(self) -> None:
        self.message = ""
        self.sticky = False
        self.until = 0.0

    @property
    def active(self) -> bool:
        return bool(self.message) and (self.sticky or time.monotonic() < self.until)

    def draw(self, frame: Frame, row: Optional[int] = None) -> None:
        if not self.active:
            return
        row = frame.rows - 1 if row is None else row
        frame.row_text(row, T.fit(self.message, frame.cols, align="center"))


class Ticker:
    """Cycles a set of short strings through one slot of the status row."""

    def __init__(self, interval: float = 2.5):
        self.interval = interval
        self.items: List[str] = []
        self._started = time.monotonic()

    def set(self, items: Sequence[str]) -> None:
        items = [i for i in items if i]
        if items != self.items:
            self.items = list(items)
            self._started = time.monotonic()

    def current(self) -> str:
        if not self.items:
            return ""
        index = int((time.monotonic() - self._started) / self.interval)
        return self.items[index % len(self.items)]


def draw_gauge(display: Display, frame: Frame, row: int, col: int,
               width: int, fraction: float, half_glyph: str = "half") -> None:
    """Horizontal bar gauge at half-cell resolution."""
    bar = T.progress_bar(fraction, width, G.ROM_FULL_BLOCK,
                         display.g(half_glyph), " ")
    frame.text(row, col, bar)


def draw_title(frame: Frame, title: str, right: str = "", row: int = 0,
               rule: str = "\xff") -> None:
    """A title bar: inverse-looking block ends with the title between them.

    There is no inverse video on a character panel, so a title is framed with
    solid blocks instead.  It reads as a header at a glance, which is the whole
    job, and it costs two columns rather than a whole row.
    """
    cols = frame.cols
    frame.clear_row(row)
    body = f" {title.upper()} "
    if right:
        body = f" {title.upper()} "
        available = cols - 2 - len(right) - 1
        body = T.fit(body, max(0, available))
        frame.text(row, 1, body)
        frame.text(row, cols - 1 - len(right), right)
    else:
        frame.text(row, 1, T.fit(body, cols - 2))
    frame.text(row, 0, rule)
    frame.text(row, cols - 1, rule)
