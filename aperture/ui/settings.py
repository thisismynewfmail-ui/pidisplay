"""
The settings tree.

Structure is two levels deep and no more.  With three visible rows, a third
level costs more in navigation than it saves in grouping, and every setting
this program has fits into eight sections.

Editing happens in place.  Selecting a numeric setting puts the row into adjust
mode, where left and right step the value and the row shows the value flanked
by arrow glyphs so it is obvious which keys do something.  Nothing is committed
to disk until the operator leaves the settings screen, and settings that only
take effect on a fresh engine are collected and applied in one restart rather
than restarting on each keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from ..config import restart_required
from ..hal import glyphs as G
from ..hal.display import Frame
from ..hal.keys import KeyEvent
from ..hal import keys as K
from .screen import Screen
from .widgets import Activity, ListView, draw_title
from . import text as T

SETTING = "setting"
ACTION = "action"
SUBMENU = "submenu"


@dataclass
class Entry:
    """One row in a settings section."""

    kind: str
    label: str = ""
    key: str = ""
    action: Optional[Callable[["SectionScreen"], None]] = None
    value: Optional[Callable[["SectionScreen"], str]] = None
    section: str = ""
    help: str = ""

    def resolve_label(self, screen: "SectionScreen") -> str:
        if self.kind == SETTING:
            return screen.app.config.setting(self.key).label
        return self.label

    def resolve_value(self, screen: "SectionScreen") -> str:
        if self.kind == SETTING:
            return screen.app.config.format(self.key)
        if self.value is not None:
            return self.value(screen)
        if self.kind == SUBMENU:
            return G.ROM_RIGHT_ARROW
        return ""

    def resolve_help(self, screen: "SectionScreen") -> str:
        if self.kind == SETTING:
            return screen.app.config.setting(self.key).help
        return self.help


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def _pick_model(screen: "SectionScreen") -> None:
    from .pickers import ModelPickerScreen
    screen.app.push(ModelPickerScreen(screen.app))


def _wifi(screen: "SectionScreen") -> None:
    from .pickers import WifiScreen
    screen.app.push(WifiScreen(screen.app))


def _bluetooth(screen: "SectionScreen") -> None:
    from .pickers import BluetoothScreen
    screen.app.push(BluetoothScreen(screen.app))


def _restart_engine(screen: "SectionScreen") -> None:
    screen.app.confirm("RESTART ENGINE?",
                       lambda: screen.app.restart_engine("RESTARTING ENGINE"))


def _edit_custom_prompt(screen: "SectionScreen") -> None:
    app = screen.app

    def _save(value: str) -> None:
        app.config.set("chat.custom_prompt", value)
        app.config.set("chat.persona", "custom")
        app.conversation.system_prompt = app._system_prompt()
        app.notify("PERSONA UPDATED")

    app.ask_text("SYSTEM PROMPT", _save,
                 initial=str(app.config.get("chat.custom_prompt", "")),
                 hint="OVERRIDES THE PERSONA")


def _edit_text_setting(key: str, title: str, secret: bool = False):
    def _run(screen: "SectionScreen") -> None:
        app = screen.app

        def _save(value: str) -> None:
            app.config.set(key, value)
            screen.mark_restart(key)

        app.ask_text(title, _save, initial=str(app.config.get(key, "")),
                     secret=secret)
    return _run


def _save_now(screen: "SectionScreen") -> None:
    if screen.app.config.save():
        screen.app.notify("SETTINGS SAVED")
    else:
        screen.app.notify("SAVE FAILED")


def _reset_defaults(screen: "SectionScreen") -> None:
    app = screen.app

    def _do() -> None:
        app.config.reset_all()
        app.apply_engine_config()
        app.config.save()
        app.notify("DEFAULTS RESTORED")

    app.confirm("RESTORE DEFAULTS?", _do, detail="All settings revert.",
                danger=True)


def _show_about(screen: "SectionScreen") -> None:
    from .dialogs import MessageScreen
    from .. import BUILD_NAME, VERSION

    app = screen.app
    lines = [
        f"{BUILD_NAME} {VERSION}.",
        "",
        "A local chat terminal for a 20x4 HD44780 panel on a Raspberry Pi,",
        "running llama.cpp on the same machine.",
        "",
        f"Config: {app.config.path}",
        f"Models: {app.config.models_dir}",
        f"Engine: {app.config.base_url}",
    ]
    app.push(MessageScreen(app, "ABOUT", "\n".join(lines)))


def _power_off(screen: "SectionScreen") -> None:
    screen.app.confirm(
        "POWER OFF THE PI?",
        lambda: _run_power(screen, ["systemctl", "poweroff"]),
        detail="The whole machine halts.", danger=True)


def _reboot(screen: "SectionScreen") -> None:
    screen.app.confirm(
        "REBOOT THE PI?",
        lambda: _run_power(screen, ["systemctl", "reboot"]),
        detail="The whole machine restarts.", danger=True)


def _run_power(screen: "SectionScreen", command: Sequence[str]) -> None:
    from ..services.shell import run
    screen.app.notify("SHUTTING DOWN", 10.0)
    result = run(list(command), timeout=10.0)
    if not result.ok:
        # Almost always a missing privilege; say which, since "failed" alone
        # sends people looking in the wrong place.
        screen.app.notify("NEEDS ROOT", 4.0)


def _quit_program(screen: "SectionScreen") -> None:
    screen.app.confirm("EXIT TO SHELL?", screen.app.quit)


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------

SECTIONS: dict = {
    "INFERENCE": [
        Entry(SETTING, key="engine.context"),
        Entry(ACTION, label="Model", action=_pick_model,
              value=lambda s: (s.app.model_info.stem[-8:]
                               if s.app.model_info else "NONE"),
              help="Choose a GGUF file from the models directory."),
        Entry(SETTING, key="engine.mode"),
        Entry(SETTING, key="engine.threads"),
        Entry(SETTING, key="engine.batch"),
        Entry(SETTING, key="engine.gpu_layers"),
        Entry(SETTING, key="engine.cache_reuse"),
        Entry(SETTING, key="engine.flash_attn"),
        Entry(SETTING, key="engine.mlock"),
        Entry(SETTING, key="engine.prefill"),
        Entry(SETTING, key="engine.type_prefill"),
        Entry(ACTION, label="Restart eng", action=_restart_engine,
              help="Stop and relaunch llama-server with current settings."),
    ],
    "ENDPOINT": [
        Entry(ACTION, label="Host", action=_edit_text_setting(
            "endpoint.host", "SERVER HOST"),
              value=lambda s: str(s.app.config.get("endpoint.host")),
              help="Address of the llama.cpp server."),
        Entry(SETTING, key="endpoint.port"),
        Entry(ACTION, label="API key", action=_edit_text_setting(
            "endpoint.api_key", "API KEY", secret=True),
              value=lambda s: s.app.config.format("endpoint.api_key"),
              help="Bearer token, if the server requires one."),
        Entry(SETTING, key="endpoint.timeout"),
    ],
    "SAMPLING": [
        Entry(SETTING, key="sampling.temperature"),
        Entry(SETTING, key="sampling.top_p"),
        Entry(SETTING, key="sampling.top_k"),
        Entry(SETTING, key="sampling.repeat_penalty"),
        Entry(SETTING, key="sampling.max_tokens"),
        Entry(SETTING, key="sampling.seed"),
    ],
    "CONVERSATION": [
        Entry(SETTING, key="chat.persona"),
        Entry(ACTION, label="Edit prompt", action=_edit_custom_prompt,
              value=lambda s: ("SET" if s.app.config.get("chat.custom_prompt")
                               else "-none-"),
              help="Write a custom system prompt."),
        Entry(SETTING, key="chat.reserve"),
        Entry(SETTING, key="chat.show_reasoning"),
        Entry(SETTING, key="chat.autoscroll"),
        Entry(SETTING, key="chat.stream_rate"),
        Entry(SETTING, key="chat.save_history"),
    ],
    "DISPLAY": [
        Entry(SETTING, key="display.backlight"),
        Entry(SETTING, key="display.dim_after"),
        Entry(SETTING, key="display.fps"),
        Entry(SETTING, key="display.i2c_bus"),
        Entry(SETTING, key="display.i2c_addr"),
    ],
    "INPUT": [
        Entry(SETTING, key="input.grab"),
        Entry(SETTING, key="input.repeat_nav"),
        Entry(ACTION, label="Bluetooth", action=_bluetooth,
              value=lambda s: f"{len(s.app.keyboard.devices)} HID",
              help="Pair or reconnect a Bluetooth keyboard."),
    ],
    "NETWORK": [
        Entry(ACTION, label="Wi-Fi", action=_wifi,
              value=lambda s: _wifi_value(s),
              help="Join a wireless network."),
        Entry(ACTION, label="Address", action=lambda s: None,
              value=lambda s: _ip_value(s),
              help="This machine's current IP address."),
    ],
    "SYSTEM": [
        Entry(ACTION, label="Save now", action=_save_now,
              help="Write settings to disk immediately."),
        Entry(ACTION, label="About", action=_show_about),
        Entry(ACTION, label="Defaults", action=_reset_defaults,
              help="Restore every setting to its default."),
        Entry(ACTION, label="Exit", action=_quit_program,
              help="Stop the terminal and return to a shell."),
        Entry(ACTION, label="Reboot", action=_reboot),
        Entry(ACTION, label="Power off", action=_power_off),
    ],
}

SECTION_ORDER = ["INFERENCE", "ENDPOINT", "SAMPLING", "CONVERSATION",
                 "DISPLAY", "INPUT", "NETWORK", "SYSTEM"]

_SECTION_HINTS = {
    "INFERENCE": "Model, context window and engine behaviour.",
    "ENDPOINT": "Where the llama.cpp server lives.",
    "SAMPLING": "How the model picks its next token.",
    "CONVERSATION": "Persona, history and streaming.",
    "DISPLAY": "Panel, backlight and refresh.",
    "INPUT": "Keyboards, wired and wireless.",
    "NETWORK": "Wireless networking.",
    "SYSTEM": "Configuration and power.",
}

_cached_ip = {"value": "", "when": 0.0}


def _ip_value(screen: "SectionScreen") -> str:
    import time
    from ..services import net as net_service
    now = time.monotonic()
    if now - _cached_ip["when"] > 5.0:
        _cached_ip["value"] = net_service.primary_ip() or "NONE"
        _cached_ip["when"] = now
    return str(_cached_ip["value"])[-12:]


def _wifi_value(screen: "SectionScreen") -> str:
    from ..services import net as net_service
    if not net_service.available():
        return "N/A"
    return "SET UP"


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

class SettingsScreen(Screen):
    """Top level: the list of sections."""

    bank = G.BANK_MENU
    title = "SETTINGS"

    def __init__(self, app):
        super().__init__(app)
        self.list = ListView(rows=3)
        self.list.set_count(len(SECTION_ORDER))
        self._before = app.config.snapshot()

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
        if event.key == K.HOME:
            self.list.home()
            return True
        if event.key == K.END:
            self.list.end()
            return True
        if event.key in (K.ENTER, K.RIGHT):
            name = SECTION_ORDER[self.list.index]
            self.app.push(SectionScreen(self.app, name))
            return True
        if event.key in (K.ESC, K.F7, K.F4, K.LEFT):
            self.dismiss()
            return True
        return False

    def dismiss(self) -> None:
        """Leave settings, then apply anything that needs a fresh engine.

        Closing before committing matters: the commit may raise a restart
        prompt, and that prompt has to land on the screen underneath rather
        than on a settings screen that is about to be popped out from under it.
        """
        self.close(None)
        self._commit()

    def _commit(self) -> None:
        """Persist, then restart the engine once if anything requires it."""
        config = self.app.config
        changed = restart_required(self._before, config.snapshot())
        if config.dirty:
            config.save()
        self.app.apply_engine_config()
        if changed:
            keys = ", ".join(k.split(".")[-1].upper() for k in changed[:2])
            self.app.confirm(
                "RESTART ENGINE?",
                lambda: self.app.restart_engine("APPLYING CHANGES"),
                detail=f"{keys} needs a fresh engine.")

    def draw(self, frame: Frame) -> None:
        draw_title(frame, "SETTINGS",
                   f"{self.list.index + 1}/{len(SECTION_ORDER)}")
        self.list.draw(
            self.display, frame,
            lambda index, width: (SECTION_ORDER[index], G.ROM_RIGHT_ARROW))


class SectionScreen(Screen):
    """One section: rows of settings and actions, edited in place."""

    bank = G.BANK_MENU

    def __init__(self, app, section: str):
        super().__init__(app)
        self.section = section
        self.entries: List[Entry] = SECTIONS[section]
        self.list = ListView(rows=3)
        self.list.set_count(len(self.entries))
        self.adjusting = False
        self.activity = Activity(fps=6.0)
        self.restart_keys: List[str] = []
        self._help_shown = False

    @property
    def current(self) -> Optional[Entry]:
        if not self.entries:
            return None
        return self.entries[self.list.index]

    def mark_restart(self, key: str) -> None:
        if key not in self.restart_keys:
            self.restart_keys.append(key)

    # -- input --------------------------------------------------------------

    def on_key(self, event: KeyEvent) -> bool:
        entry = self.current
        if entry is None:
            if event.key == K.ESC:
                self.close(None)
                return True
            return False

        if self.adjusting:
            return self._adjust_key(event, entry)

        if event.key == K.UP:
            self.list.move(-1)
            return True
        if event.key == K.DOWN:
            self.list.move(1)
            return True
        if event.key in (K.PGUP, K.PGDN):
            self.list.page(-1 if event.key == K.PGUP else 1)
            return True
        if event.key == K.HOME:
            self.list.home()
            return True
        if event.key == K.END:
            self.list.end()
            return True
        if event.key in (K.ESC, K.LEFT):
            self.close(None)
            return True
        if event.key == K.F1:
            self._show_help(entry)
            return True
        if event.key in (K.ENTER, K.RIGHT):
            return self._activate(entry)
        # Plus and minus work on a highlighted numeric row without entering
        # adjust mode first: for the context window in particular, stepping it
        # should take one key, not three.
        if event.key in (K.PLUS, K.MINUS) or (event.is_char() and
                                              event.char in "+-"):
            direction = 1 if (event.key == K.PLUS or event.char == "+") else -1
            return self._step(entry, direction, big=False)
        return False

    def _activate(self, entry: Entry) -> bool:
        if entry.kind == ACTION:
            if entry.action is not None:
                entry.action(self)
            return True
        if entry.kind == SUBMENU:
            self.app.push(SectionScreen(self.app, entry.section))
            return True

        setting = self.app.config.setting(entry.key)
        if setting.kind == "bool":
            self.app.config.adjust(entry.key, 1)
            self._after_change(entry.key)
            return True
        if setting.kind in ("text", "secret"):
            _edit_text_setting(entry.key, setting.label.upper(),
                               secret=(setting.kind == "secret"))(self)
            return True
        self.adjusting = True
        return True

    def _adjust_key(self, event: KeyEvent, entry: Entry) -> bool:
        if event.key in (K.ENTER, K.ESC):
            self.adjusting = False
            return True
        if event.key == K.RIGHT or event.key == K.PLUS:
            return self._step(entry, 1)
        if event.key == K.LEFT or event.key == K.MINUS:
            return self._step(entry, -1)
        if event.key == K.UP:
            return self._step(entry, 1, big=True)
        if event.key == K.DOWN:
            return self._step(entry, -1, big=True)
        if event.key == K.HOME:
            self.app.config.reset(entry.key)
            self._after_change(entry.key)
            return True
        if event.is_char():
            if event.char == "+":
                return self._step(entry, 1)
            if event.char == "-":
                return self._step(entry, -1)
        return True

    def _step(self, entry: Entry, direction: int, big: bool = False) -> bool:
        if entry.kind != SETTING:
            return False
        setting = self.app.config.setting(entry.key)
        if setting.kind not in ("int", "float", "enum", "bool"):
            return False
        before = self.app.config.get(entry.key)
        after = self.app.config.adjust(entry.key, direction, big)
        if after == before:
            if setting.kind in ("int", "float"):
                limit = setting.maximum if direction > 0 else setting.minimum
                self.app.notify(f"LIMIT {setting.format(limit)}")
            return True
        self._after_change(entry.key)
        return True

    def _after_change(self, key: str) -> None:
        setting = self.app.config.setting(key)
        if setting.restart:
            self.mark_restart(key)
        self.app.apply_engine_config()
        if key == "display.backlight":
            self.app.set_backlight(bool(self.app.config.get(key)))
        if key == "chat.persona":
            self.app.conversation.system_prompt = self.app._system_prompt()
        if key == "chat.show_reasoning":
            from .chat import ChatScreen
            chat = self.app.find_screen(ChatScreen)
            if chat is not None:
                chat.invalidate_lines()
        if key == "engine.context":
            self._warn_context()

    def _warn_context(self) -> None:
        """Flag a context window the loaded model was not trained for."""
        model = self.app.model_info
        if model is None or not model.train_context:
            return
        requested = int(self.app.config.get("engine.context"))
        if requested > model.train_context:
            self.app.notify(f"ABOVE NATIVE {T.format_count(model.train_context)}",
                            2.5)

    def _show_help(self, entry: Entry) -> None:
        from .dialogs import MessageScreen
        body = entry.resolve_help(self)
        if not body:
            self.app.notify("NO DETAIL")
            return
        label = entry.resolve_label(self).upper()
        if entry.kind == SETTING:
            setting = self.app.config.setting(entry.key)
            extra = []
            if setting.minimum is not None and setting.kind in ("int", "float"):
                extra.append(f"Range {setting.minimum:g} to {setting.maximum:g}, "
                             f"step {setting.step:g}.")
            if setting.restart:
                extra.append("Applies on the next engine start.")
            if extra:
                body = body + "\n\n" + " ".join(extra)
        self.app.push(MessageScreen(self.app, label, body))

    # -- drawing ------------------------------------------------------------

    def draw(self, frame: Frame) -> None:
        counter = f"{self.list.index + 1}/{len(self.entries)}"
        draw_title(frame, self.section, counter)
        if not self.entries:
            frame.text(2, 0, T.fit("EMPTY", frame.cols, align="center"))
            return
        self.list.draw(self.display, frame, self._render_row)

    def _render_row(self, index: int, width: int) -> tuple:
        entry = self.entries[index]
        label = entry.resolve_label(self)
        value = entry.resolve_value(self)
        if self.adjusting and index == self.list.index:
            # Flanking the value with arrows is the whole affordance: it says
            # which keys move it, without spending a row on instructions.
            value = f"{self.g('left')}{value}{self.g('right')}"
        return label, value

    @property
    def hide_caret(self) -> bool:                     # type: ignore[override]
        return True
