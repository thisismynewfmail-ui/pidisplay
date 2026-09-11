"""
Headless driver for the interface.

Builds the whole program against the HD44780 emulator and a scripted keyboard,
steps the render loop one frame at a time, and prints what the panel would
show.  Nothing is stubbed above the transport, so what this renders is what the
hardware renders.
"""

from __future__ import annotations

import os
import queue
import sys
import time
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aperture.config import Config
from aperture.hal.display import Display
from aperture.hal.keys import CHAR, KeyEvent
from aperture.hal.lcd import CharacterLCD
from aperture.hal.transport import EmulatedTransport
from aperture.simulator import ascii_frame
from aperture.ui.app import App


class ScriptedKeyboard:
    """Stands in for KeyboardHub, delivering whatever the test types."""

    def __init__(self) -> None:
        self.events: "queue.Queue[KeyEvent]" = queue.Queue()
        self.permission_error = False
        self.last_error = ""
        self._devices: List = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def press(self, key: str, char: str = "", ctrl: bool = False) -> None:
        self.events.put(KeyEvent(key=key, char=char, ctrl=ctrl, source="script"))

    def type(self, text: str) -> None:
        for ch in text:
            self.press(CHAR, ch)

    def drain(self, limit: int = 48) -> List[KeyEvent]:
        out = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    def flush(self) -> None:
        self.drain(1000)

    @property
    def devices(self):
        return self._devices

    @property
    def device_count(self) -> int:
        return 1

    @property
    def has_input(self) -> bool:
        return True

    def describe_sources(self) -> List[str]:
        return ["scripted harness"]


class Harness:
    """An app instance wired to the emulator, steppable frame by frame."""

    def __init__(self, config_path: str, models_dir: str, port: int = 8080,
                 state_dir: Optional[str] = None):
        config = Config.load(config_path)
        config.set("paths.models", models_dir)
        # Transcripts and logs must land beside the test's own files, not in
        # the real user state directory where separate runs would collide.
        config.set("paths.state",
                   state_dir or os.path.join(os.path.dirname(config_path), "state"))
        config.set("endpoint.port", port)
        config.set("display.fps", 20)
        self.config = config

        self.transport = EmulatedTransport(cols=20, rows=4)
        lcd = CharacterLCD(self.transport, cols=20, rows=4)
        lcd.initialise()
        self.display = Display(lcd)
        self.keyboard = ScriptedKeyboard()
        self.app = App(config, self.display, self.keyboard,
                       project_root=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
        self.app.running = True

    @property
    def emulator(self):
        return self.transport.emulator

    def push_chat(self) -> None:
        from aperture.ui.chat import ChatScreen
        self.app.push(ChatScreen(self.app))

    def push(self, screen) -> None:
        self.app.push(screen)

    def step(self, frames: int = 1, dt: float = 0.05,
             sleep: float = 0.0) -> None:
        for _ in range(frames):
            self.app.tick(dt)
            if sleep:
                time.sleep(sleep)

    def run_for(self, seconds: float, dt: float = 0.05) -> None:
        """Step in real time, so background threads actually make progress."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.tick(dt)
            time.sleep(dt)

    def screen(self) -> List[str]:
        return self.emulator.readable_screen()

    def show(self, label: str = "") -> str:
        out = ascii_frame(self.emulator)
        if label:
            out = f"--- {label} ---\n" + out
        print(out)
        return out

    def close(self) -> None:
        self.app.running = False
        try:
            self.app.engine.abort()
            self.app.engine.wait(1.0)
        except Exception:
            pass
        if self.app.server is not None:
            self.app.server.stop()
