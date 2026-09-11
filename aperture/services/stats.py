"""Host telemetry for the diagnostics screen.

Everything here reads from procfs and sysfs rather than shelling out, so it is
cheap enough to sample once a second from the render loop without showing up in
a profile.  The one exception is the throttling flag, which only the firmware
knows; that is polled far less often.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict

from .shell import run

#: Bits returned by ``vcgencmd get_throttled``.  The sticky (since-boot) bits
#: are the ones worth reporting: a Pi that browned out during model load will
#: have cleared the live bit by the time anyone looks.
_THROTTLE_BITS = {
    0: "UNDERVOLT",
    1: "FREQ CAP",
    2: "THROTTLED",
    3: "TEMP LIMIT",
    16: "UNDERVOLT (SEEN)",
    17: "FREQ CAP (SEEN)",
    18: "THROTTLED (SEEN)",
    19: "TEMP LIMIT (SEEN)",
}


@dataclass
class HostStats:
    cpu_temp: float = 0.0
    load1: float = 0.0
    mem_total_mb: int = 0
    mem_available_mb: int = 0
    uptime_s: float = 0.0
    cpu_mhz: int = 0
    model: str = ""
    throttled: str = ""

    @property
    def mem_used_mb(self) -> int:
        return max(0, self.mem_total_mb - self.mem_available_mb)

    @property
    def mem_fraction(self) -> float:
        return (self.mem_used_mb / self.mem_total_mb) if self.mem_total_mb else 0.0

    def uptime_text(self) -> str:
        seconds = int(self.uptime_s)
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
        return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def cpu_temperature() -> float:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value / 1000.0 if value > 1000 else value


def cpu_mhz() -> int:
    raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    try:
        return int(raw) // 1000 if raw else 0
    except ValueError:
        return 0


def board_model() -> str:
    raw = _read("/proc/device-tree/model").replace("\x00", "")
    if raw:
        return raw
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("Model"):
            return line.split(":", 1)[-1].strip()
    return os.uname().machine


def memory() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) // 1024      # kB -> MiB
    return out


_throttle_cache = ("", 0.0)


def throttled(max_age: float = 30.0) -> str:
    global _throttle_cache
    value, when = _throttle_cache
    if time.monotonic() - when < max_age:
        return value
    result = run(["vcgencmd", "get_throttled"], timeout=3.0)
    text = ""
    if result.ok and "=" in result.out:
        try:
            flags = int(result.out.split("=", 1)[1].strip(), 16)
        except ValueError:
            flags = 0
        live = [name for bit, name in _THROTTLE_BITS.items()
                if bit < 16 and flags & (1 << bit)]
        sticky = [name for bit, name in _THROTTLE_BITS.items()
                  if bit >= 16 and flags & (1 << bit)]
        text = ", ".join(live) or (", ".join(sticky) if sticky else "")
    _throttle_cache = (text, time.monotonic())
    return text


def sample() -> HostStats:
    mem = memory()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    uptime_raw = _read("/proc/uptime").split()
    return HostStats(
        cpu_temp=cpu_temperature(),
        load1=load1,
        mem_total_mb=mem.get("MemTotal", 0),
        mem_available_mb=mem.get("MemAvailable", 0),
        uptime_s=float(uptime_raw[0]) if uptime_raw else 0.0,
        cpu_mhz=cpu_mhz(),
        model=board_model(),
        throttled=throttled(),
    )


def disk_free_gib(path: str) -> float:
    try:
        stat = os.statvfs(path)
    except OSError:
        return 0.0
    return stat.f_bavail * stat.f_frsize / (1024 ** 3)
