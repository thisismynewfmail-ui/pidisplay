"""
Background tasks for operations that are too slow to run on the render loop.

A Wi-Fi scan takes several seconds and a Bluetooth scan takes ten.  Doing
either inline would freeze the display mid-animation, which on a device with no
other status indicator looks exactly like a crash.  Screens therefore start an
:class:`AsyncTask`, keep drawing, and poll it each frame.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class AsyncTask:
    """A function running on its own thread, pollable without blocking."""

    def __init__(self, func: Callable[[], Any], name: str = "task"):
        self.name = name
        self.result: Any = None
        self.error: str = ""
        self.started = time.monotonic()
        self.finished = False
        self._thread = threading.Thread(target=self._run, args=(func,),
                                        daemon=True, name=name)
        self._thread.start()

    def _run(self, func: Callable[[], Any]) -> None:
        try:
            self.result = func()
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
        finally:
            self.finished = True

    @property
    def running(self) -> bool:
        return not self.finished

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def take(self) -> Optional[Any]:
        """Result if finished and successful, else None."""
        return self.result if self.finished and not self.error else None
