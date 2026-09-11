"""
Bluetooth control, aimed squarely at getting a keyboard paired.

This exists because of a bootstrapping problem that the rest of the program is
organised around: if the only keyboard is a Bluetooth one and it is not yet
paired, there is no way to type -- so there is no way to drive a menu to pair
it.  The boot sequence therefore runs this module *before* anything else and
drives it without operator input: power the adapter on, reconnect anything
already trusted, and only if that fails fall back to a scan-and-pair wizard
that can be driven by the four buttons on the panel or by any USB keyboard.

``bluetoothctl`` is held open as a long-lived session rather than invoked per
command.  Pairing is a conversation -- the agent prints a passkey partway
through and expects a reply -- and that conversation cannot survive being
chopped into separate processes.  Passkey prompts are surfaced so the display
can show the operator the six digits to type on the keyboard being paired,
which is exactly how this pairing mode works and is otherwise invisible to
anyone without a terminal attached.
"""

from __future__ import annotations

import collections
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

from .shell import have, run

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MAC = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
_DEVICE_LINE = re.compile(r"Device\s+" + _MAC + r"\s*(.*)")
_NEW_DEVICE = re.compile(r"\[\s*(NEW|CHG|DEL)\s*\]\s*Device\s+" + _MAC + r"\s*(.*)")
_PASSKEY = re.compile(r"Passkey:?\s*(\d{1,6})", re.I)
_CONFIRM = re.compile(r"Confirm passkey\s*(\d+)", re.I)
_PINCODE = re.compile(r"PIN code:?\s*(\w+)", re.I)

#: Device classes that are plausibly a keyboard.
_KEYBOARD_ICONS = ("input-keyboard", "input-tablet", "input-gaming")


@dataclass
class BTDevice:
    mac: str
    name: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    icon: str = ""
    rssi: int = 0
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def is_keyboard(self) -> bool:
        if self.icon in _KEYBOARD_ICONS:
            return True
        lowered = self.name.lower()
        return any(word in lowered for word in ("keyboard", "keypad", "kbd"))

    def label(self, width: int = 18) -> str:
        name = self.name or self.mac
        return name[:width]

    def status_char(self) -> str:
        if self.connected:
            return "*"
        if self.paired:
            return "+"
        return " "


def available() -> bool:
    return have("bluetoothctl")


def adapter_present() -> bool:
    result = run(["bluetoothctl", "list"], timeout=6.0)
    return result.ok and bool(result.lines())


class BluetoothSession:
    """A long-lived ``bluetoothctl`` conversation."""

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen] = None
        self.devices: Dict[str, BTDevice] = {}
        self.log: Deque[str] = collections.deque(maxlen=200)
        self.passkey: str = ""          # digits to type on the remote keyboard
        self.prompt: str = ""           # human-readable pairing prompt
        self.last_result: str = ""
        self.scanning = False
        self.powered = False
        self.error = ""
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._on_event: Optional[Callable[[str], None]] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, on_event: Optional[Callable[[str], None]] = None) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        if not available():
            self.error = "bluetoothctl not installed"
            return False
        self._on_event = on_event
        try:
            self.process = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return False

        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="bluetoothctl")
        self._reader.start()

        # KeyboardDisplay is the right agent capability for pairing a keyboard:
        # it lets the adapter display a passkey for the operator to type on the
        # device, which is how HID keyboards authenticate.
        self.send("power on")
        self.send("agent KeyboardDisplay")
        self.send("default-agent")
        time.sleep(0.4)
        self.powered = True
        return True

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.scan(False)
            self.send("quit")
            self.process.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            try:
                self.process.kill()
            except OSError:
                pass
        self.process = None
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # -- command channel ----------------------------------------------------

    def send(self, command: str) -> bool:
        if not self.alive or self.process.stdin is None:
            return False
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            return False
        with self._lock:
            self.log.append("> " + command)
        return True

    def answer(self, text: str) -> bool:
        """Reply to an agent prompt (a passkey, or yes/no)."""
        ok = self.send(text)
        if ok:
            with self._lock:
                self.prompt = ""
        return ok

    # -- output parsing -----------------------------------------------------

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for raw in process.stdout:
                line = _ANSI.sub("", raw).replace("\r", "").strip()
                # bluetoothctl decorates its prompt; strip it so the parsing
                # below sees the event text alone.
                line = re.sub(r"^\[[^\]]*\]#\s*", "", line)
                if not line:
                    continue
                with self._lock:
                    self.log.append(line)
                self._parse(line)
                if self._on_event is not None:
                    try:
                        self._on_event(line)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass

    def _parse(self, line: str) -> None:
        match = _NEW_DEVICE.search(line)
        if match:
            action, mac, rest = match.group(1), match.group(2), match.group(3)
            with self._lock:
                if action == "DEL":
                    self.devices.pop(mac, None)
                    return
                device = self.devices.setdefault(mac, BTDevice(mac=mac))
                device.last_seen = time.monotonic()
                self._apply_attribute(device, rest)
            return

        match = _PASSKEY.search(line)
        if match and "Enter" not in line:
            with self._lock:
                self.passkey = match.group(1).zfill(6)
                self.prompt = ""
            return

        match = _CONFIRM.search(line)
        if match:
            with self._lock:
                self.passkey = match.group(1)
                self.prompt = "confirm"
            return

        match = _PINCODE.search(line)
        if match:
            with self._lock:
                self.passkey = match.group(1)
            return

        if "Enter passkey" in line or "Enter PIN code" in line:
            with self._lock:
                self.prompt = "enter"
            return

        lowered = line.lower()
        for marker in ("pairing successful", "connection successful",
                       "failed to pair", "failed to connect",
                       "authentication failed", "not available",
                       "device not available", "already exists"):
            if marker in lowered:
                with self._lock:
                    self.last_result = line
                return

    @staticmethod
    def _apply_attribute(device: BTDevice, rest: str) -> None:
        """Fold a ``[CHG] Device <mac> Key: Value`` fragment into the record."""
        if not rest:
            return
        key, _, value = rest.partition(":")
        key, value = key.strip(), value.strip()
        if not value:
            # A [NEW] line carries the name with no key/value structure.
            if not device.name and rest.strip():
                device.name = rest.strip()
            return
        lowered = key.lower()
        if lowered in ("name", "alias") and value:
            device.name = value
        elif lowered == "paired":
            device.paired = value.lower() in ("yes", "1", "true")
        elif lowered == "trusted":
            device.trusted = value.lower() in ("yes", "1", "true")
        elif lowered == "connected":
            device.connected = value.lower() in ("yes", "1", "true")
        elif lowered == "icon":
            device.icon = value
        elif lowered == "rssi":
            try:
                device.rssi = int(value)
            except ValueError:
                pass

    # -- operations ---------------------------------------------------------

    def scan(self, enable: bool = True) -> None:
        self.scanning = enable
        self.send("scan " + ("on" if enable else "off"))

    def refresh_known(self) -> None:
        """Merge in devices bluez already knows about, with their state."""
        for command, flag in (("devices Paired", "paired"),
                              ("devices Connected", "connected"),
                              ("devices", "")):
            result = run(["bluetoothctl"] + command.split(), timeout=8.0)
            if not result.ok:
                continue
            for line in result.lines():
                match = _DEVICE_LINE.search(_ANSI.sub("", line))
                if not match:
                    continue
                mac, name = match.group(1), match.group(2).strip()
                with self._lock:
                    device = self.devices.setdefault(mac, BTDevice(mac=mac))
                    if name:
                        device.name = name
                    if flag == "paired":
                        device.paired = True
                    elif flag == "connected":
                        device.connected = True

    def inspect(self, mac: str) -> BTDevice:
        """Fill in a device's details from ``bluetoothctl info``."""
        with self._lock:
            device = self.devices.setdefault(mac, BTDevice(mac=mac))
        result = run(["bluetoothctl", "info", mac], timeout=8.0)
        if not result.ok:
            return device
        for line in result.lines():
            clean = _ANSI.sub("", line).strip()
            with self._lock:
                self._apply_attribute(device, clean)
        return device

    def pair(self, mac: str) -> None:
        with self._lock:
            self.passkey = ""
            self.prompt = ""
            self.last_result = ""
        self.send(f"pair {mac}")

    def trust(self, mac: str) -> None:
        self.send(f"trust {mac}")

    def connect(self, mac: str) -> None:
        with self._lock:
            self.last_result = ""
        self.send(f"connect {mac}")

    def remove(self, mac: str) -> None:
        self.send(f"remove {mac}")

    # -- views --------------------------------------------------------------

    def snapshot(self, keyboards_first: bool = True) -> List[BTDevice]:
        with self._lock:
            devices = list(self.devices.values())
        def sort_key(device: BTDevice):
            return (
                not device.connected,
                not (keyboards_first and device.is_keyboard),
                not device.paired,
                -device.rssi,
                device.name or device.mac,
            )
        return sorted(devices, key=sort_key)

    def keyboards(self) -> List[BTDevice]:
        return [d for d in self.snapshot() if d.is_keyboard]

    def state(self) -> Dict[str, str]:
        with self._lock:
            return {"passkey": self.passkey, "prompt": self.prompt,
                    "result": self.last_result}

    def tail(self, count: int = 10) -> List[str]:
        with self._lock:
            return list(self.log)[-count:]


def reconnect_trusted(timeout: float = 12.0) -> List[str]:
    """Connect every trusted, currently-disconnected device.

    Run unattended at boot: a keyboard that was paired on a previous run should
    simply work on the next one without the operator being asked anything.
    """
    if not available():
        return []
    result = run(["bluetoothctl", "devices", "Paired"], timeout=8.0)
    if not result.ok:
        return []
    reconnected = []
    deadline = time.monotonic() + timeout
    for line in result.lines():
        if time.monotonic() > deadline:
            break
        match = _DEVICE_LINE.search(_ANSI.sub("", line))
        if not match:
            continue
        mac = match.group(1)
        info = run(["bluetoothctl", "info", mac], timeout=5.0)
        if "Connected: yes" in info.out:
            continue
        if "Trusted: yes" not in info.out:
            continue
        attempt = run(["bluetoothctl", "connect", mac], timeout=10.0)
        if "successful" in attempt.out.lower():
            reconnected.append(match.group(2).strip() or mac)
    return reconnected
