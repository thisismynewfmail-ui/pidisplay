"""
Entry point: assemble the hardware, the engine and the UI, then run.

Failures here are the ones a person meets first, so each one reports what is
wrong, where, and what to do about it, on stderr where there is room for a
sentence -- rather than on a panel that may itself be the thing that is broken.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from typing import Optional

from . import BUILD_NAME, VERSION
from .config import Config, config_path
from .hal import glyphs as G
from .hal.display import Display
from .hal import pinmap as pinmaps
from .hal.lcd import (CANDIDATE_ADDRESSES, CharacterLCD, list_buses,
                      probe_addresses, scan_all_buses)
from .hal.keyboard import KeyboardHub, enumerate_keyboards
from .hal.transport import EmulatedTransport, TransportError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aperture",
        description=f"{BUILD_NAME} {VERSION} -- a local chat terminal for a "
                    "20x4 HD44780 display on a Raspberry Pi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              aperture                    run against the attached panel
              aperture --sim              mirror the panel in this terminal
              aperture --probe            list I2C addresses and keyboards
              aperture --self-test        draw a test pattern and exit
              aperture --doctor           diagnose a panel that shows nothing
        """))
    parser.add_argument("--config", metavar="PATH", default=None,
                        help=f"settings file (default: {config_path()})")
    parser.add_argument("--models", metavar="DIR", default=None,
                        help="directory to scan for .gguf models")
    parser.add_argument("--sim", action="store_true",
                        help="run against a simulated panel in this terminal")
    parser.add_argument("--sim-mode", choices=("auto", "pixel", "text"),
                        default="auto", help="simulator fidelity")
    parser.add_argument("--bus", type=int, default=None,
                        help="I2C bus number (default: from settings)")
    parser.add_argument("--address", default=None,
                        help="I2C address, e.g. 0x27 (default: autodetect)")
    parser.add_argument("--no-grab", action="store_true",
                        help="do not claim keyboards exclusively")
    parser.add_argument("--no-stdin", action="store_true",
                        help="ignore this terminal as an input source")
    parser.add_argument("--probe", action="store_true",
                        help="report I2C and input devices, then exit")
    parser.add_argument("--self-test", action="store_true",
                        help="draw a panel test pattern, then exit")
    parser.add_argument("--doctor", action="store_true",
                        help="interactively diagnose a panel that shows nothing")
    parser.add_argument("--pinmap", choices=pinmaps.NAMES, default=None,
                        help="how the backpack wires the expander to the panel")
    parser.add_argument("--version", action="version",
                        version=f"{BUILD_NAME} {VERSION}")
    return parser


def _parse_address(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        raise SystemExit(f"not a valid I2C address: {value!r}")


def open_display(config: Config, args) -> tuple:
    """Return ``(display, simulator)``; simulator is None on real hardware."""
    cols, rows = 20, 4

    if args.sim:
        from .simulator import TerminalSimulator
        mapping = pinmaps.get(args.pinmap or str(config.get("display.pinmap")))
        transport = EmulatedTransport(cols=cols, rows=rows, pinmap=mapping)
        lcd = CharacterLCD(transport, cols=cols, rows=rows, pinmap=mapping)
        lcd.initialise()
        simulator = TerminalSimulator(transport.emulator, mode=args.sim_mode)
        return Display(lcd), simulator

    bus = args.bus if args.bus is not None else int(config.get("display.i2c_bus", 1))
    address = _parse_address(args.address)
    if address is None:
        configured = int(config.get("display.i2c_addr", 0))
        address = configured if configured > 0 else None

    mapping = pinmaps.get(args.pinmap or str(config.get("display.pinmap")))
    try:
        lcd = CharacterLCD.open_i2c(bus=bus, address=address or 0x27,
                                    cols=cols, rows=rows,
                                    autodetect=(address is None),
                                    pinmap=mapping)
    except TransportError as exc:
        _report_display_failure(exc, bus)
        raise SystemExit(2)
    return Display(lcd), None


def _report_display_failure(exc: Exception, bus: int) -> None:
    found = probe_addresses(bus)
    print(f"error: {exc}", file=sys.stderr)
    print("", file=sys.stderr)
    if found:
        listed = ", ".join(f"0x{a:02X}" for a in found)
        print(f"Devices did answer on bus {bus}: {listed}", file=sys.stderr)
        print("Try:  aperture --address 0x%02X" % found[0], file=sys.stderr)
    else:
        print(textwrap.dedent(f"""\
            Nothing answered on I2C bus {bus}. Check, in this order:

              1. I2C is enabled:      sudo raspi-config nonint do_i2c 0
              2. The kernel sees it:  ls /dev/i2c-*
              3. The panel answers:   i2cdetect -y {bus}
              4. Wiring, against the table in README.md. The two most common
                 faults are SDA and SCL swapped, and no ground between the
                 panel and the Pi.

            If the backlight is on but the screen is blank, the contrast
            trimmer on the back of the module needs turning.
        """), file=sys.stderr)


def command_probe(config: Config, args) -> int:
    bus = args.bus if args.bus is not None else int(config.get("display.i2c_bus", 1))
    print(f"{BUILD_NAME} {VERSION}")
    print(f"config       {config.path}")
    print(f"models       {config.models_dir}")
    print(f"endpoint     {config.base_url}")
    print()

    buses = list_buses()
    print("I2C:")
    if not buses:
        print("  no /dev/i2c-* devices -- I2C is not enabled")
        print("  enable it with: sudo raspi-config nonint do_i2c 0 && sudo reboot")
    else:
        # Every bus, not just the configured one: on a Pi 5 the header has
        # not always been bus 1, and a panel on an unexpected number should be
        # found rather than reported as missing.
        total = 0
        for number, addresses in scan_all_buses():
            marker = " (configured)" if number == bus else ""
            if addresses:
                total += len(addresses)
                for address in addresses:
                    likely = (" <- typical display backpack"
                              if address in (0x27, 0x3F) else "")
                    print(f"  bus {number}{marker}: 0x{address:02X} responds{likely}")
            else:
                print(f"  bus {number}{marker}: nothing responded")
        if not total:
            print("  addresses probed: " +
                  ", ".join(f"0x{a:02X}" for a in CANDIDATE_ADDRESSES[:8]) + ", ...")
            print("  run './run.sh --doctor' to work through the causes")
    mapping = pinmaps.get(str(config.get("display.pinmap")))
    print(f"  pin map: {mapping.name} ({mapping.pin_summary()})")
    print()

    print("Keyboards:")
    keyboards = enumerate_keyboards()
    if not keyboards:
        print("  none found under /dev/input")
    for keyboard in keyboards:
        readable = os.access(keyboard.path, os.R_OK)
        note = "" if readable else "  [NOT READABLE -- add user to 'input' group]"
        print(f"  {keyboard.path}  {keyboard.transport():4s} {keyboard.name}{note}")
    print()

    from .llm.server import find_binary
    binary = find_binary(str(config.get("paths.llama_server", "")), PROJECT_ROOT)
    print(f"llama-server  {binary or 'NOT FOUND'}")

    from .llm.models import scan_models
    models = scan_models(config.models_dir)
    print(f"models found  {len(models)}")
    for model in models:
        context = f", native ctx {model.train_context}" if model.train_context else ""
        print(f"  {model.filename}  ({model.summary()}{context})")
    return 0


def command_self_test(config: Config, args) -> int:
    """Draw a pattern that makes every common wiring fault visible.

    The pattern is chosen so that each fault produces a *different* wrong
    result rather than all of them producing a blank panel: the row numbers
    distinguish a geometry mismatch, the ruler distinguishes a width mismatch,
    and the block runs stay visible at contrast settings where text does not.
    """
    display, simulator = open_display(config, args)
    display.use_bank(G.BANK_SYSTEM)
    frame = display.begin_frame()
    frame.text(0, 0, "1:2345678901234567890")
    frame.text(1, 0, "2:" + G.ROM_FULL_BLOCK * 6 + " abcdefgh")
    glyphs = "".join(display.g(name) for name in
                     ("state", "half", "check", "cross", "warn", "wifi", "bt"))
    frame.text(2, 0, "3:" + glyphs + " CUSTOM")
    frame.text(3, 0, "4:" + G.ROM_FULL_BLOCK * 6 + " CONTRAST")
    display.present()
    if simulator is not None:
        simulator.enter()
        simulator.render()
        simulator.leave()

    transport = display.lcd.transport
    address = getattr(transport, "address", None)
    stats = display.stats
    print()
    print("Wrote a test pattern:")
    if address is not None:
        print(f"  bus 0x{getattr(transport, 'bus_number', '?')}, "
              f"address 0x{address:02X}")
    else:
        print("  simulated panel (no hardware)")
    print(f"  pin map {display.lcd.pinmap.name}: "
          f"{display.lcd.pinmap.pin_summary()}")
    print(f"  backlight {'on' if display.backlight else 'off'}, "
          f"{stats.last_frame_bytes} bytes accepted by the bus")
    print()
    print("Each row starts with its own number. You should see:")
    print("  1:2345678901234567890   a ruler across the full 20 columns")
    print("  2:######  abcdefgh      solid blocks, then lower case")
    print("  3:....... CUSTOM        seven distinct icons")
    print("  4:######  CONTRAST      solid blocks again")
    print()
    print("What you actually see tells you which fault it is:")
    print("  Nothing at all, backlight on ..... contrast, or the pin mapping.")
    print("                                     Run: ./run.sh --doctor")
    print("  Solid blocks on every row ........ contrast set too high.")
    print("  Only rows 1 and 3 ................ the panel is 16x2, not 20x4.")
    print("  Rows appear as 1,3,2,4 ........... row address map is wrong.")
    print("  Text but wrong characters ........ bus too fast; try")
    print("                                     dtparam=i2c_arm_baudrate=100000")
    print("  Nothing, backlight also off ...... power, ground, or address.")
    print("                                     Run: ./run.sh --doctor")
    display.close()
    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    config = Config.load(args.config)
    if args.models:
        config.set("paths.models", os.path.abspath(os.path.expanduser(args.models)))
    if config.load_error:
        print(f"warning: settings could not be read ({config.load_error}); "
              "continuing with defaults", file=sys.stderr)

    os.makedirs(config.models_dir, exist_ok=True)
    try:
        os.makedirs(os.path.expanduser(str(config.get("paths.state"))), exist_ok=True)
    except OSError:
        pass

    if args.probe:
        return command_probe(config, args)
    if args.doctor:
        from .doctor import run as run_doctor
        return run_doctor(config)
    if args.self_test:
        return command_self_test(config, args)

    display, simulator = open_display(config, args)
    display.backlight = bool(config.get("display.backlight", True))

    keyboard = KeyboardHub(
        grab=bool(config.get("input.grab", True)) and not args.no_grab,
        use_stdin=not args.no_stdin)

    from .ui.app import App
    from .ui.boot import SplashStage
    from .ui.chat import ChatScreen

    app = App(config, display, keyboard, project_root=PROJECT_ROOT)
    exit_code = 0
    try:
        keyboard.start()
        if simulator is not None:
            simulator.enter()
            app.frame_hook = simulator.render
        app.push(ChatScreen(app))
        app.push(SplashStage(app))
        exit_code = app.run()
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        keyboard.stop()
        if simulator is not None:
            simulator.leave()
        try:
            display.close()
        except Exception:
            pass
        if config.dirty:
            config.save()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
