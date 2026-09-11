"""
A cycle-faithful-enough HD44780 emulator driven by PCF8574 port bytes.

This is not a convenience mock.  It decodes exactly what the driver puts on the
wire -- enable-pin edges, four-bit nibble pairs, the eight-bit-mode handshake
that precedes the switch to four-bit, DDRAM address arithmetic, CGRAM loads --
and maintains the controller state that results.  Running the program against
this emulator therefore exercises the same code path that runs against real
silicon, which is the only way to have any confidence in a display driver
without the display in front of you.

The emulator also renders, at the pixel level, what the panel would physically
show, so the simulator is a real preview of the layout rather than an
approximation of it.
"""

from __future__ import annotations

from typing import List, Optional

# PCF8574 backpack pin assignment.  This is the near-universal wiring used by
# the Freenove / "LCM1602" style modules: the low nibble carries the control
# lines and the high nibble carries D4..D7.
PIN_RS = 0x01
PIN_RW = 0x02
PIN_EN = 0x04
PIN_BACKLIGHT = 0x08


class HD44780Emulator:
    """Controller state machine fed one PCF8574 port byte at a time."""

    def __init__(self, cols: int = 20, rows: int = 4):
        self.cols = cols
        self.rows = rows
        # Row start addresses.  Note these are *not* in visual order: the
        # controller lays out a 20x4 panel as two 40-character logical lines,
        # each split in half.  Getting this wrong is the classic symptom where
        # text wraps from row 1 into row 3.
        self.row_offsets = self._row_offsets(cols, rows)

        self.ddram = bytearray(0x68)
        self.cgram = bytearray(64)
        self.address = 0
        self.in_cgram = False
        self.increment = True
        self.display_on = True
        self.cursor_on = False
        self.blink_on = False
        self.backlight = True
        self.four_bit = False
        self.two_line = True

        self._en_high = False
        self._pending_nibble: Optional[int] = None
        self._pending_rs = 0

        # Diagnostics: how much traffic the renderer actually generated.
        self.bytes_written = 0
        self.commands = 0
        self.data_writes = 0

    @staticmethod
    def _row_offsets(cols: int, rows: int) -> List[int]:
        if rows == 1:
            return [0x00]
        if rows == 2:
            return [0x00, 0x40]
        return [0x00, 0x40, 0x00 + cols, 0x40 + cols]

    # -- wire level ---------------------------------------------------------

    def write_port(self, value: int) -> None:
        """Consume one byte as latched onto the PCF8574 output port."""
        self.bytes_written += 1
        self.backlight = bool(value & PIN_BACKLIGHT)
        en = bool(value & PIN_EN)
        # The controller latches on the falling edge of E.
        if self._en_high and not en:
            self._latch(value)
        self._en_high = en

    def write_bytes(self, values) -> None:
        for value in values:
            self.write_port(value)

    def _latch(self, value: int) -> None:
        if value & PIN_RW:
            return  # reads are never issued by this driver
        rs = value & PIN_RS
        nibble = (value >> 4) & 0x0F

        if not self.four_bit:
            # During the power-on handshake the controller is still in eight-bit
            # mode and interprets a single transfer as the high nibble of a
            # command with the low nibble zeroed.
            self._execute(nibble << 4, rs)
            return

        if self._pending_nibble is None:
            self._pending_nibble = nibble
            self._pending_rs = rs
            return

        byte = (self._pending_nibble << 4) | nibble
        self._pending_nibble = None
        self._execute(byte, self._pending_rs if rs == self._pending_rs else rs)

    # -- instruction set ----------------------------------------------------

    def _execute(self, byte: int, rs: int) -> None:
        if rs:
            self.data_writes += 1
            self._write_ram(byte)
            return

        self.commands += 1
        if byte & 0x80:
            self.address = byte & 0x7F
            self.in_cgram = False
        elif byte & 0x40:
            self.address = byte & 0x3F
            self.in_cgram = True
        elif byte & 0x20:
            was_four_bit = self.four_bit
            self.four_bit = not (byte & 0x10)
            self.two_line = bool(byte & 0x08)
            if not was_four_bit and self.four_bit:
                # The transfer that selects four-bit mode is itself a single
                # nibble; anything buffered before it is stale.
                self._pending_nibble = None
        elif byte & 0x10:
            pass  # cursor/display shift -- unused by this driver
        elif byte & 0x08:
            self.display_on = bool(byte & 0x04)
            self.cursor_on = bool(byte & 0x02)
            self.blink_on = bool(byte & 0x01)
        elif byte & 0x04:
            self.increment = bool(byte & 0x02)
        elif byte & 0x02:
            self.address = 0
            self.in_cgram = False
        elif byte & 0x01:
            self.ddram = bytearray(b" " * len(self.ddram))
            self.address = 0
            self.in_cgram = False

    def _write_ram(self, byte: int) -> None:
        if self.in_cgram:
            self.cgram[self.address % 64] = byte & 0x1F
            self.address = (self.address + (1 if self.increment else -1)) % 64
        else:
            if self.address < len(self.ddram):
                self.ddram[self.address] = byte
            step = 1 if self.increment else -1
            self.address = (self.address + step) % len(self.ddram)

    # -- observation --------------------------------------------------------

    def text_screen(self) -> List[str]:
        """The visible characters, one string per row (CGRAM codes preserved)."""
        out = []
        for row in range(self.rows):
            base = self.row_offsets[row]
            chars = [chr(self.ddram[(base + c) % len(self.ddram)])
                     for c in range(self.cols)]
            out.append("".join(chars))
        return out

    def readable_screen(self, placeholder: str = "@") -> List[str]:
        """Like :meth:`text_screen` but with CGRAM codes replaced for printing."""
        rows = []
        for line in self.text_screen():
            rows.append("".join(
                placeholder if ord(c) < 0x20 else (c if ord(c) < 0x7F else "?")
                for c in line
            ))
        return rows

    def cgram_glyph(self, slot: int) -> List[int]:
        base = (slot % 8) * 8
        return list(self.cgram[base:base + 8])

    def pixel_screen(self) -> List[List[int]]:
        """Render the panel as a bitmap of ``rows*8`` by ``cols*5`` pixels.

        Character cells are drawn without the inter-character gap; the caller
        can insert one if it wants the authentic dot-matrix look.
        """
        if not self.display_on:
            return [[0] * (self.cols * 5) for _ in range(self.rows * 8)]

        bitmap = [[0] * (self.cols * 5) for _ in range(self.rows * 8)]
        cursor_addr = self.address
        for row in range(self.rows):
            base = self.row_offsets[row]
            for col in range(self.cols):
                addr = (base + col) % len(self.ddram)
                ch = self.ddram[addr]
                pattern = self._pattern_for(ch)
                for y in range(8):
                    bits = pattern[y]
                    for x in range(5):
                        if bits & (1 << (4 - x)):
                            bitmap[row * 8 + y][col * 5 + x] = 1
                if self.cursor_on and addr == cursor_addr:
                    for x in range(5):
                        bitmap[row * 8 + 7][col * 5 + x] = 1
                if self.blink_on and addr == cursor_addr:
                    for y in range(8):
                        for x in range(5):
                            bitmap[row * 8 + y][col * 5 + x] ^= 1
        return bitmap

    def _pattern_for(self, ch: int) -> List[int]:
        if ch < 16:
            return self.cgram_glyph(ch % 8)
        return _FONT_A00.get(ch, _FONT_UNKNOWN)


# --------------------------------------------------------------------------
# A partial HD44780 A00 character ROM.
#
# Only the printable ASCII range plus the handful of high codepoints this
# program uses are defined; everything else renders as a filled box, which is
# exactly what the real panel does for an undefined glyph.
# --------------------------------------------------------------------------

def _glyph(*rows: str) -> List[int]:
    out = []
    for row in rows:
        value = 0
        for i, ch in enumerate(row[:5]):
            if ch != " ":
                value |= 1 << (4 - i)
        out.append(value)
    while len(out) < 8:
        out.append(0)
    return out


_FONT_UNKNOWN = [0b11111] * 7 + [0]

# A compact 5x7 ASCII font.  Each entry is seven rows of five columns.
_ASCII_5x7 = {
    " ": ("     ", "     ", "     ", "     ", "     ", "     ", "     "),
    "!": ("  #  ", "  #  ", "  #  ", "  #  ", "     ", "  #  ", "     "),
    '"': (" # # ", " # # ", "     ", "     ", "     ", "     ", "     "),
    "#": (" # # ", " # # ", "#####", " # # ", "#####", " # # ", " # # "),
    "$": ("  #  ", " ####", "# #  ", " ### ", "  # #", "#### ", "  #  "),
    "%": ("##   ", "##  #", "   # ", "  #  ", " #   ", "#  ##", "   ##"),
    "&": (" ##  ", "#  # ", " ##  ", " ##  ", "#  # ", "#   #", " ### "),
    "'": ("  #  ", "  #  ", "     ", "     ", "     ", "     ", "     "),
    "(": ("   # ", "  #  ", " #   ", " #   ", " #   ", "  #  ", "   # "),
    ")": (" #   ", "  #  ", "   # ", "   # ", "   # ", "  #  ", " #   "),
    "*": ("     ", "  #  ", "# # #", " ### ", "# # #", "  #  ", "     "),
    "+": ("     ", "  #  ", "  #  ", "#####", "  #  ", "  #  ", "     "),
    ",": ("     ", "     ", "     ", "     ", "  ## ", "  #  ", " #   "),
    "-": ("     ", "     ", "     ", "#####", "     ", "     ", "     "),
    ".": ("     ", "     ", "     ", "     ", "     ", "  ## ", "  ## "),
    "/": ("    #", "   # ", "  #  ", "  #  ", " #   ", "#    ", "     "),
    "0": (" ### ", "#   #", "#  ##", "# # #", "##  #", "#   #", " ### "),
    "1": ("  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "2": (" ### ", "#   #", "    #", "   # ", "  #  ", " #   ", "#####"),
    "3": ("#####", "   # ", "  #  ", "   # ", "    #", "#   #", " ### "),
    "4": ("   # ", "  ## ", " # # ", "#  # ", "#####", "   # ", "   # "),
    "5": ("#####", "#    ", "#### ", "    #", "    #", "#   #", " ### "),
    "6": ("  ## ", " #   ", "#    ", "#### ", "#   #", "#   #", " ### "),
    "7": ("#####", "    #", "   # ", "  #  ", " #   ", " #   ", " #   "),
    "8": (" ### ", "#   #", "#   #", " ### ", "#   #", "#   #", " ### "),
    "9": (" ### ", "#   #", "#   #", " ####", "    #", "   # ", " ##  "),
    ":": ("     ", "  ## ", "  ## ", "     ", "  ## ", "  ## ", "     "),
    ";": ("     ", "  ## ", "  ## ", "     ", "  ## ", "  #  ", " #   "),
    "<": ("   # ", "  #  ", " #   ", "#    ", " #   ", "  #  ", "   # "),
    "=": ("     ", "     ", "#####", "     ", "#####", "     ", "     "),
    ">": (" #   ", "  #  ", "   # ", "    #", "   # ", "  #  ", " #   "),
    "?": (" ### ", "#   #", "    #", "   # ", "  #  ", "     ", "  #  "),
    "@": (" ### ", "#   #", "    #", " ## #", "# # #", "# # #", " ### "),
    "A": ("  #  ", " # # ", "#   #", "#   #", "#####", "#   #", "#   #"),
    "B": ("#### ", "#   #", "#   #", "#### ", "#   #", "#   #", "#### "),
    "C": (" ### ", "#   #", "#    ", "#    ", "#    ", "#   #", " ### "),
    "D": ("###  ", "#  # ", "#   #", "#   #", "#   #", "#  # ", "###  "),
    "E": ("#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"),
    "F": ("#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#    "),
    "G": (" ### ", "#   #", "#    ", "#  ##", "#   #", "#   #", " ####"),
    "H": ("#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"),
    "I": (" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "J": ("    #", "    #", "    #", "    #", "#   #", "#   #", " ### "),
    "K": ("#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"),
    "L": ("#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"),
    "M": ("#   #", "## ##", "# # #", "# # #", "#   #", "#   #", "#   #"),
    "N": ("#   #", "#   #", "##  #", "# # #", "#  ##", "#   #", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "),
    "Q": (" ### ", "#   #", "#   #", "#   #", "# # #", "#  # ", " ## #"),
    "R": ("#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"),
    "S": (" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "),
    "U": ("#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "V": ("#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "),
    "W": ("#   #", "#   #", "#   #", "# # #", "# # #", "## ##", "#   #"),
    "X": ("#   #", "#   #", " # # ", "  #  ", " # # ", "#   #", "#   #"),
    "Y": ("#   #", "#   #", " # # ", "  #  ", "  #  ", "  #  ", "  #  "),
    "Z": ("#####", "    #", "   # ", "  #  ", " #   ", "#    ", "#####"),
    "[": (" perc", "     ", "     ", "     ", "     ", "     ", "     "),
    "\\": ("#    ", "#    ", " #   ", "  #  ", "   # ", "    #", "    #"),
    "]": ("     ", "     ", "     ", "     ", "     ", "     ", "     "),
    "^": ("  #  ", " # # ", "#   #", "     ", "     ", "     ", "     "),
    "_": ("     ", "     ", "     ", "     ", "     ", "     ", "#####"),
    "`": (" #   ", "  #  ", "     ", "     ", "     ", "     ", "     "),
    "a": ("     ", "     ", " ### ", "    #", " ####", "#   #", " ####"),
    "b": ("#    ", "#    ", "#### ", "#   #", "#   #", "#   #", "#### "),
    "c": ("     ", "     ", " ####", "#    ", "#    ", "#    ", " ####"),
    "d": ("    #", "    #", " ####", "#   #", "#   #", "#   #", " ####"),
    "e": ("     ", "     ", " ### ", "#   #", "#####", "#    ", " ### "),
    "f": ("  ## ", " #  #", " #   ", "#### ", " #   ", " #   ", " #   "),
    "g": ("     ", " ####", "#   #", "#   #", " ####", "    #", " ### "),
    "h": ("#    ", "#    ", "#### ", "#   #", "#   #", "#   #", "#   #"),
    "i": ("  #  ", "     ", " ##  ", "  #  ", "  #  ", "  #  ", " ### "),
    "j": ("   # ", "     ", "  ## ", "   # ", "   # ", "#  # ", " ##  "),
    "k": ("#    ", "#    ", "#  # ", "# #  ", "##   ", "# #  ", "#  # "),
    "l": (" ##  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "m": ("     ", "     ", "## # ", "# # #", "# # #", "#   #", "#   #"),
    "n": ("     ", "     ", "#### ", "#   #", "#   #", "#   #", "#   #"),
    "o": ("     ", "     ", " ### ", "#   #", "#   #", "#   #", " ### "),
    "p": ("     ", "     ", "#### ", "#   #", "#### ", "#    ", "#    "),
    "q": ("     ", "     ", " ####", "#   #", " ####", "    #", "    #"),
    "r": ("     ", "     ", "# ###", "##   ", "#    ", "#    ", "#    "),
    "s": ("     ", "     ", " ####", "#    ", " ### ", "    #", "#### "),
    "t": (" #   ", " #   ", "#### ", " #   ", " #   ", " #  #", "  ## "),
    "u": ("     ", "     ", "#   #", "#   #", "#   #", "#  ##", " ## #"),
    "v": ("     ", "     ", "#   #", "#   #", "#   #", " # # ", "  #  "),
    "w": ("     ", "     ", "#   #", "#   #", "# # #", "# # #", " # # "),
    "x": ("     ", "     ", "#   #", " # # ", "  #  ", " # # ", "#   #"),
    "y": ("     ", "     ", "#   #", "#   #", " ####", "    #", " ### "),
    "z": ("     ", "     ", "#####", "   # ", "  #  ", " #   ", "#####"),
    "{": ("   ##", "  #  ", "  #  ", " #   ", "  #  ", "  #  ", "   ##"),
    "|": ("  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "),
    "}": ("##   ", "  #  ", "  #  ", "   # ", "  #  ", "  #  ", "##   "),
    "~": ("     ", "     ", " #  #", "# # #", "#  # ", "     ", "     "),
}

_ASCII_5x7["["] = ("  ###", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  ###")
_ASCII_5x7["]"] = ("###  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "###  ")

_FONT_A00 = {ord(ch): _glyph(*rows) for ch, rows in _ASCII_5x7.items()}
_FONT_A00[0xFF] = [0b11111] * 8
_FONT_A00[0x7E] = _glyph("     ", "  #  ", "  ## ", "#####", "  ## ", "  #  ", "     ")
_FONT_A00[0x7F] = _glyph("     ", "  #  ", " ##  ", "#####", " ##  ", "  #  ", "     ")
_FONT_A00[0xDF] = _glyph(" ##  ", "#  # ", "#  # ", " ##  ", "     ", "     ", "     ")
_FONT_A00[0xA5] = _glyph("     ", "     ", "  ## ", "  ## ", "     ", "     ", "     ")
