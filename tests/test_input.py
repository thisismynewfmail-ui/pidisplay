"""Keyboard decoding, for both input paths."""

import unittest

from aperture.hal import keys as K
from aperture.hal.keyboard import _StdinReader


def stdin_reader():
    reader = _StdinReader.__new__(_StdinReader)
    reader._pending = ""
    reader._last_escape = 0.0
    return reader


def decode(sequence):
    reader = stdin_reader()
    reader._pending = sequence
    return reader._drain()


class TestStdinDecoding(unittest.TestCase):
    def test_arrow_keys(self):
        for sequence, expected in (("\x1b[A", K.UP), ("\x1b[B", K.DOWN),
                                   ("\x1b[C", K.RIGHT), ("\x1b[D", K.LEFT),
                                   ("\x1bOA", K.UP), ("\x1bOD", K.LEFT)):
            self.assertEqual(decode(sequence)[0].key, expected, sequence)

    def test_settings_key(self):
        """F7 opens settings, so both of its common encodings must decode."""
        self.assertEqual(decode("\x1b[18~")[0].key, K.F7)
        self.assertEqual(decode("\x1b[19~")[0].key, K.F8)

    def test_function_keys_in_both_encodings(self):
        self.assertEqual(decode("\x1bOP")[0].key, K.F1)
        self.assertEqual(decode("\x1b[11~")[0].key, K.F1)

    def test_navigation_keys(self):
        for sequence, expected in (("\x1b[5~", K.PGUP), ("\x1b[6~", K.PGDN),
                                   ("\x1b[3~", K.DELETE), ("\x1b[H", K.HOME),
                                   ("\x1b[F", K.END)):
            self.assertEqual(decode(sequence)[0].key, expected, sequence)

    def test_editing_keys(self):
        self.assertEqual(decode("\r")[0].key, K.ENTER)
        self.assertEqual(decode("\n")[0].key, K.ENTER)
        self.assertEqual(decode("\x7f")[0].key, K.BACKSPACE)
        self.assertEqual(decode("\t")[0].key, K.TAB)

    def test_control_characters_become_ctrl_chords(self):
        event = decode("\x17")[0]
        self.assertTrue(event.ctrl)
        self.assertEqual(event.char, "w")

    def test_plain_text(self):
        events = decode("abc")
        self.assertEqual([e.char for e in events], ["a", "b", "c"])
        self.assertTrue(all(e.key == K.CHAR for e in events))

    def test_incomplete_sequence_is_held_back(self):
        """Half an escape sequence must not be printed as literal text."""
        reader = stdin_reader()
        reader._pending = "\x1b["
        self.assertEqual(reader._drain(), [])
        reader._pending += "A"
        self.assertEqual(reader._drain()[0].key, K.UP)

    def test_double_escape_is_one_escape(self):
        self.assertEqual([e.key for e in decode("\x1b\x1b")], [K.ESC])


class TestKeycodeDecoding(unittest.TestCase):
    def test_letters_respect_shift(self):
        self.assertEqual(K.decode_keycode(30, False, False, True), (K.CHAR, "a"))
        self.assertEqual(K.decode_keycode(30, True, False, True), (K.CHAR, "A"))

    def test_capslock_inverts_only_letters(self):
        self.assertEqual(K.decode_keycode(30, False, True, True), (K.CHAR, "A"))
        self.assertEqual(K.decode_keycode(2, False, True, True), (K.CHAR, "1"))

    def test_shift_with_capslock_gives_lowercase(self):
        self.assertEqual(K.decode_keycode(30, True, True, True), (K.CHAR, "a"))

    def test_function_keys(self):
        self.assertEqual(K.decode_keycode(65, False, False, True)[0], K.F7)
        self.assertEqual(K.decode_keycode(62, False, False, True)[0], K.F4)

    def test_keypad_follows_numlock(self):
        self.assertEqual(K.decode_keycode(72, False, False, True), (K.CHAR, "8"))
        self.assertEqual(K.decode_keycode(72, False, False, False)[0], K.UP)

    def test_keypad_plus_minus_are_named(self):
        """The context adjuster binds these, so they must not be characters."""
        self.assertEqual(K.decode_keycode(78, False, False, True)[0], K.PLUS)
        self.assertEqual(K.decode_keycode(74, False, False, True)[0], K.MINUS)

    def test_unknown_keycodes_are_dropped(self):
        self.assertEqual(K.decode_keycode(240, False, False, True), (None, ""))

    def test_both_enter_keys(self):
        self.assertEqual(K.decode_keycode(28, False, False, True)[0], K.ENTER)
        self.assertEqual(K.decode_keycode(96, False, False, True)[0], K.ENTER)


class TestTextField(unittest.TestCase):
    def setUp(self):
        from aperture.ui.widgets import TextField
        self.field = TextField("hello world")

    def test_window_follows_the_caret(self):
        self.field.end()
        visible, caret, cut_left, _ = self.field.render(5)
        # With the caret at the end of the line it occupies the last column and
        # the visible text is one shorter than the window.
        self.assertEqual(visible, "orld")
        self.assertEqual(caret, 4)
        self.assertTrue(cut_left)

    def test_window_is_full_when_caret_is_inside_the_text(self):
        self.field.cursor = 4
        visible, caret, _, cut_right = self.field.render(5)
        self.assertEqual(len(visible), 5)
        self.assertTrue(cut_right)

    def test_caret_stays_inside_the_window(self):
        for width in range(1, 12):
            for cursor in range(0, len(self.field.text) + 1):
                self.field.cursor = cursor
                _, caret, _, _ = self.field.render(width)
                self.assertTrue(0 <= caret < width)

    def test_no_right_cut_when_caret_is_at_the_end(self):
        self.field.end()
        _, _, _, cut_right = self.field.render(5)
        self.assertFalse(cut_right)

    def test_word_deletion(self):
        self.field.end()
        self.field.delete_word()
        self.assertEqual(self.field.text, "hello ")

    def test_word_movement(self):
        self.field.home()
        self.field.move_word(1)
        self.assertEqual(self.field.cursor, 6)

    def test_insert_sanitises(self):
        self.field.clear()
        self.field.insert("café — ok")
        self.assertTrue(self.field.text.isascii())

    def test_limit_is_enforced(self):
        from aperture.ui.widgets import TextField
        field = TextField("", limit=4)
        self.assertTrue(field.insert("abcd"))
        self.assertFalse(field.insert("e"))


if __name__ == "__main__":
    unittest.main()
