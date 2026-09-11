"""
PCF8574-to-HD44780 pin mappings.

A PCF8574 backpack is just eight output pins wired to the display's control and
data lines, and *which pin goes where is a property of the board, not of the
protocol*.  Two mappings exist in the wild and they are electrically
incompatible: a module wired one way, driven the other way, receives perfectly
valid I2C traffic and does nothing at all.  The backlight may even work, because
that bit happens to be independent.

This is the single hardest fault to diagnose from software, because every layer
above it reports success.  So the mapping is data here rather than constants
baked into the driver, and :mod:`aperture.hal.emulator` decodes through the same
table -- which means a test can drive one mapping and decode with another, and
prove the two really are distinguishable.

The mappings:

  ``standard``  P0=RS P1=RW P2=E P3=LED P4..P7=D4..D7
                The "LCM1602"/"Arduino-IIC-LCD1602" layout. The overwhelming
                majority of modules, including the Freenove LCD2004.

  ``ywrobot``   P0..P3=D4..D7 P4=E P5=RW P6=RS P7=LED
                Sold under the YwRobot name among others. This is the mapping
                the Arduino ``LiquidCrystal_I2C`` library exposes through its
                long-form constructor, which is why that constructor exists.

Backlight polarity is separate again: nearly all modules drive the LED pin
active-high, but a few invert it, which shows up as a backlight that is on
when the software thinks it is off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class PinMap:
    """Which PCF8574 output pin carries which display signal."""

    name: str
    rs: int
    rw: int
    en: int
    backlight: int
    #: Bit positions carrying D4, D5, D6, D7 in that order.
    data: Tuple[int, int, int, int]
    backlight_active_low: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        pins = [self.rs, self.rw, self.en, self.backlight, *self.data]
        if sorted(pins) != list(range(8)):
            raise ValueError(
                f"pin map {self.name!r} must use each of P0-P7 exactly once, "
                f"got {sorted(pins)}")

    # -- derived bit masks -------------------------------------------------

    @property
    def rs_bit(self) -> int:
        return 1 << self.rs

    @property
    def rw_bit(self) -> int:
        return 1 << self.rw

    @property
    def en_bit(self) -> int:
        return 1 << self.en

    @property
    def backlight_bit(self) -> int:
        return 1 << self.backlight

    def backlight_mask(self, on: bool) -> int:
        """The bits to OR into every byte for the requested backlight state."""
        lit = on != self.backlight_active_low
        return self.backlight_bit if lit else 0

    def encode_nibble(self, nibble: int) -> int:
        """Scatter a four-bit data nibble across this map's data pins."""
        out = 0
        for index in range(4):
            if nibble & (1 << index):
                out |= 1 << self.data[index]
        return out

    def decode_nibble(self, port: int) -> int:
        """The inverse of :meth:`encode_nibble`, for the emulator."""
        out = 0
        for index in range(4):
            if port & (1 << self.data[index]):
                out |= 1 << index
        return out

    def nibble_table(self) -> List[int]:
        """A 16-entry lookup, so the hot path does no bit shuffling."""
        return [self.encode_nibble(n) for n in range(16)]

    def pin_summary(self) -> str:
        pins = ["?"] * 8
        pins[self.rs] = "RS"
        pins[self.rw] = "RW"
        pins[self.en] = "E"
        pins[self.backlight] = "LED"
        for index, bit in enumerate(self.data):
            pins[bit] = f"D{index + 4}"
        return " ".join(f"P{bit}={name}" for bit, name in enumerate(pins))


STANDARD = PinMap(
    name="standard", rs=0, rw=1, en=2, backlight=3, data=(4, 5, 6, 7),
    description="LCM1602 layout; nearly all modules including Freenove")

YWROBOT = PinMap(
    name="ywrobot", rs=6, rw=5, en=4, backlight=7, data=(0, 1, 2, 3),
    description="YwRobot layout; data on the low nibble")

STANDARD_INVERTED = PinMap(
    name="standard-inv", rs=0, rw=1, en=2, backlight=3, data=(4, 5, 6, 7),
    backlight_active_low=True,
    description="LCM1602 layout with an active-low backlight")

YWROBOT_INVERTED = PinMap(
    name="ywrobot-inv", rs=6, rw=5, en=4, backlight=7, data=(0, 1, 2, 3),
    backlight_active_low=True,
    description="YwRobot layout with an active-low backlight")

#: Every mapping, in the order the diagnostic should try them: most likely
#: first, so the common case is answered on the first question.
ALL: List[PinMap] = [STANDARD, YWROBOT, STANDARD_INVERTED, YWROBOT_INVERTED]

BY_NAME: Dict[str, PinMap] = {m.name: m for m in ALL}
NAMES: Tuple[str, ...] = tuple(m.name for m in ALL)

DEFAULT = STANDARD


def get(name: str) -> PinMap:
    """Look up a mapping by name, falling back to the common one."""
    return BY_NAME.get(str(name), DEFAULT)
