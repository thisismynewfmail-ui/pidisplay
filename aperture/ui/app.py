"""
The application shell: screen stack, global keys, and the render loop.

The loop is single-threaded and deliberately so.  Keyboard events arrive on a
queue from the input thread, generation deltas arrive on a queue from the
inference thread, and system probes arrive through :class:`AsyncTask` results.
The loop drains all three, updates state, draws one frame, and sleeps.  Nothing
in the drawing path can block, which is why the display keeps animating while a
model is loading, a Bluetooth scan is running, or a reply is streaming.

Frame pacing is a ceiling, not a floor.  A frame whose content is unchanged
costs one buffer comparison and no bus traffic at all, so idling at twenty
frames a second is close to free; when something *is* moving, the differential
renderer keeps the cost proportional to how much of the panel actually changed.
"""

from __future__ import annotations

import os
import signal
import time
from typing import Any, Callable, List, Optional

from .. import persona
from ..config import Config
from ..hal import glyphs as G
from ..hal.display import Display
from ..hal.keyboard import KeyboardHub
from ..hal.keys import KeyEvent
from ..hal import keys as K
from ..llm.client import LlamaClient
from ..llm.server import LlamaServer, find_binary
from ..llm.session import ChatEngine, Conversation
from ..services import bt as bt_service
from .screen import Screen
from .widgets import Toast
from . import text as T


class App:
    """Owns the hardware, the engine and the screen stack."""

    def __init__(self, config: Config, display: Display, keyboard: KeyboardHub,
                 project_root: str = ""):
        self.config = config
        self.display = display
        self.keyboard = keyboard
        self.project_root = project_root or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))

        self.stack: List[Screen] = []
        self.toast = Toast()
        self.running = False
        self.exit_code = 0
        self._pending_result: Any = None
        self._result_handlers: List[Optional[Callable[[Any], None]]] = []

        # Engine plumbing.  The client is created up front so screens can probe
        # a server that is already running before we decide to start our own.
        self.client = LlamaClient(config.base_url,
                                  api_key=str(config.get("endpoint.api_key", "")),
                                  timeout=float(config.get("endpoint.timeout", 600)))
        self.conversation = Conversation(self._system_prompt())
        self.engine = ChatEngine(self.client, self.conversation)
        self.server: Optional[LlamaServer] = None
        self.engine_ready = False
        self.engine_status = "NOT STARTED"
        self.model_info = None
        self.bluetooth: Optional[bt_service.BluetoothSession] = None

        self._last_key_at = time.monotonic()
        self._backlight_off = False
        self._frame_budget = 1.0 / max(5, int(config.get("display.fps", 20)))
        self._stream_budget = 1.0 / max(2, int(config.get("chat.stream_rate", 12)))
        self.frame_count = 0
        self.started_at = time.monotonic()
        #: Called after every present. The simulator uses it to mirror the
        #: panel into a terminal; on real hardware it stays None.
        self.frame_hook: Optional[Callable[[], None]] = None

        self.apply_engine_config()

    # -- configuration ------------------------------------------------------

    def _system_prompt(self) -> str:
        return persona.system_prompt(
            str(self.config.get("chat.persona", "terminal")),
            str(self.config.get("chat.custom_prompt", "")))

    def apply_engine_config(self) -> None:
        """Push current settings into the client and engine."""
        config = self.config
        self.client.base_url = config.base_url
        self.client.api_key = str(config.get("endpoint.api_key", ""))
        self.client.timeout = float(config.get("endpoint.timeout", 600))
        self.conversation.system_prompt = self._system_prompt()

        params = {
            "n_predict": int(config.get("sampling.max_tokens", 512)),
            "temperature": float(config.get("sampling.temperature", 0.7)),
            "top_p": float(config.get("sampling.top_p", 0.95)),
            "top_k": int(config.get("sampling.top_k", 40)),
            "repeat_penalty": float(config.get("sampling.repeat_penalty", 1.1)),
        }
        seed = int(config.get("sampling.seed", -1))
        if seed >= 0:
            params["seed"] = seed
        self.engine.configure(window=int(config.get("engine.context", 4096)),
                              reserve=int(config.get("chat.reserve", 640)),
                              params=params,
                              prefill=bool(config.get("engine.prefill", True)))
        self._frame_budget = 1.0 / max(5, int(config.get("display.fps", 20)))
        self._stream_budget = 1.0 / max(2, int(config.get("chat.stream_rate", 12)))

    def frame_budget(self) -> float:
        """Seconds per frame, which is lower while a reply is arriving.

        Streaming is the one time the render loop competes for the CPU with
        something that matters more. Redrawing the transcript faster than the
        eye resolves buys nothing and takes cycles from the model, so the
        stream rate caps it separately from the idle refresh.
        """
        return self._stream_budget if self.engine.busy else self._frame_budget

    @property
    def state_dir(self) -> str:
        return os.path.expanduser(str(self.config.get("paths.state", "")))

    # -- screen stack -------------------------------------------------------

    def push(self, screen: Screen,
             on_result: Optional[Callable[[Any], None]] = None) -> None:
        self.stack.append(screen)
        self._result_handlers.append(on_result)
        # A new screen almost always changes the whole panel; skipping the
        # differential path avoids a frame of mixed old and new content.
        self.display.invalidate()
        screen.on_enter()

    def pop(self, result: Any = None) -> None:
        if len(self.stack) <= 1:
            return
        screen = self.stack.pop()
        handler = self._result_handlers.pop()
        screen.on_exit()
        self.display.invalidate()
        if handler is not None:
            handler(result)
        if self.stack:
            self.stack[-1].on_reveal()

    def replace(self, screen: Screen) -> None:
        if self.stack:
            old = self.stack.pop()
            self._result_handlers.pop()
            old.on_exit()
        self.push(screen)

    def reset_to(self, screen: Screen) -> None:
        while len(self.stack) > 1:
            self.pop()
        self.replace(screen)

    @property
    def top(self) -> Optional[Screen]:
        return self.stack[-1] if self.stack else None

    def find_screen(self, cls) -> Optional[Screen]:
        for screen in self.stack:
            if isinstance(screen, cls):
                return screen
        return None

    # -- notifications ------------------------------------------------------

    def notify(self, message: str, seconds: float = 2.0) -> None:
        self.toast.show(message, seconds)

    def confirm(self, question: str, on_yes: Callable[[], None],
                detail: str = "", danger: bool = False) -> None:
        from .dialogs import ConfirmScreen

        def _handle(result: Any) -> None:
            if result:
                on_yes()

        self.push(ConfirmScreen(self, question, detail=detail, danger=danger),
                  on_result=_handle)

    def ask_text(self, title: str, on_done: Callable[[str], None],
                 initial: str = "", secret: bool = False,
                 hint: str = "") -> None:
        from .dialogs import TextEntryScreen

        def _handle(result: Any) -> None:
            if result is not None:
                on_done(str(result))

        self.push(TextEntryScreen(self, title, initial=initial, secret=secret,
                                  hint=hint), on_result=_handle)

    # -- engine -------------------------------------------------------------

    def stop_engine(self) -> None:
        self.engine.abort()
        self.engine.wait(timeout=2.0)
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.engine_ready = False

    def restart_engine(self, reason: str = "") -> None:
        """Tear down and relaunch the engine, showing the boot stage again."""
        from .boot import EngineStage

        self.stop_engine()
        self.engine_ready = False
        self.engine_status = "RESTARTING"
        if reason:
            self.notify(reason)
        self.push(EngineStage(self, standalone=True))

    def make_server(self) -> Optional[LlamaServer]:
        binary = find_binary(str(self.config.get("paths.llama_server", "")),
                             self.project_root)
        if not binary:
            return None
        self.server = LlamaServer(binary)
        return self.server

    # -- global keys --------------------------------------------------------

    def _handle_global(self, event: KeyEvent) -> bool:
        from .dialogs import DiagnosticsScreen, HelpScreen
        from .settings import SettingsScreen
        from .chat import ChatScreen
        from .pickers import ModelPickerScreen

        # F7 is the documented settings key. F4 is accepted as well because
        # some compact keyboards put F4 where a full-size layout puts F7, and
        # a settings menu nobody can reach is worse than a duplicated binding.
        if event.key in (K.F7, K.F4):
            # F7 toggles. Pressing it from inside a settings section unwinds
            # and closes rather than stacking a second copy of the menu on top
            # of the first, which is what "open settings" would otherwise do.
            existing = self.find_screen(SettingsScreen)
            if existing is None:
                self.push(SettingsScreen(self))
            else:
                while self.top is not existing and len(self.stack) > 1:
                    self.pop()
                if isinstance(self.top, SettingsScreen):
                    self.top.dismiss()
            return True
        if event.key == K.F1:
            if not isinstance(self.top, HelpScreen):
                self.push(HelpScreen(self))
            return True
        if event.key == K.F6:
            if not isinstance(self.top, DiagnosticsScreen):
                self.push(DiagnosticsScreen(self))
            return True
        if event.key == K.F3:
            if not isinstance(self.top, ModelPickerScreen):
                self.push(ModelPickerScreen(self))
            return True
        if event.key == K.F12:
            self.set_backlight(not self.display.backlight, manual=True)
            self.notify("BACKLIGHT " + ("ON" if self.display.backlight else "OFF"))
            return True
        if event.key == K.F10 or (event.ctrl and event.char == "q"):
            self.confirm("SHUT DOWN TERMINAL?", self.quit)
            return True
        if event.ctrl and event.char == "c":
            self.quit()
            return True
        return False

    # -- backlight ----------------------------------------------------------

    def set_backlight(self, on: bool, manual: bool = False) -> None:
        self.display.backlight = on
        self._backlight_off = not on
        if manual:
            self.config.set("display.backlight", on)

    def _update_backlight(self, now: float) -> None:
        timeout = int(self.config.get("display.dim_after", 0))
        if not bool(self.config.get("display.backlight", True)):
            return
        if timeout <= 0:
            if self._backlight_off:
                self.set_backlight(True)
            return
        idle = now - self._last_key_at
        # Never blank the panel while a reply is arriving: the whole point of
        # the display at that moment is that something is being shown on it.
        if self.engine.busy:
            idle = 0.0
            self._last_key_at = now
        if idle > timeout and not self._backlight_off:
            self.set_backlight(False)
        elif idle <= timeout and self._backlight_off:
            self.set_backlight(True)

    # -- main loop ----------------------------------------------------------

    def quit(self) -> None:
        self.running = False

    def run(self) -> int:
        self.running = True
        self._install_signal_handlers()
        last = time.monotonic()

        while self.running:
            now = time.monotonic()
            dt = now - last
            last = now
            self.tick(dt, now)
            budget = self.frame_budget()
            spent = time.monotonic() - now
            if spent < budget:
                time.sleep(budget - spent)

        self._shutdown()
        return self.exit_code

    def tick(self, dt: float, now: Optional[float] = None) -> None:
        """One iteration: drain inputs, update the top screen, draw one frame.

        Factored out of :meth:`run` so the test harness can step the whole
        interface deterministically, one frame at a time, without a real clock
        or a real panel.
        """
        now = time.monotonic() if now is None else now

        self._pump_keys()
        if not self.running:
            return
        self._pump_engine()

        screen = self.top
        if screen is None:
            return
        screen.update(dt)

        frame = self.display.begin_frame()
        self.display.use_bank(screen.bank)
        screen.draw(frame)
        if self.toast.active:
            self.toast.draw(frame)
        if screen.hide_caret:
            self.display.set_caret(None)
        self.display.present()
        self.frame_count += 1
        if self.frame_hook is not None:
            self.frame_hook()

        self._update_backlight(now)

    #: Keys whose auto-repeat the "Key repeat" setting governs. Typing repeat
    #: is always allowed -- holding backspace is how people delete a word.
    _REPEATABLE_NAV = (K.UP, K.DOWN, K.LEFT, K.RIGHT, K.PGUP, K.PGDN)

    def _pump_keys(self) -> None:
        repeat_nav = bool(self.config.get("input.repeat_nav", True))
        for event in self.keyboard.drain(limit=48):
            if event.repeat and not repeat_nav and event.key in self._REPEATABLE_NAV:
                continue
            self._last_key_at = time.monotonic()
            if self._backlight_off:
                # The keystroke that wakes the panel should not also act: the
                # operator cannot see what they are pressing until it is lit.
                self.set_backlight(True)
                continue
            if self.toast.active and not self.toast.sticky:
                self.toast.clear()
            screen = self.top
            if screen is None:
                return
            if screen.on_key(event):
                continue
            if screen.allow_global_keys and self._handle_global(event):
                continue

    def _pump_engine(self) -> None:
        """Route inference events to the chat screen wherever it sits.

        Events are drained every frame regardless of which screen is on top, so
        opening the settings menu mid-reply neither stalls generation nor loses
        the tokens that arrive while it is open.
        """
        from .chat import ChatScreen

        events = self.engine.drain()
        if not events:
            return
        chat = self.find_screen(ChatScreen)
        if chat is not None:
            chat.consume(events)

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.running = False

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass

    def _shutdown(self) -> None:
        from .dialogs import farewell_frame

        try:
            self.engine.abort()
            self.engine.wait(timeout=2.0)
        except Exception:
            pass
        try:
            farewell_frame(self)
            if self.frame_hook is not None:
                self.frame_hook()
        except Exception:
            pass
        if self.config.dirty:
            self.config.save()
        if self.bluetooth is not None:
            try:
                self.bluetooth.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.stop()
