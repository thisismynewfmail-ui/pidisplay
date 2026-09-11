"""Base class for everything that can occupy the panel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent

if TYPE_CHECKING:                                    # pragma: no cover
    from .app import App


class Screen:
    """One full-panel view.

    Screens are stacked.  The one on top receives keys and draws; the ones
    below keep their state and are redrawn unchanged when uncovered, which is
    what lets the settings menu open over a reply that is still streaming and
    return to it exactly where it was.
    """

    #: CGRAM bank this screen needs.  Re-asserted every frame; reprogramming
    #: only happens when the bank actually changes.
    bank = G.BANK_MENU

    #: Title used by the default header and by the help overlay.
    title = ""

    #: When False, the app's global hotkeys are not consulted -- used by modal
    #: dialogs that must not be interrupted by, say, the settings key.
    allow_global_keys = True

    #: When True the app parks the hardware caret; screens with a text field
    #: set it themselves during draw.
    hide_caret = True

    def __init__(self, app: "App"):
        self.app = app

    # -- lifecycle ---------------------------------------------------------

    def on_enter(self) -> None:
        """Called when this screen becomes the top of the stack."""

    def on_exit(self) -> None:
        """Called when this screen is popped."""

    def on_reveal(self) -> None:
        """Called when a screen above this one is popped."""

    # -- interaction -------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        """Handle *event*.  Return True if it was consumed."""
        return False

    def update(self, dt: float) -> None:
        """Advance animation and poll background work."""

    def draw(self, frame: Frame) -> None:
        """Render into *frame*.  Called every frame; must not block."""

    # -- helpers -----------------------------------------------------------

    @property
    def display(self):
        return self.app.display

    def g(self, name: str) -> str:
        return self.app.display.g(name)

    def close(self, result: Optional[object] = None) -> None:
        self.app.pop(result)
