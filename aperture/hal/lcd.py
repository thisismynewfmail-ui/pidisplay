"""
HD44780 driver for PCF8574-backpacked character panels (Freenove LCD2004 etc).

The driver deliberately exposes a *batched* interface.  Callers stage a series
of operations -- move the cursor, write a run of text, reprogram a CGRAM slot --
and then flush, at which point every staged byte goes out as a small number of
I2C messages.  A naive per-character driver spends most of its time in bus
turnaround; this one spends it moving pixels.

Cost model, for anyone tuning the UI: one displayed character is six port
bytes, one cursor move is six, and one CGRAM slot reload is fifty-four.  At the
400 kHz bus speed install.sh configures, a full 20x4 repaint is about 11 ms and
a typical differential repaint is under 2 ms.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .pinmap import DEFAULT as DEFAULT_PINMAP, PinMap
from .transport import I2CTransport, Transport, TransportError

# Instruction set ------------------------------------------------------------
CMD_CLEAR = 0x01
CMD_HOME = 0x02
CMD_ENTRY_MODE = 0x04
CMD_DISPLAY_CTRL = 0x08
CMD_FUNCTION_SET = 0x20
CMD_SET_CGRAM = 0x40
CMD_SET_DDRAM = 0x80

ENTRY_INCREMENT = 0x02
DISPLAY_ON = 0x04
CURSOR_ON = 0x02
BLINK_ON = 0x01
FUNC_2LINE = 0x08
FUNC_5x8 = 0x00

#: Addresses PCF8574 backpacks are strapped to from the factory.  The PCF8574
#: lands at 0x20-0x27 and the PCF8574A at 0x38-0x3F; 0x27 and 0x3F are the two
#: you will meet in practice because they are the all-pins-high defaults.
CANDIDATE_ADDRESSES = (0x27, 0x3F, 0x26, 0x3E, 0x20, 0x21, 0x22, 0x23,
                       0x24, 0x25, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D)


def list_buses() -> List[int]:
    """Every I2C bus the kernel exposes, lowest first.

    On a Pi 5 this is not just bus 1: the RP1 southbridge presents several
    internal buses (HDMI DDC, camera, the RTC) alongside the GPIO header's,
    and which number the header lands on has moved between kernel releases.
    Scanning all of them means a header on an unexpected number is found
    rather than reported as absent.
    """
    import glob
    import re
    buses = []
    for path in glob.glob("/dev/i2c-*"):
        match = re.search(r"/dev/i2c-(\d+)$", path)
        if match:
            buses.append(int(match.group(1)))
    return sorted(buses)


def probe_addresses(bus: int = 1,
                    addresses: Sequence[int] = CANDIDATE_ADDRESSES) -> List[int]:
    """Return I2C addresses that answer on *bus*.

    Three probe methods are tried in the order ``i2cdetect`` prefers them,
    because no single one works on every adapter:

      1. SMBus quick write -- address plus the R/W bit and no data. This is
         what ``i2cdetect`` uses for this address range and it cannot disturb
         a PCF8574, whose outputs only latch on a real data byte.
      2. A one-byte read. A PCF8574 answers this with its current port state,
         so it is both safe and definitive.
      3. A zero-length ``I2C_RDWR`` write, as a last resort. Some adapters
         reject this outright, which is why it is not the first choice --
         relying on it alone made the probe report nothing on hardware that
         was working.
    """
    try:
        from smbus2 import SMBus, i2c_msg
    except ImportError:
        return []

    found = []
    try:
        with SMBus(bus) as smbus:
            for address in addresses:
                if _address_responds(smbus, i2c_msg, address):
                    found.append(address)
    except (OSError, IOError):
        return []
    return found


def _address_responds(smbus, i2c_msg, address: int) -> bool:
    for probe in (_probe_quick, _probe_read, _probe_empty_write):
        result = probe(smbus, i2c_msg, address)
        if result is True:
            return True
        if result is False:
            return False           # the method worked and the address is idle
    return False                   # every method was unsupported here


def _probe_quick(smbus, _i2c_msg, address: int):
    try:
        smbus.write_quick(address)
        return True
    except OSError as exc:
        import errno
        # ENXIO / EREMOTEIO mean nothing acknowledged: a real, useful answer.
        if exc.errno in (errno.ENXIO, errno.EREMOTEIO, errno.ETIMEDOUT):
            return False
        return None                # unsupported by this adapter; try the next
    except (AttributeError, TypeError):
        return None


def _probe_read(smbus, _i2c_msg, address: int):
    try:
        smbus.read_byte(address)
        return True
    except OSError as exc:
        import errno
        if exc.errno in (errno.ENXIO, errno.EREMOTEIO, errno.ETIMEDOUT):
            return False
        return None
    except (AttributeError, TypeError):
        return None


def _probe_empty_write(smbus, i2c_msg, address: int):
    try:
        smbus.i2c_rdwr(i2c_msg.write(address, b""))
        return True
    except OSError:
        return False
    except (AttributeError, TypeError, ValueError):
        return None


def scan_all_buses() -> List[Tuple[int, List[int]]]:
    """``(bus, addresses)`` for every bus, including those with nothing on."""
    return [(bus, probe_addresses(bus)) for bus in list_buses()]


class CharacterLCD:
    """A 20x4 (or other geometry) HD44780 panel behind a PCF8574."""

    def __init__(self, transport: Transport, cols: int = 20, rows: int = 4,
                 pinmap: Optional[PinMap] = None):
        self.transport = transport
        self.cols = cols
        self.rows = rows
        self.pinmap = pinmap or DEFAULT_PINMAP
        # Precomputed so the hot path does no bit shuffling per character.
        self._nibbles = self.pinmap.nibble_table()
        self.row_offsets = self._row_offsets(cols, rows)
        self._backlight = True
        self._display_ctrl = CMD_DISPLAY_CTRL | DISPLAY_ON
        self._pending: List[int] = []
        # The controller auto-increments its address counter, so consecutive
        # writes to consecutive cells need no cursor move.  Tracking where the
        # counter is lets the renderer skip those moves.
        self._address: Optional[int] = None
        self.initialised = False

    @staticmethod
    def _row_offsets(cols: int, rows: int) -> List[int]:
        if rows == 1:
            return [0x00]
        if rows == 2:
            return [0x00, 0x40]
        return [0x00, 0x40, 0x00 + cols, 0x40 + cols]

    # -- construction helpers ----------------------------------------------

    @classmethod
    def open_i2c(cls, bus: int = 1, address: int = 0x27, cols: int = 20,
                 rows: int = 4, autodetect: bool = True,
                 pinmap: Optional[PinMap] = None) -> "CharacterLCD":
        """Open a panel on a real bus, optionally hunting for its address.

        Autodetection exists because these modules ship strapped to either 0x27
        or 0x3F with no markings, and asking a first-time user to run
        ``i2cdetect`` before the program will start is a bad first impression.
        """
        if autodetect:
            found = probe_addresses(bus)
            if found and address not in found:
                address = found[0]
        lcd = cls(I2CTransport(bus=bus, address=address), cols=cols, rows=rows,
                  pinmap=pinmap)
        lcd.initialise()
        return lcd

    # -- byte encoding ------------------------------------------------------

    def _bl_bit(self) -> int:
        return self.pinmap.backlight_mask(self._backlight)

    def _encode(self, value: int, rs: int) -> List[int]:
        """One controller byte as six PCF8574 port bytes.

        Three writes per nibble: data stable, data+E, data again.  Collapsing
        this to two writes (the trick some Arduino libraries use) violates the
        40 ns address-setup time before E rises, because the expander switches
        every output at once.  Most panels tolerate it; the ones that do not
        fail as intermittent garbage characters, which is a miserable thing to
        debug.  The third write costs nothing worth having.
        """
        backlight = self._bl_bit()
        enable = self.pinmap.en_bit
        high = self._nibbles[(value >> 4) & 0x0F] | rs | backlight
        low = self._nibbles[value & 0x0F] | rs | backlight
        return [high, high | enable, high,
                low, low | enable, low]

    def _stage(self, data: Sequence[int]) -> None:
        self._pending.extend(data)

    def command(self, value: int) -> None:
        self._stage(self._encode(value, 0))
        self._address = None

    def _data(self, value: int) -> None:
        self._stage(self._encode(value, self.pinmap.rs_bit))

    def flush(self) -> None:
        """Send everything staged so far as one or more I2C bursts."""
        if not self._pending:
            return
        payload, self._pending = self._pending, []
        self.transport.write(payload)

    # -- power-on --------------------------------------------------------

    def initialise(self) -> None:
        """Run the datasheet's power-on sequence into four-bit mode.

        The controller wakes up in eight-bit mode and cannot be assumed to be
        in any particular state -- a warm restart of this program may find it
        mid-byte from the previous run.  The triple 0x30 resets that: whatever
        the internal nibble phase was, three eight-bit function-set commands
        leave it deterministic, and only then is it safe to select four-bit.
        """
        time.sleep(0.05)
        for delay in (0.0045, 0.0045, 0.00015):
            self._write_nibble(0x30)
            self.flush()
            time.sleep(delay)
        self._write_nibble(0x20)  # four-bit mode from here on
        self.flush()
        time.sleep(0.00015)

        self.command(CMD_FUNCTION_SET | FUNC_2LINE | FUNC_5x8)
        self.command(CMD_DISPLAY_CTRL)  # display off while we set up
        self.command(CMD_CLEAR)
        self.flush()
        time.sleep(0.002)  # clear needs 1.52 ms and does not ack
        self.command(CMD_ENTRY_MODE | ENTRY_INCREMENT)
        self._display_ctrl = CMD_DISPLAY_CTRL | DISPLAY_ON
        self.command(self._display_ctrl)
        self.flush()
        self._address = None
        self.initialised = True

    def _write_nibble(self, value: int) -> None:
        """Send a single four-bit transfer, as the power-on handshake needs."""
        backlight = self._bl_bit()
        byte = self._nibbles[(value >> 4) & 0x0F] | backlight
        self._stage([byte, byte | self.pinmap.en_bit, byte])

    # -- drawing -----------------------------------------------------------

    def clear(self) -> None:
        self.command(CMD_CLEAR)
        self.flush()
        time.sleep(0.002)
        self._address = 0

    def set_cursor(self, row: int, col: int) -> None:
        address = self.row_offsets[row] + col
        if self._address == address:
            return
        self.command(CMD_SET_DDRAM | address)
        self._address = address

    def write_text(self, text: str) -> None:
        """Write *text* at the current address, advancing the counter."""
        for ch in text:
            self._data(ord(ch) & 0xFF)
        if self._address is not None:
            self._address += len(text)

    def write_at(self, row: int, col: int, text: str) -> None:
        self.set_cursor(row, col)
        self.write_text(text)

    def load_glyph(self, slot: int, pattern: Sequence[int]) -> None:
        """Reprogram one CGRAM slot.

        Writing CGRAM moves the address counter into character memory, so the
        DDRAM position is lost and the next draw must re-address.  Callers that
        batch glyph loads with text should load glyphs first.
        """
        self.command(CMD_SET_CGRAM | ((slot & 0x07) << 3))
        for row in range(8):
            self._data(pattern[row] & 0x1F)
        self._address = None

    # -- panel state -------------------------------------------------------

    @property
    def backlight(self) -> bool:
        return self._backlight

    @backlight.setter
    def backlight(self, value: bool) -> None:
        value = bool(value)
        if value == self._backlight:
            return
        self._backlight = value
        # The backlight bit rides along with every transfer, so it takes effect
        # on the next byte regardless; sending a no-op keeps it immediate.
        self._stage([self._bl_bit()])
        self.flush()

    def set_backlight_raw(self, on: bool) -> None:
        """Drive only the backlight pin, bypassing the controller entirely.

        This is the one operation that depends on no part of the HD44780
        protocol -- not the pin mapping for RS, E or the data lines, not the
        four-bit handshake, not contrast. If this makes the backlight change,
        the bus, the address, the wiring and the power are all proven good and
        the fault lies further up. The diagnostic in main.py bisects on exactly
        that.
        """
        self._backlight = bool(on)
        self._pending = []
        self._stage([self._bl_bit()])
        self.flush()

    def set_cursor_style(self, visible: bool = False, blinking: bool = False) -> None:
        """Enable the controller's own cursor.

        Used for the compose-line caret.  Letting the controller blink it costs
        no bus traffic and no frames, and it blinks at a rate that reads as
        native because it is.
        """
        ctrl = CMD_DISPLAY_CTRL | DISPLAY_ON
        if visible:
            ctrl |= CURSOR_ON
        if blinking:
            ctrl |= BLINK_ON
        if ctrl == self._display_ctrl:
            return
        self._display_ctrl = ctrl
        self.command(ctrl)

    def set_display_on(self, on: bool) -> None:
        ctrl = self._display_ctrl
        ctrl = (ctrl | DISPLAY_ON) if on else (ctrl & ~DISPLAY_ON)
        if ctrl == self._display_ctrl:
            return
        self._display_ctrl = ctrl
        self.command(ctrl)
        self.flush()

    def close(self) -> None:
        try:
            self.flush()
        except TransportError:
            pass
        try:
            self.transport.close()
        except Exception:
            pass
