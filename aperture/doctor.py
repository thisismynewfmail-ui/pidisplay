"""
Interactive hardware diagnosis for a panel that shows nothing.

A blank display has several possible causes and, from software, they all look
identical: the I2C writes succeed and the screen stays dark.  Guessing between
them wastes an afternoon.  This walks the operator through a bisection that
eliminates them in order, asking only questions a person can answer by looking
at the panel.

The bisection turns on one observation.  Of the eight PCF8574 output pins, the
backlight pin is the only one whose effect requires *no* part of the HD44780
protocol: no four-bit handshake, no enable-pin timing, no register select, no
contrast.  Writing a single byte toggles it.  So:

    backlight responds  ->  bus, address, wiring, power and ground are all
                            proven good, and the fault is above the bus:
                            pin mapping or contrast.

    backlight does not  ->  the fault is at or below the bus, and no amount of
                            protocol work will help.

Everything after that first question is a consequence of the answer.
"""

from __future__ import annotations

import sys
import time
from typing import List, Optional, Sequence, Tuple

from .hal import pinmap as pinmaps
from .hal.lcd import (CANDIDATE_ADDRESSES, CharacterLCD, list_buses,
                      probe_addresses)
from .hal.pinmap import PinMap
from .hal.transport import I2CTransport, TransportError

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def _heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(DIM + "-" * len(text) + RESET)


def _good(text: str) -> None:
    print(f"  {GREEN}OK{RESET}   {text}")


def _bad(text: str) -> None:
    print(f"  {RED}FAIL{RESET} {text}")


def _note(text: str) -> None:
    print(f"       {text}")


def _ask(question: str, default: Optional[bool] = None) -> bool:
    """Yes/no question.  Returns *default* when there is no terminal."""
    if not sys.stdin.isatty():
        if default is None:
            raise SystemExit(
                "--doctor needs a terminal to ask questions. Run it directly.")
        return default
    hint = "[y/n]" if default is None else ("[Y/n]" if default else "[y/N]")
    while True:
        try:
            reply = input(f"\n  {BOLD}{question}{RESET} {hint} ").strip().lower()
        except EOFError:
            raise SystemExit("\naborted")
        if not reply and default is not None:
            return default
        if reply.startswith("y"):
            return True
        if reply.startswith("n"):
            return False


def _choose(prompt: str, options: Sequence[str]) -> int:
    for index, option in enumerate(options, start=1):
        print(f"    {index}. {option}")
    while True:
        try:
            reply = input(f"\n  {BOLD}{prompt}{RESET} [1-{len(options)}] ").strip()
        except EOFError:
            raise SystemExit("\naborted")
        if reply.isdigit() and 1 <= int(reply) <= len(options):
            return int(reply) - 1


# --------------------------------------------------------------------------
# Stage 1: is anything on the bus at all
# --------------------------------------------------------------------------

def find_candidates() -> List[Tuple[int, int]]:
    """Every (bus, address) pair that looks like a display backpack."""
    found: List[Tuple[int, int]] = []
    for bus in list_buses():
        for address in probe_addresses(bus):
            found.append((bus, address))
    return found


def report_buses() -> List[Tuple[int, int]]:
    _heading("1. The I2C bus")
    buses = list_buses()
    if not buses:
        _bad("no /dev/i2c-* devices exist at all")
        _note("I2C is not enabled, or the kernel module is not loaded.")
        _note("Fix with:  sudo raspi-config nonint do_i2c 0  &&  sudo reboot")
        _note("Then check:  ls /dev/i2c-*")
        return []
    _good(f"kernel exposes bus{'es' if len(buses) > 1 else ''}: "
          + ", ".join(str(b) for b in buses))

    candidates = find_candidates()
    if not candidates:
        _bad("no device answered on any bus")
        _note("Probed addresses: " +
              ", ".join(f"0x{a:02X}" for a in CANDIDATE_ADDRESSES[:6]) + ", ...")
        _note("")
        _note("This is a wiring or power fault, not a software one. In order of")
        _note("how often each is the cause:")
        _note("  1. No ground between the panel and the Pi. Header pin 6.")
        _note("     The backlight runs off VCC, so the panel can look alive")
        _note("     with no ground at all -- this is the usual trap.")
        _note("  2. SDA and SCL swapped. SDA is pin 3, SCL is pin 5.")
        _note("  3. A jumper not fully seated, or a broken wire. Reseat all four.")
        _note("  4. VCC not connected. Header pin 2 or 4 for 5 V.")
        _note("")
        _note("Cross-check independently with:  i2cdetect -y 1")
        return []

    for bus, address in candidates:
        likely = "  (typical display backpack)" if address in (0x27, 0x3F) else ""
        _good(f"bus {bus}, address 0x{address:02X} responds{likely}")
    return candidates


# --------------------------------------------------------------------------
# Stage 2: the backlight bisection
# --------------------------------------------------------------------------

def blink_backlight(bus: int, address: int, on_byte: int, off_byte: int,
                    times: int = 4, period: float = 0.6) -> None:
    """Toggle only the backlight pin.  Touches no other part of the protocol."""
    transport = I2CTransport(bus=bus, address=address)
    try:
        for _ in range(times):
            transport.write([on_byte])
            time.sleep(period / 2)
            transport.write([off_byte])
            time.sleep(period / 2)
        transport.write([on_byte])          # leave it lit
    finally:
        transport.close()


def backlight_pins() -> List[Tuple[int, List[str]]]:
    """Distinct backlight pin positions, with the layouts that use each.

    The two layouts put the LED on different pins -- P3 for the standard one,
    P7 for the YwRobot one -- so finding which pin responds halves the search
    with a test that contrast cannot confound.
    """
    order: List[Tuple[int, List[str]]] = []
    for mapping in pinmaps.ALL:
        for pin, names in order:
            if pin == mapping.backlight:
                if mapping.name not in names:
                    names.append(mapping.name)
                break
        else:
            order.append((mapping.backlight, [mapping.name]))
    return order


def find_backlight_pin(bus: int, address: int) -> Optional[int]:
    """Which PCF8574 pin drives the backlight, or None if none does."""
    for pin, names in backlight_pins():
        bit = 1 << pin
        print()
        _note(f"Blinking P{pin} -- used by: {', '.join(names)}")
        try:
            blink_backlight(bus, address, bit, 0x00)
        except TransportError as exc:
            _bad(f"the write itself failed: {exc}")
            return None
        if _ask("Did the backlight blink off and on?"):
            _good(f"the backlight is on P{pin}")
            return pin
    return None


def find_backlight_polarity(bus: int, address: int, pin: int) -> bool:
    """True if the backlight is active-low.

    Polarity cannot be read off a blink: driving an active-low pin with
    active-high bytes still blinks, just in antiphase. The only way to tell
    them apart is to hold the pin at one level and ask what the panel is
    doing -- so that is what this does.
    """
    bit = 1 << pin
    transport = I2CTransport(bus=bus, address=address)
    try:
        transport.write([bit])
    except TransportError as exc:
        _bad(f"the write failed: {exc}")
        return False
    finally:
        transport.close()
    time.sleep(0.4)
    if _ask("Is the backlight lit right now?", default=True):
        return False
    _note("Then this module drives its backlight inverted.")
    return True


def mapping_for_backlight(pin: int, active_low: bool) -> Optional[PinMap]:
    for mapping in pinmaps.ALL:
        if mapping.backlight == pin and mapping.backlight_active_low == active_low:
            return mapping
    return None


def check_backlight(bus: int, address: int) -> Optional[PinMap]:
    """Find the backlight wiring, and so narrow down the mapping.

    Returns the mapping this points to, or None if the backlight never
    responded -- which puts the fault below the protocol and makes every later
    stage pointless.
    """
    _heading("2. The backlight")
    _note("The backlight pin is the only one that needs no part of the display")
    _note("protocol -- no handshake, no timing, no contrast. If it responds,")
    _note("the bus, address, power and ground are all proven good.")
    _note("")
    _note("The two backpack layouts also put the backlight on different pins,")
    _note("so which pin responds narrows down the layout too.")

    pin = find_backlight_pin(bus, address)
    if pin is None:
        _bad("the backlight does not respond to writes that are acknowledged")
        return None

    active_low = find_backlight_polarity(bus, address, pin)
    mapping = mapping_for_backlight(pin, active_low)
    if mapping is not None:
        _good(f"backlight on P{pin}"
              f"{', active low' if active_low else ''}"
              f" -- consistent with the '{mapping.name}' layout")
    return mapping


def report_backlight_failure(bus: int, address: int) -> None:
    _heading("Diagnosis")
    _note("Something at 0x%02X acknowledges on bus %d, but the backlight pin"
          % (address, bus))
    _note("does nothing. That is a narrow set of causes:")
    _note("")
    _note("  1. The backlight jumper on the backpack is missing. Most modules")
    _note("     have a two-pin jumper beside the contrast trimmer; without it")
    _note("     the LED is disconnected and the panel is simply unlit.")
    _note("  2. VCC is on 3.3 V. The backlight and the contrast bias both want")
    _note("     5 V; at 3.3 V the panel can be too dim to see at all in a lit")
    _note("     room. Move VCC to header pin 2 or 4 and look again.")
    _note("  3. The address belongs to something else on the bus -- an RTC or")
    _note("     a sensor -- and the display is not connected at all.")
    _note("  4. The backpack is not soldered to the panel properly. Check the")
    _note("     sixteen pins joining the two boards.")


# --------------------------------------------------------------------------
# Stage 3: which pin mapping the module uses
# --------------------------------------------------------------------------

def try_pinmap(bus: int, address: int, mapping: PinMap, hold: float) -> None:
    """Bring the panel up under *mapping* and show a pattern naming it."""
    lcd = CharacterLCD.open_i2c(bus=bus, address=address, cols=20, rows=4,
                                autodetect=False, pinmap=mapping)
    try:
        lcd.clear()
        lcd.write_at(0, 0, "PIN MAP TEST")
        lcd.write_at(1, 0, mapping.name.upper()[:20])
        lcd.write_at(2, 0, "IF YOU CAN READ")
        lcd.write_at(3, 0, "THIS, SAY YES")
        lcd.flush()
        time.sleep(hold)
    finally:
        lcd.close()


def check_pinmaps(bus: int, address: int, identified: Optional[PinMap],
                  hold: float = 3.0) -> Optional[PinMap]:
    _heading("3. The pin mapping")
    _note("A backpack is eight output pins wired to the display's control and")
    _note("data lines, and which pin goes where is a property of the board.")
    _note("Two layouts exist. Driven with the wrong one, a module receives")
    _note("perfectly valid traffic and shows nothing -- which is exactly what")
    _note("you are seeing. This confirms what the backlight test suggested.")

    # Try the layout the backlight named first, then the rest: a board could
    # in principle mix conventions, and the cost of checking is one question.
    ordered = list(pinmaps.ALL)
    if identified is not None:
        ordered.remove(identified)
        ordered.insert(0, identified)

    for mapping in ordered:
        print()
        _note(f"Trying {BOLD}{mapping.name}{RESET}: {mapping.pin_summary()}")
        _note(f"Watch the panel for {hold:.0f} seconds...")
        try:
            try_pinmap(bus, address, mapping, hold)
        except TransportError as exc:
            _bad(f"write failed: {exc}")
            continue
        if _ask("Did the panel show readable text?"):
            _good(f"this module uses the '{mapping.name}' mapping")
            return mapping
    return None


# --------------------------------------------------------------------------
# Stage 4: contrast
# --------------------------------------------------------------------------

def contrast_pattern(bus: int, address: int, mapping: PinMap,
                     hold: float = 25.0) -> None:
    """Fill the panel with solid blocks: the most visible pattern there is."""
    lcd = CharacterLCD.open_i2c(bus=bus, address=address, cols=20, rows=4,
                                autodetect=False, pinmap=mapping)
    try:
        lcd.clear()
        # Alternating solid blocks and spaces. At the wrong contrast a panel
        # shows either nothing or uniform blocks; this pattern is unmistakable
        # the moment the trimmer passes through the usable band.
        for row in range(4):
            lcd.write_at(row, 0, ("\xff " * 10) if row % 2 == 0
                         else (" \xff" * 10))
        lcd.flush()
        deadline = time.time() + hold
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            print(f"\r       holding pattern, {remaining:2d}s left ",
                  end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 50 + "\r", end="")
    finally:
        lcd.close()


def check_contrast(bus: int, address: int, mapping: PinMap) -> bool:
    _heading("4. Contrast")
    _note("The contrast bias is set by a trimmer potentiometer on the back of")
    _note("the backpack -- a small blue box with a cross-head screw. It is")
    _note("frequently shipped at one extreme, where the panel shows either")
    _note("nothing or a solid row of blocks. Software cannot change it.")
    _note("")
    _note("A checkerboard of solid blocks is now on the panel. Turn the trimmer")
    _note("slowly through its WHOLE range -- it may take 15 or more turns end")
    _note("to end -- and watch for the pattern appearing.")
    input(f"\n  {BOLD}Press ENTER to display the pattern.{RESET} ")
    try:
        contrast_pattern(bus, address, mapping)
    except TransportError as exc:
        _bad(f"write failed: {exc}")
        return False
    return _ask("Did a checkerboard appear at any point?")


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run(config) -> int:
    """Walk the whole bisection.  Returns a process exit code."""
    print(f"{BOLD}Panel diagnosis{RESET}")
    print(DIM + "Answer by looking at the panel. Ctrl+C stops." + RESET)

    candidates = report_buses()
    if not candidates:
        return 1

    if len(candidates) == 1:
        bus, address = candidates[0]
    else:
        print()
        _note("Several devices answered. Pick the display:")
        index = _choose("Which one?",
                        [f"bus {b}, address 0x{a:02X}" for b, a in candidates])
        bus, address = candidates[index]

    identified = check_backlight(bus, address)
    if identified is None:
        report_backlight_failure(bus, address)
        return 1

    mapping = check_pinmaps(bus, address, identified)

    if mapping is None:
        # Every mapping produced nothing, but the bus is proven good. The one
        # remaining explanation that software cannot rule out is contrast.
        fallback = identified
        if check_contrast(bus, address, fallback):
            _good("the fault was contrast")
            mapping = fallback
        else:
            _heading("Diagnosis")
            _bad("the bus works, but no mapping and no contrast setting")
            _note("produced readable characters.")
            _note("")
            _note("What is left:")
            _note("  1. VCC on 3.3 V rather than 5 V. An HD44780 needs close to")
            _note("     5 V for a readable contrast bias; at 3.3 V some panels")
            _note("     cannot be brought into range by the trimmer at all.")
            _note("  2. A damaged panel, or a backpack not making contact with")
            _note("     all sixteen of the panel's pins.")
            _note("  3. A 16x2 panel rather than 20x4 -- text would appear on")
            _note("     rows 1 and 2 only. Run:  ./run.sh --self-test")
            _note("")
            _note("Report what you have found with:  ./run.sh --probe")
            return 1

    _heading("Result")
    _good(f"bus {bus}, address 0x{address:02X}, "
          f"'{mapping.name}' mapping")
    _note(mapping.pin_summary())

    changed = []
    if int(config.get("display.i2c_bus")) != bus:
        config.set("display.i2c_bus", bus)
        changed.append(f"bus {bus}")
    if int(config.get("display.i2c_addr")) != address:
        config.set("display.i2c_addr", address)
        changed.append(f"address 0x{address:02X}")
    if str(config.get("display.pinmap")) != mapping.name:
        config.set("display.pinmap", mapping.name)
        changed.append(f"pin map {mapping.name}")

    if changed and config.save():
        _good("saved to " + config.path)
        _note("changed: " + ", ".join(changed))
    elif changed:
        _bad("could not write " + config.path)
    else:
        _note("settings already correct; nothing to change")

    print()
    _note("Now try:  ./run.sh")
    return 0
