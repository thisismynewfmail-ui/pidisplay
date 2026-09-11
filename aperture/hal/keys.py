"""
Normalised key events and the Linux keycode tables behind them.

Two very different sources produce key events here -- raw ``/dev/input``
event devices and an ANSI terminal on stdin -- and the UI must not care which
one a keystroke came from.  Both are decoded into the vocabulary below.
"""

from __future__ import annotations

from dataclasses import dataclass

# Named keys.  Anything that produces a character arrives as CHAR with the
# character in :attr:`KeyEvent.char`.
CHAR = "CHAR"
UP, DOWN, LEFT, RIGHT = "UP", "DOWN", "LEFT", "RIGHT"
ENTER, ESC, TAB, BACKSPACE, DELETE = "ENTER", "ESC", "TAB", "BACKSPACE", "DELETE"
HOME, END, PGUP, PGDN, INSERT = "HOME", "END", "PGUP", "PGDN", "INSERT"
PLUS, MINUS = "PLUS", "MINUS"          # keypad +/-, mapped for the adjusters
F1, F2, F3, F4, F5, F6 = "F1", "F2", "F3", "F4", "F5", "F6"
F7, F8, F9, F10, F11, F12 = "F7", "F8", "F9", "F10", "F11", "F12"

FUNCTION_KEYS = (F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12)


@dataclass(frozen=True)
class KeyEvent:
    """One keystroke, normalised across input sources."""

    key: str
    char: str = ""
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    repeat: bool = False
    source: str = ""

    def is_char(self) -> bool:
        return self.key == CHAR and bool(self.char)

    def matches(self, key: str, ctrl: bool = False) -> bool:
        return self.key == key and self.ctrl == ctrl

    def describe(self) -> str:
        parts = []
        if self.ctrl:
            parts.append("Ctrl")
        if self.alt:
            parts.append("Alt")
        if self.key == CHAR:
            parts.append(self.char)
        else:
            parts.append(self.key)
        return "+".join(parts)


# --------------------------------------------------------------------------
# Linux input-event keycodes (linux/input-event-codes.h)
# --------------------------------------------------------------------------

EV_KEY = 0x01
EV_REP = 0x14

KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_CAPSLOCK = 58

MODIFIER_CODES = {
    KEY_LEFTCTRL, KEY_RIGHTCTRL,
    KEY_LEFTSHIFT, KEY_RIGHTSHIFT,
    KEY_LEFTALT, KEY_RIGHTALT,
    KEY_CAPSLOCK,
    125, 126,  # meta / super
}

#: Keycodes that map to a named key rather than a character.
NAMED_KEYS = {
    1: ESC,
    14: BACKSPACE,
    15: TAB,
    28: ENTER,
    96: ENTER,        # keypad enter
    59: F1, 60: F2, 61: F3, 62: F4, 63: F5, 64: F6,
    65: F7, 66: F8, 67: F9, 68: F10, 87: F11, 88: F12,
    102: HOME,
    103: UP,
    104: PGUP,
    105: LEFT,
    106: RIGHT,
    107: END,
    108: DOWN,
    109: PGDN,
    110: INSERT,
    111: DELETE,
    74: MINUS,        # keypad minus
    78: PLUS,         # keypad plus
}

#: US-layout character map: keycode -> (unshifted, shifted).
CHAR_KEYS = {
    2: ("1", "!"), 3: ("2", "@"), 4: ("3", "#"), 5: ("4", "$"), 6: ("5", "%"),
    7: ("6", "^"), 8: ("7", "&"), 9: ("8", "*"), 10: ("9", "("), 11: ("0", ")"),
    12: ("-", "_"), 13: ("=", "+"),
    16: ("q", "Q"), 17: ("w", "W"), 18: ("e", "E"), 19: ("r", "R"),
    20: ("t", "T"), 21: ("y", "Y"), 22: ("u", "U"), 23: ("i", "I"),
    24: ("o", "O"), 25: ("p", "P"), 26: ("[", "{"), 27: ("]", "}"),
    30: ("a", "A"), 31: ("s", "S"), 32: ("d", "D"), 33: ("f", "F"),
    34: ("g", "G"), 35: ("h", "H"), 36: ("j", "J"), 37: ("k", "K"),
    38: ("l", "L"), 39: (";", ":"), 40: ("'", '"'), 41: ("`", "~"),
    43: ("\\", "|"),
    44: ("z", "Z"), 45: ("x", "X"), 46: ("c", "C"), 47: ("v", "V"),
    48: ("b", "B"), 49: ("n", "N"), 50: ("m", "M"),
    51: (",", "<"), 52: (".", ">"), 53: ("/", "?"),
    55: ("*", "*"),
    57: (" ", " "),
    # Keypad digits are only characters when NumLock is on; the reader tracks
    # that and falls back to the navigation meanings below.
    71: ("7", "7"), 72: ("8", "8"), 73: ("9", "9"),
    75: ("4", "4"), 76: ("5", "5"), 77: ("6", "6"),
    79: ("1", "1"), 80: ("2", "2"), 81: ("3", "3"),
    82: ("0", "0"), 83: (".", "."),
    98: ("/", "/"),
}

#: Keypad navigation meanings used when NumLock is off.
KEYPAD_NAV = {
    71: HOME, 72: UP, 73: PGUP,
    75: LEFT, 77: RIGHT,
    79: END, 80: DOWN, 81: PGDN,
    82: INSERT, 83: DELETE,
}

#: Letters, for translating Ctrl+<letter> into a control code name.
_LETTERS = {code: pair[0] for code, pair in CHAR_KEYS.items()
            if pair[0].isalpha()}


def decode_keycode(code: int, shift: bool, capslock: bool, numlock: bool):
    """Translate a Linux keycode into ``(key, char)``.

    Returns ``(None, "")`` for keys this program has no meaning for, which the
    reader drops rather than surfacing as mystery events.
    """
    if code in NAMED_KEYS:
        return NAMED_KEYS[code], ""
    if code in KEYPAD_NAV and not numlock:
        return KEYPAD_NAV[code], ""
    pair = CHAR_KEYS.get(code)
    if pair is None:
        return None, ""
    ch = pair[1] if shift else pair[0]
    if capslock and ch.isalpha():
        ch = ch.upper() if not shift else ch.lower()
    return CHAR, ch


def letter_for(code: int) -> str:
    return _LETTERS.get(code, "")
