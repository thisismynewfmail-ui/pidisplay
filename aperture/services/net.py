"""
Wi-Fi and network state, via NetworkManager.

``nmcli`` is used in its terse mode (``-t``), which emits colon-separated
fields and is the only output of these tools that is actually safe to parse --
the human-readable table reflows with terminal width and localises its headers.
Escaped colons inside values (common in SSIDs and in MAC addresses) are handled
by the field splitter below.

If NetworkManager is not present, every call reports that cleanly rather than
failing: a Pi configured with dhcpcd and wpa_supplicant still runs this program
perfectly well, it just cannot change networks from the settings menu.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import List, Optional

from .shell import Result, have, run


@dataclass
class AccessPoint:
    ssid: str
    signal: int = 0
    security: str = ""
    active: bool = False
    channel: str = ""

    @property
    def secured(self) -> bool:
        return bool(self.security) and self.security not in ("--", "none")

    def bars(self, width: int = 4) -> int:
        """Signal strength mapped onto *width* bars."""
        return max(0, min(width, round(self.signal / 100.0 * width)))


@dataclass
class NetStatus:
    available: bool = False          # NetworkManager present
    connected: bool = False
    ssid: str = ""
    ip: str = ""
    signal: int = 0
    interface: str = ""
    hostname: str = ""
    error: str = ""

    def summary(self) -> str:
        if not self.available:
            return "NO NETWORKMANAGER"
        if self.connected:
            return self.ssid or self.interface or "LINK UP"
        return "OFFLINE"


def _split_terse(line: str) -> List[str]:
    """Split an ``nmcli -t`` line, honouring backslash-escaped colons."""
    fields: List[str] = []
    current: List[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def available() -> bool:
    return have("nmcli")


def status() -> NetStatus:
    state = NetStatus(available=available(), hostname=socket.gethostname())
    state.ip = primary_ip()
    if not state.available:
        state.connected = bool(state.ip)
        return state

    result = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                  "device", "status"], timeout=6.0)
    if not result.ok:
        state.error = result.err[:60]
        return state

    for line in result.lines():
        device, kind, device_state, connection = (_split_terse(line) + ["", "", "", ""])[:4]
        if kind == "wifi" and device_state == "connected":
            state.connected = True
            state.ssid = connection
            state.interface = device
            break
        if kind == "ethernet" and device_state == "connected" and not state.connected:
            state.connected = True
            state.interface = device
            state.ssid = connection

    if state.connected and state.interface:
        state.signal = _active_signal()
    return state


def _active_signal() -> int:
    result = run(["nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi", "list"],
                 timeout=8.0)
    for line in result.lines():
        fields = _split_terse(line)
        if len(fields) >= 2 and fields[0] == "yes":
            try:
                return int(fields[1])
            except ValueError:
                return 0
    return 0


def primary_ip() -> str:
    """The address this host would use to reach the outside world.

    Opening a UDP socket to a public address resolves the routing question
    without sending a packet, which beats parsing ``ip addr`` and picking the
    wrong interface on a host with several.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            sock.connect(("192.0.2.1", 9))       # TEST-NET-1: never routed
            return sock.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


def scan(rescan: bool = True) -> List[AccessPoint]:
    """List visible access points, strongest first, one entry per SSID."""
    if not available():
        return []
    args = ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY,CHAN",
            "device", "wifi", "list"]
    if rescan:
        args.append("--rescan")
        args.append("yes")
    result = run(args, timeout=25.0)
    if not result.ok:
        return []

    best = {}
    for line in result.lines():
        fields = _split_terse(line)
        if len(fields) < 4:
            continue
        active, ssid, signal, security = fields[0], fields[1], fields[2], fields[3]
        channel = fields[4] if len(fields) > 4 else ""
        if not ssid:
            continue                       # hidden network: nothing to show
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        point = AccessPoint(ssid=ssid, signal=strength, security=security,
                            active=(active == "yes"), channel=channel)
        # The same SSID appears once per band and per repeater; keep the best.
        if ssid not in best or strength > best[ssid].signal:
            best[ssid] = point
    return sorted(best.values(), key=lambda ap: (-ap.active, -ap.signal))


def known_connections() -> List[str]:
    result = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                 timeout=8.0)
    out = []
    for line in result.lines():
        fields = _split_terse(line)
        if len(fields) >= 2 and "wireless" in fields[1]:
            out.append(fields[0])
    return out


def connect(ssid: str, password: Optional[str] = None,
            timeout: float = 45.0) -> Result:
    """Join *ssid*.  Reuses a stored profile when the password is omitted."""
    if not available():
        return Result(False, err="nmcli not installed")
    if password:
        return run(["nmcli", "device", "wifi", "connect", ssid,
                    "password", password], timeout=timeout)
    if ssid in known_connections():
        return run(["nmcli", "connection", "up", ssid], timeout=timeout)
    return run(["nmcli", "device", "wifi", "connect", ssid], timeout=timeout)


def disconnect_wifi() -> Result:
    result = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
                 timeout=6.0)
    for line in result.lines():
        fields = _split_terse(line)
        if len(fields) >= 2 and fields[1] == "wifi":
            return run(["nmcli", "device", "disconnect", fields[0]], timeout=20.0)
    return Result(False, err="no wifi device")


def forget(ssid: str) -> Result:
    return run(["nmcli", "connection", "delete", ssid], timeout=15.0)
