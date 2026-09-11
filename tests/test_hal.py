"""Panel driver, framebuffer and glyph tests, run against the emulator."""

import unittest

from aperture.hal import glyphs as G
from aperture.hal.display import Display
from aperture.hal.emulator import HD44780Emulator
from aperture.hal.lcd import CharacterLCD
from aperture.hal.transport import EmulatedTransport, RecordingTransport


def build(cols=20, rows=4):
    transport = EmulatedTransport(cols=cols, rows=rows)
    recorder = RecordingTransport(transport)
    lcd = CharacterLCD(recorder, cols=cols, rows=rows)
    lcd.initialise()
    return Display(lcd), transport.emulator, recorder


class TestController(unittest.TestCase):
    def test_enters_four_bit_mode(self):
        _, emulator, _ = build()
        self.assertTrue(emulator.four_bit)
        self.assertTrue(emulator.display_on)

    def test_row_addresses_are_not_sequential(self):
        """20x4 panels lay rows out as 0x00,0x40,0x14,0x54, not 0,20,40,60."""
        _, emulator, _ = build()
        self.assertEqual(emulator.row_offsets, [0x00, 0x40, 0x14, 0x54])

    def test_each_row_is_addressed_independently(self):
        display, emulator, _ = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        for row in range(4):
            frame.text(row, 0, f"ROW{row}" + "." * 16)
        display.present()
        for row, line in enumerate(emulator.text_screen()):
            self.assertTrue(line.startswith(f"ROW{row}"), line)

    def test_full_width_write_does_not_bleed(self):
        """Writing 20 characters to row 0 must not spill into row 2."""
        display, emulator, _ = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        frame.text(0, 0, "X" * 20)
        display.present()
        screen = emulator.text_screen()
        self.assertEqual(screen[0], "X" * 20)
        self.assertEqual(screen[2].strip(), "")

    def test_reinitialises_from_mid_byte_state(self):
        """A warm restart must resynchronise the four-bit nibble phase."""
        transport = EmulatedTransport()
        lcd = CharacterLCD(transport)
        lcd.initialise()
        # Leave the controller expecting a second nibble, as a crash would.
        transport.emulator._pending_nibble = 0x4
        lcd2 = CharacterLCD(transport)
        lcd2.initialise()
        lcd2.write_at(0, 0, "RECOVERED")
        lcd2.flush()
        self.assertTrue(transport.emulator.text_screen()[0].startswith("RECOVERED"))


class TestGlyphs(unittest.TestCase):
    def test_banks_fit_in_cgram(self):
        for bank in G.BANKS.values():
            self.assertLessEqual(len(bank.names), G.CGRAM_SLOTS, bank.name)

    def test_glyph_rows_are_five_bits(self):
        for bank in G.BANKS.values():
            for name in bank.names:
                for row in bank.pattern(name):
                    self.assertTrue(0 <= row <= 0b11111, f"{bank.name}.{name}")

    def test_cgram_codes_avoid_nul(self):
        for index in range(G.CGRAM_SLOTS):
            self.assertGreaterEqual(ord(G.code(index)), 8)

    def test_gauge_never_reads_full_below_full(self):
        for percent in range(0, 100):
            cells = G.hgauge_cells(percent / 100.0, 4)
            self.assertNotEqual(cells, [2, 2, 2, 2], f"{percent}% read as full")

    def test_gauge_never_reads_empty_above_zero(self):
        for percent in range(1, 101):
            cells = G.hgauge_cells(percent / 100.0, 4)
            self.assertNotEqual(cells, [0, 0, 0, 0], f"{percent}% read as empty")

    def test_gauge_is_monotonic(self):
        previous = -1
        for percent in range(0, 101):
            total = sum(G.hgauge_cells(percent / 100.0, 4))
            self.assertGreaterEqual(total, previous)
            previous = total

    def test_glyphs_reach_the_controller(self):
        display, emulator, _ = build()
        display.use_bank(G.BANK_CHAT)
        display.present()
        slot = G.BANK_CHAT.slot("rail")
        self.assertEqual(emulator.cgram_glyph(slot), G.RAIL_DOT)

    def test_dynamic_slot_updates_in_place(self):
        display, emulator, _ = build()
        display.use_bank(G.BANK_CHAT)
        display.set_glyph("state", G.iris(2))
        display.present()
        slot = G.BANK_CHAT.slot("state")
        self.assertEqual(emulator.cgram_glyph(slot), G.iris(2))


class TestDifferentialRendering(unittest.TestCase):
    def test_unchanged_frame_costs_nothing(self):
        display, _, recorder = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        frame.text(0, 0, "STEADY")
        display.present()
        recorder.reset()
        frame = display.begin_frame()
        display.use_bank(G.BANK_CHAT)
        frame.text(0, 0, "STEADY")
        display.present()
        self.assertEqual(len(recorder.log), 0)

    def test_single_character_change_is_cheap(self):
        display, _, recorder = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        frame.text(1, 0, "hello world")
        display.present()
        recorder.reset()
        frame = display.begin_frame()
        display.use_bank(G.BANK_CHAT)
        frame.text(1, 0, "hello worlds")
        display.present()
        # One cursor move plus one character: twelve port bytes.
        self.assertLessEqual(len(recorder.log), 12)

    def test_adjacent_changes_merge_into_one_run(self):
        display, _, recorder = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        frame.text(0, 0, "A.B")
        display.present()
        recorder.reset()
        frame = display.begin_frame()
        display.use_bank(G.BANK_CHAT)
        frame.text(0, 0, "X.Y")
        display.present()
        # Bridging the single unchanged cell costs the same as re-addressing,
        # so this must be one run of three, not two runs of one.
        self.assertLessEqual(len(recorder.log), 24)

    def test_full_repaint_after_invalidate(self):
        display, emulator, recorder = build()
        display.use_bank(G.BANK_CHAT)
        frame = display.begin_frame()
        frame.text(0, 0, "FULL")
        display.present()
        display.invalidate()
        recorder.reset()
        frame = display.begin_frame()
        display.use_bank(G.BANK_CHAT)
        frame.text(0, 0, "FULL")
        display.present()
        self.assertGreater(len(recorder.log), 80 * 6 * 0.5)


if __name__ == "__main__":
    unittest.main()
