"""
Keyboard capture: every keyboard attached to the machine, plus stdin.

Requirements this module exists to satisfy:

  * A keyboard plugged into any USB port, or paired over Bluetooth, must drive
    the UI -- with no X server, no desktop, and no terminal focus.  That rules
    out reading stdin alone and means reading ``/dev/input/event*`` directly.

  * Keyboards come and go.  A Bluetooth keyboard in particular may not exist
    when the program starts; the whole first stage of the boot sequence is
    built around waiting for one.  So devices are rescanned continuously and
    hot-plugged without restarting anything.

  * Keystrokes must not leak to the console behind the display.  Without that,
    everything typed at the chatbot is also typed at a login shell on tty1.
    The reader takes an exclusive grab (``EVIOCGRAB``) on each keyboard, which
    the kernel releases automatically if this process dies.

  * Running over SSH, with no keyboard on the Pi at all, must still work for
    development.  So stdin is read in parallel and merged into the same queue.

The event-device decoder is written against the kernel ABI directly rather than
depending on python-evdev: it is about eighty lines, it removes a build
dependency that needs a compiler on a Pi, and it makes the hot path a single
``struct.unpack_from`` per event.
"""

from __future__ import annotations

import errno
import fcntl
import os
import queue
import re
import select
import struct
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from . import keys as K
from .keys import KeyEvent

# struct input_event: two time fields (native long), then type, code, value.
_EVENT_FORMAT = "llHHi" if struct.calcsize("l") == 8 else "iiHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)

# EVIOCGRAB == _IOW('E', 0x90, int)
_EVIOCGRAB = 0x40044590

_INPUT_DIR = "/dev/input"
_PROC_DEVICES = "/proc/bus/input/devices"


@dataclass
class KeyboardInfo:
    """A keyboard the reader has found."""

    path: str
    name: str
    grabbed: bool = False
    bus: str = ""

    @property
    def is_bluetooth(self) -> bool:
        return self.bus == "0005"

    @property
    def is_usb(self) -> bool:
        return self.bus == "0003"

    def transport(self) -> str:
        if self.is_bluetooth:
            return "BT"
        if self.is_usb:
            return "USB"
        return "HID"


def enumerate_keyboards() -> List[KeyboardInfo]:
    """List event devices the kernel considers keyboards.

    ``/proc/bus/input/devices`` is authoritative here: the kernel attaches the
    ``kbd`` handler to exactly those devices that produce console keystrokes,
    which is precisely the set we want.  Matching on capability bitmasks
    instead sweeps up power buttons, lid switches and the consumer-control
    interface that every gaming mouse presents.
    """
    found: List[KeyboardInfo] = []
    try:
        with open(_PROC_DEVICES, "r", errors="replace") as handle:
            blocks = handle.read().split("\n\n")
    except OSError:
        return _enumerate_fallback()

    for block in blocks:
        if "Handlers=" not in block:
            continue
        name_match = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=(.*)", block)
        ident = re.search(r"I: Bus=([0-9a-fA-F]+)", block)
        if not handlers:
            continue
        handler_list = handlers.group(1).split()
        if "kbd" not in handler_list:
            continue
        event_node = next((h for h in handler_list if h.startswith("event")), None)
        if not event_node:
            continue
        path = os.path.join(_INPUT_DIR, event_node)
        if not os.path.exists(path):
            continue
        found.append(KeyboardInfo(
            path=path,
            name=name_match.group(1) if name_match else event_node,
            bus=ident.group(1).lower() if ident else "",
        ))
    return found


def _enumerate_fallback() -> List[KeyboardInfo]:
    """Used when /proc is unavailable: take every readable event device."""
    out = []
    try:
        names = sorted(n for n in os.listdir(_INPUT_DIR) if n.startswith("event"))
    except OSError:
        return out
    for name in names:
        path = os.path.join(_INPUT_DIR, name)
        if os.access(path, os.R_OK):
            out.append(KeyboardInfo(path=path, name=name))
    return out


class _EventDevice:
    """One open ``/dev/input/eventN`` with its own modifier state."""

    def __init__(self, info: KeyboardInfo, grab: bool):
        self.info = info
        self.fd = os.open(info.path, os.O_RDONLY | os.O_NONBLOCK)
        self.shift = False
        self.ctrl = False
        self.alt = False
        self.capslock = False
        self.numlock = True
        self._buffer = b""
        if grab:
            try:
                fcntl.ioctl(self.fd, _EVIOCGRAB, struct.pack("i", 1))
                info.grabbed = True
            except OSError:
                # Another process (often a display manager) holds the grab.
                # Not fatal: we still receive the events, they are just shared.
                info.grabbed = False

    def close(self) -> None:
        try:
            if self.info.grabbed:
                try:
                    fcntl.ioctl(self.fd, _EVIOCGRAB, struct.pack("i", 0))
                except OSError:
                    pass
            os.close(self.fd)
        except OSError:
            pass

    def read_events(self) -> List[KeyEvent]:
        try:
            chunk = os.read(self.fd, _EVENT_SIZE * 64)
        except BlockingIOError:
            return []
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.EBADF, errno.EIO):
                raise
            return []
        if not chunk:
            return []

        self._buffer += chunk
        events: List[KeyEvent] = []
        count = len(self._buffer) // _EVENT_SIZE
        for i in range(count):
            _, _, etype, code, value = struct.unpack_from(
                _EVENT_FORMAT, self._buffer, i * _EVENT_SIZE)
            if etype != K.EV_KEY:
                continue
            event = self._handle(code, value)
            if event is not None:
                events.append(event)
        self._buffer = self._buffer[count * _EVENT_SIZE:]
        return events

    def _handle(self, code: int, value: int) -> Optional[KeyEvent]:
        pressed = value in (1, 2)
        if code in (K.KEY_LEFTSHIFT, K.KEY_RIGHTSHIFT):
            self.shift = pressed
            return None
        if code in (K.KEY_LEFTCTRL, K.KEY_RIGHTCTRL):
            self.ctrl = pressed
            return None
        if code in (K.KEY_LEFTALT, K.KEY_RIGHTALT):
            self.alt = pressed
            return None
        if code == K.KEY_CAPSLOCK:
            if value == 1:
                self.capslock = not self.capslock
            return None
        if code == 69:  # NumLock
            if value == 1:
                self.numlock = not self.numlock
            return None
        if code in K.MODIFIER_CODES or value == 0:
            return None

        key, char = K.decode_keycode(code, self.shift, self.capslock, self.numlock)
        if key is None:
            return None
        if self.ctrl and key == K.CHAR:
            # Ctrl+<letter> is a command, not text.
            letter = K.letter_for(code)
            if letter:
                char = letter
        return KeyEvent(
            key=key,
            char=char,
            ctrl=self.ctrl,
            alt=self.alt,
            shift=self.shift,
            repeat=(value == 2),
            source=self.info.name,
        )


class _StdinReader:
    """ANSI escape decoder for stdin, so SSH sessions can drive the UI."""

    _ESCAPES = {
        "[A": K.UP, "[B": K.DOWN, "[C": K.RIGHT, "[D": K.LEFT,
        "OA": K.UP, "OB": K.DOWN, "OC": K.RIGHT, "OD": K.LEFT,
        "[H": K.HOME, "[F": K.END, "OH": K.HOME, "OF": K.END,
        "[1~": K.HOME, "[2~": K.INSERT, "[3~": K.DELETE, "[4~": K.END,
        "[5~": K.PGUP, "[6~": K.PGDN,
        "OP": K.F1, "OQ": K.F2, "OR": K.F3, "OS": K.F4,
        "[11~": K.F1, "[12~": K.F2, "[13~": K.F3, "[14~": K.F4,
        "[15~": K.F5, "[17~": K.F6, "[18~": K.F7, "[19~": K.F8,
        "[20~": K.F9, "[21~": K.F10, "[23~": K.F11, "[24~": K.F12,
        "[[A": K.F1, "[[B": K.F2, "[[C": K.F3, "[[D": K.F4, "[[E": K.F5,
    }

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved = None
        self._pending = ""
        self._last_escape = 0.0
        try:
            self._saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        except (termios.error, ValueError):
            self._saved = None

    @property
    def usable(self) -> bool:
        return self._saved is not None

    def restore(self) -> None:
        if self._saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except (termios.error, ValueError):
                pass
            self._saved = None

    def read_events(self) -> List[KeyEvent]:
        try:
            data = os.read(self.fd, 256)
        except (BlockingIOError, OSError):
            return []
        if not data:
            return []
        self._pending += data.decode("utf-8", errors="replace")
        return self._drain()

    def flush_pending_escape(self) -> List[KeyEvent]:
        """Emit a bare ESC once it is clear no sequence is following it.

        A lone Escape and the start of an arrow-key sequence are the same byte;
        the only way to tell them apart on a stream is to wait briefly.  25 ms
        is comfortably longer than a terminal takes to deliver the rest of a
        sequence and far shorter than a person can perceive.
        """
        if self._pending == "\x1b" and time.monotonic() - self._last_escape > 0.025:
            self._pending = ""
            return [KeyEvent(key=K.ESC, source="stdin")]
        return []

    def _drain(self) -> List[KeyEvent]:
        out: List[KeyEvent] = []
        while self._pending:
            ch = self._pending[0]
            if ch == "\x1b":
                consumed, event = self._match_escape(self._pending)
                if consumed == 0:
                    self._last_escape = time.monotonic()
                    break  # incomplete; wait for more bytes
                self._pending = self._pending[consumed:]
                if event is not None:
                    out.append(event)
                continue

            self._pending = self._pending[1:]
            if ch in ("\r", "\n"):
                out.append(KeyEvent(key=K.ENTER, source="stdin"))
            elif ch in ("\x7f", "\x08"):
                out.append(KeyEvent(key=K.BACKSPACE, source="stdin"))
            elif ch == "\t":
                out.append(KeyEvent(key=K.TAB, source="stdin"))
            elif ch < " ":
                letter = chr(ord(ch) + 96)
                out.append(KeyEvent(key=K.CHAR, char=letter, ctrl=True,
                                    source="stdin"))
            else:
                out.append(KeyEvent(key=K.CHAR, char=ch, source="stdin"))
        return out

    def _match_escape(self, data: str):
        body = data[1:]
        if not body:
            return 0, None
        if body[0] == "\x1b":
            return 1, KeyEvent(key=K.ESC, source="stdin")
        for sequence, key in self._ESCAPES.items():
            if body.startswith(sequence):
                return 1 + len(sequence), KeyEvent(key=key, source="stdin")
        # Could still be a prefix of a longer sequence.
        if any(s.startswith(body) for s in self._ESCAPES):
            return 0, None
        if body[0].isprintable():
            return 2, KeyEvent(key=K.CHAR, char=body[0], alt=True, source="stdin")
        return 2, None


class KeyboardHub:
    """Aggregates every keyboard plus stdin into a single event queue."""

    #: How often to look for newly attached keyboards.
    RESCAN_INTERVAL = 1.0

    def __init__(self, grab: bool = True, use_stdin: bool = True):
        self.grab = grab
        self.events: "queue.Queue[KeyEvent]" = queue.Queue()
        self._devices: Dict[str, _EventDevice] = {}
        self._failed: Set[str] = set()
        self._stdin: Optional[_StdinReader] = None
        self._want_stdin = use_stdin and sys.stdin.isatty()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self.permission_error = False
        self.last_error = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._want_stdin:
            reader = _StdinReader()
            self._stdin = reader if reader.usable else None
        self._scan()
        self._thread = threading.Thread(target=self._run, name="keyboard",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        with self._lock:
            for device in self._devices.values():
                device.close()
            self._devices.clear()
        if self._stdin is not None:
            self._stdin.restore()
            self._stdin = None

    # -- device management --------------------------------------------------

    def _scan(self) -> None:
        self._last_scan = time.monotonic()
        try:
            discovered = enumerate_keyboards()
        except Exception as exc:
            self.last_error = str(exc)
            return

        wanted = {info.path: info for info in discovered}
        with self._lock:
            for path in list(self._devices):
                if path not in wanted:
                    self._devices.pop(path).close()
                    self._failed.discard(path)

            for path, info in wanted.items():
                if path in self._devices or path in self._failed:
                    continue
                try:
                    self._devices[path] = _EventDevice(info, self.grab)
                except PermissionError:
                    self.permission_error = True
                    self._failed.add(path)
                    self.last_error = (
                        f"permission denied on {path}; add the user to the "
                        "'input' group or run via the provided service unit"
                    )
                except OSError as exc:
                    self._failed.add(path)
                    self.last_error = f"{path}: {exc}"

            # Give previously-failed nodes another chance once they disappear
            # and come back (replugging a keyboard is the usual fix).
            self._failed &= set(wanted)

    @property
    def devices(self) -> List[KeyboardInfo]:
        with self._lock:
            return [d.info for d in self._devices.values()]

    @property
    def device_count(self) -> int:
        with self._lock:
            return len(self._devices)

    @property
    def has_input(self) -> bool:
        """True when some source can deliver keystrokes."""
        return self.device_count > 0 or self._stdin is not None

    def describe_sources(self) -> List[str]:
        out = [f"{d.transport()} {d.name}" for d in self.devices]
        if self._stdin is not None:
            out.append("TTY stdin")
        return out

    # -- reader thread ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                fd_map = {d.fd: d for d in self._devices.values()}
            fds = list(fd_map)
            if self._stdin is not None:
                fds.append(self._stdin.fd)

            try:
                ready, _, _ = select.select(fds, [], [], 0.05) if fds else ([], [], [])
            except (OSError, ValueError):
                ready = []

            for fd in ready:
                if self._stdin is not None and fd == self._stdin.fd:
                    for event in self._stdin.read_events():
                        self.events.put(event)
                    continue
                device = fd_map.get(fd)
                if device is None:
                    continue
                try:
                    for event in device.read_events():
                        self.events.put(event)
                except OSError:
                    with self._lock:
                        self._devices.pop(device.info.path, None)
                    device.close()

            if self._stdin is not None:
                for event in self._stdin.flush_pending_escape():
                    self.events.put(event)

            if time.monotonic() - self._last_scan >= self.RESCAN_INTERVAL:
                self._scan()

            if not fds:
                time.sleep(0.05)

    # -- consumer API -------------------------------------------------------

    def poll(self) -> Optional[KeyEvent]:
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit: int = 32) -> List[KeyEvent]:
        out = []
        for _ in range(limit):
            event = self.poll()
            if event is None:
                break
            out.append(event)
        return out

    def flush(self) -> None:
        while self.poll() is not None:
            pass
