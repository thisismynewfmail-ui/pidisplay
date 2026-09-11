"""
Pin mapping tests.

These exist because of a blind spot the rest of the suite had: the emulator
used to encode the same wiring assumption the driver did, so no test could
distinguish a correct mapping from a wrong one.  Decoding through a PinMap
lets a test drive one wiring and decode with another, which is the only way to
assert the difference is real.
"""

import unittest

from aperture.hal import pinmap as P
from aperture.hal.emulator import HD44780Emulator
from aperture.hal.lcd import CharacterLCD
from aperture.hal.transport import EmulatedTransport, RecordingTransport


def build(driver_map, module_map=None):
    emulator = HD44780Emulator(cols=20, rows=4, pinmap=module_map or driver_map)
    recorder = RecordingTransport(EmulatedTransport(emulator=emulator))
    lcd = CharacterLCD(recorder, cols=20, rows=4, pinmap=driver_map)
    lcd.initialise()
    return lcd, emulator, recorder


class TestPinMapDefinition(unittest.TestCase):
    def test_every_map_uses_each_pin_once(self):
        for mapping in P.ALL:
            pins = [mapping.rs, mapping.rw, mapping.en, mapping.backlight,
                    *mapping.data]
            self.assertEqual(sorted(pins), list(range(8)), mapping.name)

    def test_overlapping_pins_are_rejected(self):
        with self.assertRaises(ValueError):
            P.PinMap(name="bad", rs=0, rw=0, en=2, backlight=3,
                     data=(4, 5, 6, 7))

    def test_nibble_encoding_round_trips(self):
        for mapping in P.ALL:
            for nibble in range(16):
                self.assertEqual(
                    mapping.decode_nibble(mapping.encode_nibble(nibble)),
                    nibble, mapping.name)

    def test_lookup_table_matches_the_encoder(self):
        for mapping in P.ALL:
            table = mapping.nibble_table()
            for nibble in range(16):
                self.assertEqual(table[nibble], mapping.encode_nibble(nibble))

    def test_backlight_polarity(self):
        self.assertEqual(P.STANDARD.backlight_mask(True), 0x08)
        self.assertEqual(P.STANDARD.backlight_mask(False), 0x00)
        self.assertEqual(P.STANDARD_INVERTED.backlight_mask(True), 0x00)
        self.assertEqual(P.STANDARD_INVERTED.backlight_mask(False), 0x08)

    def test_unknown_name_falls_back_to_the_common_map(self):
        self.assertIs(P.get("nonsense"), P.STANDARD)
        self.assertIs(P.get("ywrobot"), P.YWROBOT)


class TestStandardMapIsUnchanged(unittest.TestCase):
    """The default wiring must produce exactly the bytes it always did."""

    def test_init_sequence_bytes(self):
        _, _, recorder = build(P.STANDARD)
        expected = [
            0x38, 0x3C, 0x38,        # 0x30, wake 1
            0x38, 0x3C, 0x38,        # 0x30, wake 2
            0x38, 0x3C, 0x38,        # 0x30, wake 3
            0x28, 0x2C, 0x28,        # 0x20, select four-bit mode
        ]
        self.assertEqual(recorder.log[:12], expected)

    def test_data_write_sets_rs_and_never_rw(self):
        lcd, _, recorder = build(P.STANDARD)
        recorder.reset()
        lcd.write_at(0, 0, "A")
        lcd.flush()
        self.assertTrue(all(b & 0x02 == 0 for b in recorder.log), "RW asserted")
        self.assertTrue(any(b & 0x01 for b in recorder.log), "RS never asserted")

    def test_backlight_bit_rides_every_byte(self):
        _, _, recorder = build(P.STANDARD)
        self.assertTrue(all(b & 0x08 for b in recorder.log))


class TestMapsAreDistinguishable(unittest.TestCase):
    def test_matched_map_renders(self):
        for mapping in (P.STANDARD, P.YWROBOT):
            lcd, emulator, _ = build(mapping)
            lcd.write_at(0, 0, "HELLO")
            lcd.flush()
            self.assertTrue(emulator.four_bit, mapping.name)
            self.assertTrue(emulator.readable_screen()[0].startswith("HELLO"),
                            mapping.name)

    def test_mismatched_map_never_reaches_four_bit_mode(self):
        """The signature of the fault: the mode switch never lands.

        A module driven with the wrong mapping acknowledges every byte and
        stays in its power-on eight-bit state, so nothing legible is ever
        written. That is precisely why it looks like a dead panel.
        """
        for driver, module in ((P.STANDARD, P.YWROBOT),
                               (P.YWROBOT, P.STANDARD)):
            lcd, emulator, _ = build(driver, module)
            lcd.write_at(0, 0, "HELLO")
            lcd.flush()
            self.assertFalse(emulator.four_bit,
                             f"driver={driver.name} module={module.name}")
            self.assertNotIn("HELLO", "".join(emulator.readable_screen()))

    def test_the_two_maps_put_data_on_opposite_nibbles(self):
        self.assertEqual(P.STANDARD.encode_nibble(0xF), 0xF0)
        self.assertEqual(P.YWROBOT.encode_nibble(0xF), 0x0F)


class TestRawBacklight(unittest.TestCase):
    """The bisection the diagnostic relies on must be exactly one byte."""

    def test_raw_backlight_writes_a_single_byte(self):
        lcd, _, recorder = build(P.STANDARD)
        recorder.reset()
        lcd.set_backlight_raw(False)
        self.assertEqual(recorder.log, [0x00])
        recorder.reset()
        lcd.set_backlight_raw(True)
        self.assertEqual(recorder.log, [0x08])

    def test_raw_backlight_asserts_no_protocol_lines(self):
        """It must touch no RS, RW, E or data pin -- that is the whole point."""
        for mapping in P.ALL:
            lcd, _, recorder = build(mapping)
            recorder.reset()
            lcd.set_backlight_raw(True)
            for byte in recorder.log:
                self.assertEqual(byte & mapping.rs_bit, 0, mapping.name)
                self.assertEqual(byte & mapping.rw_bit, 0, mapping.name)
                self.assertEqual(byte & mapping.en_bit, 0, mapping.name)
                for bit in mapping.data:
                    self.assertEqual(byte & (1 << bit), 0, mapping.name)

    def test_raw_backlight_discards_staged_bytes(self):
        """It must not flush a half-built frame onto a confused controller."""
        lcd, _, recorder = build(P.STANDARD)
        lcd.write_at(0, 0, "PENDING")
        recorder.reset()
        lcd.set_backlight_raw(True)
        self.assertEqual(len(recorder.log), 1)


class TestConfigIntegration(unittest.TestCase):
    def test_every_schema_choice_resolves(self):
        from aperture.config import SETTINGS_BY_KEY
        setting = SETTINGS_BY_KEY["display.pinmap"]
        for choice in setting.choices:
            self.assertIn(choice, P.BY_NAME, choice)

    def test_schema_covers_every_map(self):
        from aperture.config import SETTINGS_BY_KEY
        setting = SETTINGS_BY_KEY["display.pinmap"]
        self.assertEqual(set(setting.choices), set(P.BY_NAME))

    def test_default_is_the_common_map(self):
        from aperture.config import SETTINGS_BY_KEY
        self.assertEqual(SETTINGS_BY_KEY["display.pinmap"].default,
                         P.DEFAULT.name)


if __name__ == "__main__":
    unittest.main()
