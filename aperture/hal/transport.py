"""
Byte transports for the PCF8574 I2C backpack.

The driver in :mod:`aperture.hal.lcd` never talks to a bus directly.  It hands
a transport a flat list of port bytes and the transport is responsible for
getting them onto the wire (or into an emulator) as efficiently as it can.

Batching matters more here than it looks.  Each displayed character costs six
port writes -- three per nibble, because the PCF8574 changes all eight outputs
simultaneously and the data lines must be stable before E rises.  Issuing those
as individual SMBus transactions adds an address phase and two stop/start gaps
per byte, which on a 100 kHz bus is most of the cost.  Writing them as one
multi-byte I2C message instead lets the whole burst stream out back-to-back,
which is roughly a 2.5x throughput improvement and is the difference between
animation that reads as smooth and animation that reads as broken.
"""

from __future__ import annotations

import threading
from typing import List, Optional, Sequence


class TransportError(RuntimeError):
    """Raised when the display bus cannot be reached."""


class Transport:
    """Interface: accept a burst of PCF8574 port bytes."""

    #: Largest burst to hand the kernel in one transaction.
    max_burst = 512

    def write(self, data: Sequence[int]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class I2CTransport(Transport):
    """Real hardware, via ``smbus2`` on ``/dev/i2c-N``."""

    def __init__(self, bus: int = 1, address: int = 0x27):
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError as exc:  # pragma: no cover - hardware path
            raise TransportError(
                "python package 'smbus2' is not installed; run install.sh"
            ) from exc
        self._i2c_msg = i2c_msg
        self.bus_number = bus
        self.address = address
        try:
            self._bus = SMBus(bus)
        except (OSError, IOError) as exc:  # pragma: no cover - hardware path
            raise TransportError(
                f"cannot open I2C bus {bus} (/dev/i2c-{bus}): {exc}. "
                "Enable I2C with raspi-config and check group membership."
            ) from exc
        self._lock = threading.Lock()

    def write(self, data: Sequence[int]) -> None:
        if not data:
            return
        with self._lock:
            for start in range(0, len(data), self.max_burst):
                chunk = bytes(data[start:start + self.max_burst])
                msg = self._i2c_msg.write(self.address, chunk)
                try:
                    self._bus.i2c_rdwr(msg)
                except (OSError, IOError) as exc:  # pragma: no cover
                    raise TransportError(
                        f"I2C write to 0x{self.address:02X} failed: {exc}"
                    ) from exc

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass


class EmulatedTransport(Transport):
    """Feeds an in-process :class:`~aperture.hal.emulator.HD44780Emulator`.

    An optional *delay_model* makes the emulator run at the same speed the real
    bus would, so timing bugs (an animation that asks for more redraws per
    second than 100 kHz I2C can deliver) show up in the simulator too.
    """

    def __init__(self, emulator=None, cols: int = 20, rows: int = 4,
                 bus_hz: Optional[int] = None, pinmap=None):
        from .emulator import HD44780Emulator
        self.emulator = emulator or HD44780Emulator(cols=cols, rows=rows,
                                                    pinmap=pinmap)
        self.bus_hz = bus_hz
        self._lock = threading.Lock()

    def write(self, data: Sequence[int]) -> None:
        with self._lock:
            self.emulator.write_bytes(data)
        if self.bus_hz:
            import time
            # Nine bits per byte on the wire (eight data plus the ACK slot).
            time.sleep(len(data) * 9 / float(self.bus_hz))


class RecordingTransport(Transport):
    """Captures every byte; used by the test-suite to assert on wire traffic."""

    def __init__(self, inner: Optional[Transport] = None):
        self.inner = inner
        self.log: List[int] = []
        self.bursts: List[List[int]] = []

    def write(self, data: Sequence[int]) -> None:
        self.log.extend(data)
        self.bursts.append(list(data))
        if self.inner is not None:
            self.inner.write(data)

    def reset(self) -> None:
        self.log.clear()
        self.bursts.clear()


class NullTransport(Transport):
    """Discards everything.  Lets the UI run with no display attached."""

    def write(self, data: Sequence[int]) -> None:
        pass
