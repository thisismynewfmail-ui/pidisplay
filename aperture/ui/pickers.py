"""
Choosers for the three things that cannot be typed in as a number: which model
to load, which wireless network to join, and which Bluetooth keyboard to pair.

All three do slow work -- a directory walk that reads GGUF headers, a Wi-Fi
scan, a Bluetooth discovery -- so all three run it on a background task and
keep animating while it happens.  None of them block the render loop.
"""

from __future__ import annotations

import time
from typing import List, Optional

from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent
from ..hal import keys as K
from ..llm import models as model_scan
from ..services import bt as bt_service
from ..services import net as net_service
from ..services.tasks import AsyncTask
from .screen import Screen
from .widgets import Activity, ListView, draw_title
from . import text as T


class _BusyMixin:
    """Shared 'working on it' row."""

    def draw_busy(self, frame: Frame, label: str, row: int = 2) -> None:
        spinner = self.activity.spinner(self.display, "spin")
        elapsed = self.activity.elapsed
        frame.row_text(row, T.fit(f"{spinner} {label} {elapsed:.0f}s",
                                  frame.cols, align="center"))


class ModelPickerScreen(Screen, _BusyMixin):
    """Choose a GGUF file from the models directory."""

    bank = G.BANK_MENU
    title = "MODELS"

    def __init__(self, app):
        super().__init__(app)
        self.list = ListView(rows=3)
        self.models: List[model_scan.ModelInfo] = []
        self.activity = Activity(fps=8.0)
        self.task: Optional[AsyncTask] = None
        self.error = ""

    def on_enter(self) -> None:
        self._rescan()

    def _rescan(self) -> None:
        directory = self.app.config.models_dir
        self.activity.reset()
        self.task = AsyncTask(lambda: model_scan.scan_models(directory),
                              name="model-scan")

    def update(self, dt: float) -> None:
        if self.task is not None and self.task.finished:
            if self.task.error:
                self.error = self.task.error
            else:
                self.models = self.task.result or []
                self.list.set_count(len(self.models))
                current = str(self.app.config.get("engine.model", ""))
                for index, model in enumerate(self.models):
                    if current in (model.filename, model.stem, model.path):
                        self.list.select(index)
                        break
            self.task = None

    def on_key(self, event: KeyEvent) -> bool:
        if event.key == K.UP:
            self.list.move(-1)
            return True
        if event.key == K.DOWN:
            self.list.move(1)
            return True
        if event.key in (K.PGUP, K.PGDN):
            self.list.page(-1 if event.key == K.PGUP else 1)
            return True
        if event.key == K.F5:
            self._rescan()
            return True
        if event.key == K.F1 or event.key == K.RIGHT:
            self._inspect()
            return True
        if event.key == K.ENTER:
            self._select()
            return True
        if event.key in (K.ESC, K.LEFT, K.F3):
            self.close(None)
            return True
        return False

    def _inspect(self) -> None:
        if not self.models:
            return
        from .dialogs import MessageScreen
        model = self.models[self.list.index]
        self.app.push(MessageScreen(self.app, "MODEL",
                                    "\n".join(model.describe_lines())))

    def _select(self) -> None:
        if not self.models:
            self.app.notify("NO MODELS FOUND", 3.0)
            return
        model = self.models[self.list.index]
        current = str(self.app.config.get("engine.model", ""))
        if current == model.filename and self.app.engine_ready:
            self.close(None)
            return

        def _apply() -> None:
            self.app.config.set("engine.model", model.filename)
            self.app.config.save()
            self._warn_context(model)
            self.app.pop(None)
            self.app.restart_engine(f"LOADING {model.stem[:12]}")

        # Name the model by what identifies it -- parameter count and
        # quantisation -- rather than by the tail of a truncated filename,
        # which reads as noise.
        if model.size_label and model.quantisation:
            title = f"LOAD {model.size_label} {model.quantisation}?"
        else:
            title = f"LOAD {model.short_name(13)}?"
        self.app.confirm(title, _apply,
                         detail=f"{model.stem}, {model.size_gib:.1f} GiB. "
                                "The engine restarts.")

    def _warn_context(self, model: model_scan.ModelInfo) -> None:
        """Lower an over-large context to what this model supports.

        Silently running a model past its trained window degrades output in
        ways that look like a bad model rather than a bad setting, so the
        window is clamped and the operator told, rather than left to discover
        it.
        """
        if not model.train_context:
            return
        requested = int(self.app.config.get("engine.context"))
        if requested > model.train_context:
            self.app.config.set("engine.context", model.train_context)
            self.app.notify(f"CTX -> {T.format_count(model.train_context)}", 3.0)

    def draw(self, frame: Frame) -> None:
        counter = (f"{self.list.index + 1}/{len(self.models)}"
                   if self.models else "")
        draw_title(frame, "MODELS", counter)

        if self.task is not None:
            self.draw_busy(frame, "SCANNING")
            return
        if self.error:
            frame.text(1, 0, T.fit("SCAN FAILED", frame.cols))
            frame.text(2, 0, T.fit(self.error, frame.cols))
            return
        if not self.models:
            frame.text(1, 0, T.fit("NO .gguf FILES IN", frame.cols))
            frame.text(2, 0, T.fit(self.app.config.models_dir[-20:], frame.cols))
            frame.text(3, 0, T.fit("F5 RESCANS", frame.cols))
            return

        current = str(self.app.config.get("engine.model", ""))

        def render(index: int, width: int):
            model = self.models[index]
            marker = self.g("check") if model.filename == current else ""
            return model.stem, f"{marker}{model.size_gib:.1f}G"

        self.list.draw(self.display, frame, render)


class WifiScreen(Screen, _BusyMixin):
    """Join a wireless network."""

    bank = G.BANK_MENU
    title = "WI-FI"

    def __init__(self, app):
        super().__init__(app)
        self.list = ListView(rows=3)
        self.points: List[net_service.AccessPoint] = []
        self.activity = Activity(fps=8.0)
        self.scan_task: Optional[AsyncTask] = None
        self.connect_task: Optional[AsyncTask] = None
        self.status = ""

    def on_enter(self) -> None:
        if not net_service.available():
            self.status = "NETWORKMANAGER ABSENT"
            return
        self._rescan()

    def _rescan(self) -> None:
        self.activity.reset()
        self.status = ""
        self.scan_task = AsyncTask(net_service.scan, name="wifi-scan")

    def update(self, dt: float) -> None:
        if self.scan_task is not None and self.scan_task.finished:
            self.points = self.scan_task.result or []
            self.list.set_count(len(self.points))
            if self.scan_task.error:
                self.status = "SCAN FAILED"
            self.scan_task = None
        if self.connect_task is not None and self.connect_task.finished:
            result = self.connect_task.result
            if result is not None and getattr(result, "ok", False):
                self.app.notify("CONNECTED", 3.0)
                self.status = ""
                self._rescan()
            else:
                detail = getattr(result, "err", "") or "FAILED"
                self.status = T.ellipsis(detail.upper(), 20)
                self.app.notify("CONNECT FAILED", 3.0)
            self.connect_task = None

    def on_key(self, event: KeyEvent) -> bool:
        if self.connect_task is not None:
            if event.key == K.ESC:
                self.app.notify("STILL CONNECTING")
            return True
        if event.key == K.UP:
            self.list.move(-1)
            return True
        if event.key == K.DOWN:
            self.list.move(1)
            return True
        if event.key == K.F5:
            self._rescan()
            return True
        if event.key == K.ENTER:
            self._join()
            return True
        if event.key == K.DELETE:
            self._forget()
            return True
        if event.key in (K.ESC, K.LEFT):
            self.close(None)
            return True
        return False

    def _join(self) -> None:
        if not self.points:
            return
        point = self.points[self.list.index]
        if point.active:
            self.app.notify("ALREADY JOINED")
            return

        def _connect(password: Optional[str]) -> None:
            self.activity.reset()
            self.status = f"JOINING {point.ssid[:12]}"
            self.connect_task = AsyncTask(
                lambda: net_service.connect(point.ssid, password),
                name="wifi-connect")

        if point.secured and point.ssid not in net_service.known_connections():
            self.app.ask_text(f"KEY {point.ssid[:12]}",
                              lambda value: _connect(value or None),
                              secret=True, hint="NETWORK PASSWORD")
        else:
            _connect(None)

    def _forget(self) -> None:
        if not self.points:
            return
        point = self.points[self.list.index]
        if point.ssid not in net_service.known_connections():
            self.app.notify("NOT SAVED")
            return
        self.app.confirm(f"FORGET {point.ssid[:10]}?",
                         lambda: self._do_forget(point.ssid), danger=True)

    def _do_forget(self, ssid: str) -> None:
        result = net_service.forget(ssid)
        self.app.notify("FORGOTTEN" if result.ok else "FAILED")
        self._rescan()

    def draw(self, frame: Frame) -> None:
        counter = (f"{self.list.index + 1}/{len(self.points)}"
                   if self.points else "")
        draw_title(frame, "WI-FI", counter)

        if self.connect_task is not None:
            self.draw_busy(frame, "JOINING", row=2)
            frame.row_text(3, T.fit(self.status, frame.cols, align="center"))
            return
        if self.scan_task is not None:
            self.draw_busy(frame, "SCANNING", row=2)
            return
        if self.status and not self.points:
            frame.text(1, 0, T.fit(self.status, frame.cols))
            frame.text(3, 0, T.fit("F5 RESCANS", frame.cols))
            return
        if not self.points:
            frame.text(2, 0, T.fit("NO NETWORKS", frame.cols, align="center"))
            frame.text(3, 0, T.fit("F5 RESCANS", frame.cols))
            return

        def render(index: int, width: int):
            point = self.points[index]
            marker = self.g("check") if point.active else ""
            lock = "*" if point.secured else " "
            return point.ssid, f"{marker}{lock}{point.signal:>3}"

        self.list.draw(self.display, frame, render)


class BluetoothScreen(Screen, _BusyMixin):
    """Discover, pair and connect a Bluetooth keyboard.

    Pairing an HID keyboard is a two-party exchange: the adapter shows a
    passkey and the keyboard being paired has to type it.  That passkey is
    parsed out of the pairing agent's output and shown here, because without it
    on screen there is no way for the operator to know what to type -- which is
    exactly the situation this whole flow exists to get out of.
    """

    bank = G.BANK_DEVICES
    title = "BLUETOOTH"

    IDLE, SCANNING, PAIRING, CONNECTING = "IDLE", "SCANNING", "PAIRING", "CONNECTING"
    SCAN_SECONDS = 12.0

    def __init__(self, app, standalone: bool = False):
        super().__init__(app)
        self.list = ListView(rows=3)
        self.activity = Activity(fps=8.0)
        self.state = self.IDLE
        self.devices: List[bt_service.BTDevice] = []
        self.target: Optional[bt_service.BTDevice] = None
        self.message = ""
        self.standalone = standalone
        self._started = 0.0
        self._refresh_task: Optional[AsyncTask] = None
        self._last_refresh = 0.0

    # -- session ------------------------------------------------------------

    @property
    def session(self) -> Optional[bt_service.BluetoothSession]:
        return self.app.bluetooth

    def on_enter(self) -> None:
        if not bt_service.available():
            self.message = "BLUETOOTHCTL ABSENT"
            return
        if self.app.bluetooth is None:
            self.app.bluetooth = bt_service.BluetoothSession()
        session = self.app.bluetooth
        if not session.alive and not session.start():
            self.message = session.error[:20] or "ADAPTER UNAVAILABLE"
            return
        self._refresh_task = AsyncTask(session.refresh_known, name="bt-known")
        self._begin_scan()

    def on_exit(self) -> None:
        session = self.session
        if session is not None and session.scanning:
            session.scan(False)

    def _begin_scan(self) -> None:
        session = self.session
        if session is None:
            return
        self.state = self.SCANNING
        self.activity.reset()
        self._started = time.monotonic()
        session.scan(True)

    def _stop_scan(self) -> None:
        session = self.session
        if session is not None:
            session.scan(False)
        if self.state == self.SCANNING:
            self.state = self.IDLE

    # -- update -------------------------------------------------------------

    def update(self, dt: float) -> None:
        session = self.session
        if session is None:
            return
        now = time.monotonic()
        if now - self._last_refresh > 0.4:
            self._last_refresh = now
            self.devices = session.snapshot()
            self.list.set_count(len(self.devices))

        if self.state == self.SCANNING and now - self._started > self.SCAN_SECONDS:
            self._stop_scan()

        state = session.state()
        if self.state in (self.PAIRING, self.CONNECTING):
            result = state.get("result", "")
            if result:
                lowered = result.lower()
                if "successful" in lowered:
                    self._on_paired()
                elif "fail" in lowered or "not available" in lowered:
                    self.message = T.ellipsis(result.upper(), 20)
                    self.state = self.IDLE
                    self.target = None
                session.last_result = ""

    def _on_paired(self) -> None:
        session = self.session
        if session is None or self.target is None:
            return
        if self.state == self.PAIRING:
            # Trust, so the keyboard reconnects by itself on the next boot
            # without anyone having to drive this screen again.
            session.trust(self.target.mac)
            session.connect(self.target.mac)
            self.state = self.CONNECTING
            self.message = "CONNECTING"
            self.activity.reset()
            return
        self.message = "KEYBOARD READY"
        self.app.notify("KEYBOARD PAIRED", 3.0)
        self.state = self.IDLE
        self.target = None
        if self.standalone:
            self.close(True)

    # -- input --------------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        session = self.session
        if session is None:
            if event.key == K.ESC and not self.standalone:
                self.close(None)
                return True
            return True

        state = session.state()
        if state.get("prompt") == "confirm":
            if event.key == K.ENTER:
                session.answer("yes")
                return True
            if event.key == K.ESC:
                session.answer("no")
                return True

        if event.key == K.UP:
            self.list.move(-1)
            return True
        if event.key == K.DOWN:
            self.list.move(1)
            return True
        if event.key == K.F5:
            self._begin_scan()
            return True
        if event.key == K.ENTER:
            self._activate()
            return True
        if event.key == K.DELETE:
            self._remove()
            return True
        if event.key in (K.ESC, K.LEFT):
            if self.state in (self.PAIRING, self.CONNECTING):
                self.state = self.IDLE
                self.target = None
                self.message = "CANCELLED"
                return True
            if self.standalone:
                self.close(False)
            else:
                self.close(None)
            return True
        return False

    def _activate(self) -> None:
        session = self.session
        if session is None or not self.devices:
            return
        device = self.devices[self.list.index]
        self.target = device
        self.message = ""
        self._stop_scan()
        session.passkey = ""
        if device.paired:
            self.state = self.CONNECTING
            self.activity.reset()
            session.connect(device.mac)
        else:
            self.state = self.PAIRING
            self.activity.reset()
            session.pair(device.mac)

    def _remove(self) -> None:
        session = self.session
        if session is None or not self.devices:
            return
        device = self.devices[self.list.index]
        if not device.paired:
            return
        self.app.confirm(f"REMOVE {device.label(10)}?",
                         lambda: session.remove(device.mac), danger=True)

    # -- drawing ------------------------------------------------------------

    def draw(self, frame: Frame) -> None:
        session = self.session
        if session is None:
            draw_title(frame, "BLUETOOTH")
            frame.text(2, 0, T.fit(self.message or "UNAVAILABLE", frame.cols))
            return

        state = session.state()
        passkey = state.get("passkey", "")

        if self.state == self.PAIRING and passkey:
            self._draw_passkey(frame, passkey, state.get("prompt", ""))
            return
        if self.state in (self.PAIRING, self.CONNECTING):
            draw_title(frame, self.state)
            name = self.target.label(20) if self.target else ""
            frame.text(1, 0, T.fit(name, frame.cols))
            self.draw_busy(frame, self.state, row=2)
            frame.row_text(3, T.fit("ESC CANCELS", frame.cols, align="center"))
            return

        counter = (f"{self.list.index + 1}/{len(self.devices)}"
                   if self.devices else "")
        draw_title(frame, "BLUETOOTH", counter)

        if self.state == self.SCANNING and not self.devices:
            self.draw_busy(frame, "SCANNING", row=2)
            return
        if not self.devices:
            frame.text(1, 0, T.fit(self.message or "NO DEVICES", frame.cols))
            frame.text(3, 0, T.fit("F5 RESCANS", frame.cols))
            return

        scanning = self.state == self.SCANNING

        def render(index: int, width: int):
            device = self.devices[index]
            marker = self.g("check") if device.connected else device.status_char()
            kind = self.g("bt") if device.is_keyboard else " "
            return device.name or device.mac, f"{kind}{marker}"

        self.list.draw(self.display, frame, render)
        if scanning:
            # Keep the spinner visible in the title while the list is usable.
            frame.text(0, 0, self.activity.spinner(self.display, "spin"))

    def _draw_passkey(self, frame: Frame, passkey: str, prompt: str) -> None:
        """The one screen where the content matters more than the chrome."""
        if prompt == "confirm":
            frame.text_center(0, "CONFIRM PASSKEY")
            frame.text_center(1, passkey)
            frame.text_center(2, "MATCHES DEVICE?")
            frame.text_center(3, "ENTER=YES ESC=NO")
            return
        frame.text_center(0, "TYPE ON KEYBOARD")
        frame.text_center(1, passkey)
        frame.text_center(2, "THEN PRESS ENTER")
        name = self.target.label(20) if self.target else ""
        frame.text_center(3, T.fit(name, frame.cols, align="center"))
