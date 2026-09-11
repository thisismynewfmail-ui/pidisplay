"""
The startup sequence.

The order of these stages is the most considered decision in the program, and
it is dictated by dependency rather than by appearance:

  1. PANEL   -- prove the display before trusting anything it says. If the
                panel is mis-addressed, every later error message is invisible,
                so this stage exercises CGRAM, both halves of the DDRAM map and
                the backlight before anything else is allowed to report status.

  2. INPUT   -- a keyboard must exist before any screen that needs a decision.
                This is the stage that cannot be skipped or deferred: with no
                keyboard there is no way to answer a prompt, choose a model, or
                even acknowledge an error. So it runs unattended, waits
                indefinitely, and can pair a Bluetooth keyboard *with no
                existing input device* -- the adapter displays a passkey, and
                the keyboard being paired is the thing that types it. That is
                the one bootstrap path out of an input-less state, and it is
                why this stage comes before the engine rather than after.

  3. NETWORK -- informational for a local engine, required for a remote one.
                Never blocks: a terminal with no network is a working terminal.

  4. ENGINE  -- the slowest stage by far, so it runs last, once the operator
                can already interact. If a server is already listening it is
                adopted rather than duplicated.

Every stage animates while it waits, because a four-line panel that has stopped
updating is indistinguishable from one that has crashed.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from .. import BUILD_NAME, VERSION
from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent
from ..hal import keys as K
from ..llm import models as model_scan
from ..llm.server import find_binary, port_in_use
from ..services import bt as bt_service
from ..services import net as net_service
from ..services.tasks import AsyncTask
from .screen import Screen
from .widgets import Activity
from . import text as T


class _Stage(Screen):
    """Common furniture for boot stages."""

    bank = G.BANK_SYSTEM
    step = 0
    steps = 4

    def __init__(self, app):
        super().__init__(app)
        self.activity = Activity(fps=8.0)
        self.detail = ""

    def advance(self, nxt: Optional[Screen]) -> None:
        if nxt is None:
            self.app.pop(None)
        else:
            self.app.replace(nxt)

    def draw_header(self, frame: Frame, label: str) -> None:
        frame.text(0, 0, self.activity.iris(self.display, "state"))
        frame.text(0, 2, T.fit(label, 12))
        frame.text_right(0, f"{self.step}/{self.steps}")

    def draw_progress(self, frame: Frame, row: int, fraction: float) -> None:
        bar = T.progress_bar(fraction, frame.cols, G.ROM_FULL_BLOCK,
                             self.g("half"))
        frame.text(row, 0, bar)


class SplashStage(_Stage):
    """Panel self-test.  Proves the glyph path and both DDRAM halves."""

    step = 1
    DURATION = 2.6

    def __init__(self, app):
        super().__init__(app)
        self._phase = 0

    def update(self, dt: float) -> None:
        if self.activity.elapsed >= self.DURATION:
            self.advance(InputStage(self.app))

    def on_key(self, event: KeyEvent) -> bool:
        # Any key skips the splash; nobody needs to watch it twice.
        self.advance(InputStage(self.app))
        return True

    def draw(self, frame: Frame) -> None:
        elapsed = self.activity.elapsed
        frame.text_center(0, BUILD_NAME)

        if elapsed < 1.1:
            # Parade every custom glyph plus the ROM block, which is the
            # fastest way to see a CGRAM or bus fault with your own eyes.
            self.display.set_glyph("spin", G.spinner(self.activity.phase))
            self.display.set_glyph("state", G.iris(self.activity.phase))
            row = "".join([
                self.g("state"), self.g("check"), self.g("cross"),
                self.g("warn"), self.g("wifi"), self.g("bt"),
                self.g("spin"), self.g("half"), G.ROM_FULL_BLOCK,
            ])
            frame.text_center(1, row)
            frame.text_center(2, "PANEL SELF TEST")
        else:
            frame.text_center(1, f"VERSION {VERSION}")
            frame.text_center(2, self._panel_line())

        fraction = min(1.0, elapsed / self.DURATION)
        self.draw_progress(frame, 3, fraction)

    def _panel_line(self) -> str:
        transport = self.display.lcd.transport
        address = getattr(transport, "address", None)
        if address is None:
            return "SIMULATED PANEL"
        bus = getattr(transport, "bus_number", "?")
        return f"I2C {bus} @ 0x{address:02X}"


class InputStage(_Stage):
    """Wait for a keyboard, and try hard to produce one.

    Sequence: adopt anything already attached; reconnect trusted Bluetooth
    devices; and failing that, discover and pair a Bluetooth keyboard with no
    operator input beyond typing the passkey on the keyboard itself.
    """

    step = 2
    #: How long to accept an already-present keyboard before doing any work.
    SETTLE = 0.8
    #: How long to let trusted-device reconnection run before scanning.
    RECONNECT_TIMEOUT = 14.0
    #: Discovery window when hunting for an unpaired keyboard.
    DISCOVER_SECONDS = 16.0

    SETTLING, RECONNECTING, DISCOVERING, PAIRING, WAITING = (
        "SETTLING", "RECONNECTING", "DISCOVERING", "PAIRING", "WAITING")

    def __init__(self, app):
        super().__init__(app)
        self.state = self.SETTLING
        self.message = ""
        self.task: Optional[AsyncTask] = None
        self.target: Optional[bt_service.BTDevice] = None
        self._state_started = time.monotonic()
        self._bt_checked = False

    # -- helpers ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state
        self._state_started = time.monotonic()
        self.activity.reset()

    @property
    def state_elapsed(self) -> float:
        return time.monotonic() - self._state_started

    @property
    def session(self) -> Optional[bt_service.BluetoothSession]:
        return self.app.bluetooth

    def _proceed(self) -> None:
        sources = self.app.keyboard.describe_sources()
        if sources:
            self.app.notify(T.ellipsis(sources[0].upper(), 20), 2.0)
        session = self.session
        if session is not None and session.scanning:
            session.scan(False)
        self.advance(NetworkStage(self.app))

    # -- update -------------------------------------------------------------

    def update(self, dt: float) -> None:
        hub = self.app.keyboard

        # A keyboard appearing at any point ends this stage immediately,
        # whatever else is in flight.
        if hub.has_input and self.state != self.SETTLING:
            self._proceed()
            return

        if self.state == self.SETTLING:
            if self.state_elapsed >= self.SETTLE:
                if hub.has_input:
                    self._proceed()
                else:
                    self._start_reconnect()
            return

        if self.state == self.RECONNECTING:
            if self.task is not None and self.task.finished:
                names = self.task.result or []
                self.task = None
                if names:
                    self.message = T.ellipsis(str(names[0]).upper(), 20)
                    # Give the kernel a moment to enumerate the HID node.
                    self._set_state(self.SETTLING)
                    return
                self._start_discovery()
            elif self.state_elapsed > self.RECONNECT_TIMEOUT:
                self._start_discovery()
            return

        if self.state == self.DISCOVERING:
            self._update_discovery()
            return

        if self.state == self.PAIRING:
            self._update_pairing()
            return

        if self.state == self.WAITING:
            # Nothing more can be done automatically; keep rescanning for a
            # USB keyboard forever rather than failing.
            if self.state_elapsed > 25.0:
                self._start_reconnect()
            return

    def _start_reconnect(self) -> None:
        if not bt_service.available():
            self.message = "ATTACH A KEYBOARD"
            self._set_state(self.WAITING)
            return
        self._set_state(self.RECONNECTING)
        self.message = ""
        self.task = AsyncTask(
            lambda: bt_service.reconnect_trusted(self.RECONNECT_TIMEOUT),
            name="bt-reconnect")

    def _start_discovery(self) -> None:
        if self.app.bluetooth is None:
            self.app.bluetooth = bt_service.BluetoothSession()
        session = self.app.bluetooth
        if not session.alive and not session.start():
            self.message = "NO BT ADAPTER"
            self._set_state(self.WAITING)
            return
        self._set_state(self.DISCOVERING)
        self.message = ""
        session.scan(True)

    def _update_discovery(self) -> None:
        session = self.session
        if session is None:
            self._set_state(self.WAITING)
            return
        candidates = [d for d in session.keyboards() if not d.paired]
        if len(candidates) == 1 and self.state_elapsed > 4.0:
            self._begin_pairing(session, candidates[0])
            return
        if self.state_elapsed > self.DISCOVER_SECONDS:
            session.scan(False)
            if len(candidates) > 1:
                self.message = "SEVERAL FOUND"
            elif candidates:
                self._begin_pairing(session, candidates[0])
                return
            else:
                self.message = "ATTACH A KEYBOARD"
            self._set_state(self.WAITING)

    def _begin_pairing(self, session: bt_service.BluetoothSession,
                       device: bt_service.BTDevice) -> None:
        session.scan(False)
        self.target = device
        self._set_state(self.PAIRING)
        self.message = ""
        session.pair(device.mac)

    def _update_pairing(self) -> None:
        session = self.session
        if session is None or self.target is None:
            self._set_state(self.WAITING)
            return
        state = session.state()
        result = state.get("result", "")
        if result:
            lowered = result.lower()
            session.last_result = ""
            if "successful" in lowered:
                session.trust(self.target.mac)
                session.connect(self.target.mac)
                self.message = "PAIRED"
                self._set_state(self.SETTLING)
                return
            if "fail" in lowered or "not available" in lowered:
                self.message = "PAIRING FAILED"
                self._set_state(self.WAITING)
                return
        if self.state_elapsed > 90.0:
            self.message = "PAIRING TIMED OUT"
            self._set_state(self.WAITING)

    # -- input --------------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        # Any keystroke at all proves the point of this stage.
        self._proceed()
        return True

    # -- drawing ------------------------------------------------------------

    def draw(self, frame: Frame) -> None:
        session = self.session
        if self.state == self.PAIRING and session is not None:
            passkey = session.state().get("passkey", "")
            if passkey:
                frame.text_center(0, "TYPE ON KEYBOARD")
                frame.text_center(1, passkey)
                frame.text_center(2, "THEN PRESS ENTER")
                name = self.target.label(20) if self.target else ""
                frame.text_center(3, name)
                return

        self.draw_header(frame, "INPUT")
        spinner = self.activity.spinner(self.display, "spin")

        if self.state == self.SETTLING:
            frame.text(1, 0, T.fit("ENUMERATING HID", frame.cols))
            frame.text(2, 0, T.fit(self.message or "SCANNING BUSES", frame.cols))
        elif self.state == self.RECONNECTING:
            frame.text(1, 0, T.fit(f"{self.g('bt')} RECONNECTING", frame.cols))
            frame.text(2, 0, T.fit("KNOWN KEYBOARDS", frame.cols))
        elif self.state == self.DISCOVERING:
            found = len(session.keyboards()) if session else 0
            frame.text(1, 0, T.fit(f"{self.g('bt')} DISCOVERY", frame.cols))
            frame.text(2, 0, T.fit(f"{found} KEYBOARD(S) SEEN", frame.cols))
        elif self.state == self.PAIRING:
            frame.text(1, 0, T.fit("PAIRING", frame.cols))
            frame.text(2, 0, T.fit(self.target.label(20) if self.target else "",
                                   frame.cols))
        else:
            frame.text(1, 0, T.fit(self.g("warn") + " NO KEYBOARD", frame.cols))
            frame.text(2, 0, T.fit(self.message or "PLUG ONE IN", frame.cols))

        frame.row_text(3, T.fit(f"{spinner} {self._hint()}", frame.cols))

    def _hint(self) -> str:
        if self.state == self.WAITING:
            return "USB OR BT, ANY KEY"
        return f"{self.state_elapsed:.0f}s  ANY KEY SKIPS"


class NetworkStage(_Stage):
    """Report the network, without ever waiting on it."""

    step = 3
    TIMEOUT = 4.0

    def __init__(self, app):
        super().__init__(app)
        self.task: Optional[AsyncTask] = AsyncTask(net_service.status,
                                                   name="net-status")
        self.status: Optional[net_service.NetStatus] = None
        self._done_at = 0.0

    def update(self, dt: float) -> None:
        if self.task is not None and self.task.finished:
            self.status = self.task.result
            self.task = None
            self._done_at = time.monotonic()
        if self.task is None and self.status is not None:
            # Linger only long enough to be read, then move on.
            if time.monotonic() - self._done_at > 1.2:
                self.advance(EngineStage(self.app))
        elif self.activity.elapsed > self.TIMEOUT:
            self.advance(EngineStage(self.app))

    def on_key(self, event: KeyEvent) -> bool:
        self.advance(EngineStage(self.app))
        return True

    def draw(self, frame: Frame) -> None:
        self.draw_header(frame, "NETWORK")
        if self.status is None:
            spinner = self.activity.spinner(self.display, "spin")
            frame.text(2, 0, T.fit(f"{spinner} CHECKING LINK", frame.cols))
            return
        icon = self.g("wifi") if self.status.connected else self.g("cross")
        frame.text(1, 0, T.fit(f"{icon} {self.status.summary()}", frame.cols))
        frame.text(2, 0, T.fit(self.status.ip or "NO ADDRESS", frame.cols))
        frame.text(3, 0, T.fit(self.status.hostname, frame.cols))


class EngineStage(_Stage):
    """Find, launch and wait for the inference server."""

    step = 4
    #: Mapping a multi-gigabyte model on a Pi genuinely can take minutes.
    STARTUP_TIMEOUT = 420.0

    CHECKING, ADOPTING, LAUNCHING, WAITING, PROBING, FAILED, DONE = (
        "CHECKING", "ADOPTING", "LAUNCHING", "WAITING", "PROBING",
        "FAILED", "DONE")

    def __init__(self, app, standalone: bool = False):
        super().__init__(app)
        self.standalone = standalone
        self.state = self.CHECKING
        self.error = ""
        self.hint = ""
        self.task: Optional[AsyncTask] = None
        self.model: Optional[model_scan.ModelInfo] = None
        self._started = time.monotonic()
        self._probe_started = 0.0

    # -- flow ---------------------------------------------------------------

    def on_enter(self) -> None:
        self.app.engine_ready = False
        self.task = AsyncTask(self._probe_existing, name="engine-probe")

    def _probe_existing(self) -> bool:
        host = str(self.app.config.get("endpoint.host"))
        port = int(self.app.config.get("endpoint.port"))
        if not port_in_use(host, port):
            return False
        return self.app.client.health(timeout=3.0)

    def update(self, dt: float) -> None:
        if self.state == self.CHECKING:
            if self.task is not None and self.task.finished:
                adopted = bool(self.task.result)
                self.task = None
                if adopted:
                    # Someone already has a server on this port. Using it is
                    # both faster and the only way to avoid a port conflict.
                    self.state = self.ADOPTING
                    self._finish_ready(adopted=True)
                elif str(self.app.config.get("engine.mode")) == "remote":
                    self._fail("NO REMOTE ENGINE",
                               "Check host and port in settings.")
                else:
                    self._launch()
            return

        if self.state == self.WAITING:
            self._await_health()
            return

    def _launch(self) -> None:
        config = self.app.config
        binary = find_binary(str(config.get("paths.llama_server", "")),
                             self.app.project_root)
        if not binary:
            self._fail("NO llama-server", "Run install.sh, or set the path in "
                                          "settings.")
            return

        models_dir = config.models_dir
        model = model_scan.resolve_model(models_dir,
                                         str(config.get("engine.model", "")))
        if model is None:
            self._fail("NO MODEL FOUND",
                       f"Put a .gguf file in {models_dir} and press ENTER.")
            return
        self.model = model
        self.app.model_info = model

        # Refuse to start with a window the weights were not trained for: it
        # runs, and it quietly produces worse output, which is the hardest kind
        # of fault to attribute.
        requested = int(config.get("engine.context"))
        if model.train_context and requested > model.train_context:
            config.set("engine.context", model.train_context)
            self.app.apply_engine_config()
            self.app.notify(f"CTX -> {T.format_count(model.train_context)}", 3.0)

        server = self.app.make_server()
        if server is None:
            self._fail("NO llama-server", "Binary vanished between checks.")
            return
        server.binary = binary

        self.state = self.LAUNCHING
        self.activity.reset()

        def _start() -> bool:
            plan = server.build_plan(
                model_path=model.path,
                host=str(config.get("endpoint.host")),
                port=int(config.get("endpoint.port")),
                n_ctx=int(config.get("engine.context")),
                threads=int(config.get("engine.threads")),
                gpu_layers=int(config.get("engine.gpu_layers")),
                batch=int(config.get("engine.batch")),
                cache_reuse=int(config.get("engine.cache_reuse")),
                mlock=bool(config.get("engine.mlock")),
                flash_attn=bool(config.get("engine.flash_attn")),
                api_key=str(config.get("endpoint.api_key", "")),
            )
            return server.start(plan)

        self.task = AsyncTask(_start, name="engine-start")
        self.state = self.WAITING
        self._started = time.monotonic()

    def _await_health(self) -> None:
        server = self.app.server
        if self.task is not None and self.task.finished:
            if not self.task.result:
                message = (server.last_error if server else "") or "LAUNCH FAILED"
                self._fail(message, "See the engine log on the diagnostics page.")
                return
            self.task = None

        if server is not None and not server.running and server.process is not None:
            self._fail(server.exit_summary() or "ENGINE STOPPED",
                       "See the engine log on the diagnostics page.")
            return

        if time.monotonic() - self._probe_started > 1.0:
            self._probe_started = time.monotonic()
            if self.app.client.health(timeout=1.5):
                self._finish_ready(adopted=False)
                return

        if time.monotonic() - self._started > self.STARTUP_TIMEOUT:
            self._fail("ENGINE TIMED OUT", "The model may be too large for "
                                           "available memory.")

    def _finish_ready(self, adopted: bool) -> None:
        self.state = self.PROBING
        client = self.app.client
        try:
            props = client.fetch_props()
        except Exception:
            props = None

        config = self.app.config
        if props is not None:
            if props.n_ctx and props.n_ctx != int(config.get("engine.context")):
                # The running server is the authority on its own window; the
                # gauge must reflect reality, not what we asked for.
                config.set("engine.context", props.n_ctx)
                self.app.apply_engine_config()
            if adopted and props.model_path:
                self.app.model_info = model_scan.inspect_model(props.model_path) \
                    if os.path.isfile(props.model_path) else None
                if self.app.model_info is None:
                    config.set("engine.model", os.path.basename(props.model_path))

        self.app.engine_ready = True
        self.app.engine_status = "ADOPTED" if adopted else "RUNNING"
        # Evaluate the system prompt now, so the operator's first message pays
        # only for the operator's own tokens.
        if bool(config.get("engine.prefill", True)):
            self.app.engine.schedule_prefill()
        self.state = self.DONE
        self.app.notify("ENGINE ADOPTED" if adopted else "ENGINE READY", 2.0)
        self.advance(None)

    def _fail(self, error: str, hint: str = "") -> None:
        self.state = self.FAILED
        self.error = error
        self.hint = hint
        self.app.engine_status = error
        self.activity.reset()

    # -- input --------------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        if self.state == self.FAILED:
            if event.key == K.ENTER:
                self.state = self.CHECKING
                self.error = ""
                self.app.stop_engine()
                self.task = AsyncTask(self._probe_existing, name="engine-probe")
                return True
            if event.key == K.ESC:
                # Continuing without an engine is allowed: the transcript, the
                # settings and the diagnostics are all still useful, and the
                # alternative is a terminal that refuses to boot.
                self.app.notify("CONTINUING OFFLINE", 3.0)
                self.advance(None)
                return True
            if event.key == K.F3:
                from .pickers import ModelPickerScreen
                self.app.push(ModelPickerScreen(self.app))
                return True
            if event.key in (K.F6, K.F7, K.F4, K.F1):
                return False           # let the global handler open those
            return True
        if event.key == K.ESC:
            self.app.notify("CONTINUING OFFLINE", 3.0)
            self.advance(None)
            return True
        return False

    # -- drawing ------------------------------------------------------------

    def draw(self, frame: Frame) -> None:
        if self.state == self.FAILED:
            self._draw_failure(frame)
            return

        self.draw_header(frame, "ENGINE")
        spinner = self.activity.spinner(self.display, "spin")

        if self.state == self.CHECKING:
            frame.text(1, 0, T.fit("PROBING ENDPOINT", frame.cols))
            frame.text(2, 0, T.fit(self.app.config.base_url.replace("http://", ""),
                                   frame.cols))
        elif self.state == self.ADOPTING:
            frame.text(1, 0, T.fit("ENGINE ALREADY UP", frame.cols))
            frame.text(2, 0, T.fit("ADOPTING SESSION", frame.cols))
        else:
            name = self.model.stem if self.model else "MODEL"
            frame.text(1, 0, T.fit(T.marquee(name, frame.cols,
                                             self.activity.elapsed), frame.cols))
            frame.text(2, 0, T.fit(self._progress_line(), frame.cols))

        elapsed = time.monotonic() - self._started
        frame.row_text(3, T.fit(f"{spinner} LOADING {elapsed:.0f}s", frame.cols))

    def _progress_line(self) -> str:
        """The engine's own most recent output, condensed.

        Model loading has no progress API, but llama.cpp narrates it, so the
        last line of its log is the most honest progress indicator available.
        """
        server = self.app.server
        if server is None:
            return "STARTING"
        for line in reversed(server.tail(12)):
            lowered = line.lower()
            if "load_tensors" in lowered or "llama_model_loader" in lowered:
                return T.ellipsis(line.split(":")[-1].strip().upper(), 20)
            if "n_ctx" in lowered:
                return T.ellipsis(line.strip().upper(), 20)
        return "MAPPING WEIGHTS"

    def _draw_failure(self, frame: Frame) -> None:
        frame.text(0, 0, self.g("warn"))
        frame.text(0, 2, T.fit("ENGINE FAULT", frame.cols - 2))
        frame.text(1, 0, T.fit(self.error, frame.cols))
        hint = T.marquee(self.hint, frame.cols, self.activity.elapsed) \
            if self.hint else ""
        frame.text(2, 0, T.fit(hint, frame.cols))
        frame.row_text(3, T.fit("ENTER RETRY F3 MODEL", frame.cols))
