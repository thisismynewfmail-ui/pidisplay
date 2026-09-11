"""
Tests for the panel diagnostic.

A diagnostic that reaches the wrong conclusion is worse than none, because it
sends someone to rewire hardware that was fine.  So the whole bisection is run
against a simulated module -- one whose wiring the test knows and the
diagnostic does not -- with a stand-in operator that answers each question by
looking at what the emulated panel would actually be showing.
"""

import unittest
from unittest import mock

from aperture import doctor
from aperture.config import Config
from aperture.hal import pinmap as P
from aperture.hal.emulator import HD44780Emulator
from aperture.hal.lcd import CharacterLCD


class FakeModule:
    """A PCF8574 backpack with a known wiring, and an operator watching it."""

    def __init__(self, wiring, address=0x27, bus=1, contrast_ok=True,
                 trimmer_helps=True):
        self.wiring = wiring
        self.address = address
        self.bus = bus
        self.contrast_ok = contrast_ok
        # Whether turning the trimmer brings the panel into range at all.
        self.trimmer_helps = trimmer_helps
        self.emulator = HD44780Emulator(cols=20, rows=4, pinmap=wiring)
        self.backlight_changes = 0
        self._last_backlight = self.emulator.backlight
        self.questions = []

    # -- the wire -----------------------------------------------------------

    def transport(self, bus=None, address=None):
        module = self

        class _Transport:
            def __init__(self, *_args, **_kwargs):
                self.address = module.address
                self.bus_number = module.bus

            def write(self, data):
                for byte in data:
                    module.emulator.write_port(byte)
                    if module.emulator.backlight != module._last_backlight:
                        module._last_backlight = module.emulator.backlight
                        module.backlight_changes += 1

            def close(self):
                pass

        return _Transport()

    # -- the operator -------------------------------------------------------

    def screen(self):
        return "".join(self.emulator.readable_screen())

    def answer(self, question, default=None):
        self.questions.append(question)
        lowered = question.lower()
        if "blink" in lowered:
            seen = self.backlight_changes > 2
            self.backlight_changes = 0
            return seen
        if "readable text" in lowered:
            # Contrast too low means nothing is legible however correct the
            # traffic is -- which is the point of keeping the two separable.
            return self.contrast_ok and "PIN MAP TEST" in self.screen()
        if "lit right now" in lowered:
            return self.emulator.backlight
        if "checkerboard" in lowered:
            # The operator turns the trimmer while the pattern is held, so a
            # merely misadjusted panel comes into range here.
            return self.trimmer_helps
        return bool(default)


def run_doctor(module, config=None):
    """Run the full bisection against *module*, with no real hardware."""
    config = config or Config(path="/dev/null")

    def open_i2c(bus, address, cols=20, rows=4, autodetect=True, pinmap=None):
        lcd = CharacterLCD(module.transport(), cols=cols, rows=rows,
                           pinmap=pinmap)
        lcd.initialise()
        return lcd

    fake_lcd = mock.Mock()
    fake_lcd.open_i2c.side_effect = open_i2c

    with mock.patch.object(doctor, "list_buses", return_value=[module.bus]), \
         mock.patch.object(doctor, "probe_addresses",
                           side_effect=lambda bus: [module.address]
                           if bus == module.bus else []), \
         mock.patch.object(doctor, "I2CTransport", module.transport), \
         mock.patch.object(doctor, "CharacterLCD", fake_lcd), \
         mock.patch.object(doctor, "_ask", module.answer), \
         mock.patch.object(doctor.time, "sleep", lambda _s: None), \
         mock.patch.object(doctor, "contrast_pattern", lambda *a, **k: None), \
         mock.patch("builtins.input", lambda *_a: ""):
        code = doctor.run(config)
    return code, config


class TestDiagnosis(unittest.TestCase):
    def test_identifies_every_wiring(self):
        """Given a module of each wiring, the diagnostic must name it."""
        for wiring in P.ALL:
            module = FakeModule(wiring)
            code, config = run_doctor(module)
            self.assertEqual(code, 0, f"{wiring.name}: diagnosis failed")
            self.assertEqual(config.get("display.pinmap"), wiring.name,
                             f"{wiring.name}: identified as "
                             f"{config.get('display.pinmap')}")

    def test_records_bus_and_address(self):
        module = FakeModule(P.STANDARD, address=0x3F, bus=11)
        code, config = run_doctor(module)
        self.assertEqual(code, 0)
        self.assertEqual(config.get("display.i2c_addr"), 0x3F)
        self.assertEqual(config.get("display.i2c_bus"), 11)

    def test_the_common_case_asks_one_blink_question(self):
        """A standard module should not be interrogated needlessly."""
        module = FakeModule(P.STANDARD)
        run_doctor(module)
        blink_questions = [q for q in module.questions if "blink" in q.lower()]
        self.assertEqual(len(blink_questions), 1)

    def test_polarity_is_asked_not_inferred_from_the_blink(self):
        """A blink happens under either polarity, so it cannot settle it."""
        module = FakeModule(P.STANDARD_INVERTED)
        run_doctor(module)
        self.assertTrue(any("lit right now" in q.lower()
                            for q in module.questions),
                        "polarity was assumed rather than observed")

    def test_backlight_test_precedes_any_display_command(self):
        """The bisection is only valid if the cheap test really comes first."""
        module = FakeModule(P.STANDARD)
        run_doctor(module)
        self.assertIn("blink", module.questions[0].lower())

    def test_contrast_fault_is_not_blamed_on_wiring(self):
        """A panel that is merely turned down must not be called miswired."""
        module = FakeModule(P.STANDARD, contrast_ok=False)
        code, config = run_doctor(module)
        self.assertEqual(code, 0)
        self.assertEqual(config.get("display.pinmap"), P.STANDARD.name)
        self.assertTrue(any("checkerboard" in q.lower()
                            for q in module.questions),
                        "contrast was never offered as an explanation")

    def test_unexplained_panel_is_reported_as_unexplained(self):
        """When nothing works, say so rather than guessing an answer."""
        module = FakeModule(P.STANDARD, contrast_ok=False, trimmer_helps=False)
        code, _ = run_doctor(module)
        self.assertEqual(code, 1)


class TestNoHardware(unittest.TestCase):
    def test_no_buses_reports_i2c_disabled(self):
        config = Config(path="/dev/null")
        with mock.patch.object(doctor, "list_buses", return_value=[]):
            self.assertEqual(doctor.run(config), 1)

    def test_no_devices_reports_wiring(self):
        config = Config(path="/dev/null")
        with mock.patch.object(doctor, "list_buses", return_value=[1]), \
             mock.patch.object(doctor, "probe_addresses", return_value=[]):
            self.assertEqual(doctor.run(config), 1)

    def test_unresponsive_backlight_stops_early(self):
        """No point trying display commands on a panel that will not light."""
        config = Config(path="/dev/null")
        asked = []
        with mock.patch.object(doctor, "list_buses", return_value=[1]), \
             mock.patch.object(doctor, "probe_addresses", return_value=[0x27]), \
             mock.patch.object(doctor, "blink_backlight", lambda *a, **k: None), \
             mock.patch.object(doctor, "try_pinmap",
                               lambda *a, **k: asked.append("display")), \
             mock.patch.object(doctor, "_ask", lambda *a, **k: False):
            self.assertEqual(doctor.run(config), 1)
        self.assertEqual(asked, [], "display commands sent after backlight failed")


if __name__ == "__main__":
    unittest.main()
