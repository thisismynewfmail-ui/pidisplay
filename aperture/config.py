"""
Configuration store and the typed schema that drives the settings UI.

There is one source of truth here.  Every adjustable value is declared once as
a :class:`Setting` -- with its type, bounds, step, unit and help text -- and
both the validator and the on-screen settings tree are generated from that
declaration.  The alternative (a dict of defaults plus a hand-written menu)
guarantees that the two drift apart, and on a display with room for three menu
rows, a stale menu is not a cosmetic problem.

Values live in a nested dictionary and are addressed by dotted path.  The file
is written atomically, because the realistic failure mode for an appliance like
this is losing power mid-write, and a half-written config that refuses to parse
would strand the device with no way in.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

APP_NAME = "aperture"

#: Context length moves in 512-token steps in both directions.
CONTEXT_STEP = 512
CONTEXT_MIN = 512
CONTEXT_MAX = 131072


def _default_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, APP_NAME)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

@dataclass
class Setting:
    """One adjustable value.

    ``label`` is budgeted for the settings rows, which have twelve columns for
    the name once the cursor and the value are accounted for.  The constructor
    enforces that rather than letting a long label silently truncate.
    """

    key: str
    label: str
    kind: str                       # int | float | bool | enum | text | secret
    default: Any
    help: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: float = 1
    big_step: Optional[float] = None
    choices: Sequence[Any] = ()
    choice_labels: Sequence[str] = ()
    unit: str = ""
    restart: bool = False           # takes effect on next engine start
    secret: bool = False
    formatter: Optional[Callable[[Any], str]] = None
    max_length: int = 96

    LABEL_BUDGET = 12

    def __post_init__(self) -> None:
        if len(self.label) > self.LABEL_BUDGET:
            raise ValueError(
                f"setting {self.key!r} label {self.label!r} is "
                f"{len(self.label)} chars; the menu row allows "
                f"{self.LABEL_BUDGET}"
            )
        if self.kind == "enum" and not self.choices:
            raise ValueError(f"setting {self.key!r} is an enum with no choices")
        if self.choice_labels and len(self.choice_labels) != len(self.choices):
            raise ValueError(f"setting {self.key!r} has mismatched choice labels")
        if self.big_step is None:
            self.big_step = self.step * 10

    # -- value handling -----------------------------------------------------

    def coerce(self, value: Any) -> Any:
        """Force *value* into this setting's type and bounds."""
        if self.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.kind == "int":
            try:
                value = int(round(float(value)))
            except (TypeError, ValueError):
                return self.default
            return int(self._clamp(value))
        if self.kind == "float":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return self.default
            return round(self._clamp(value), 4)
        if self.kind == "enum":
            return value if value in self.choices else self.default
        # text / secret
        if value is None:
            return ""
        return str(value)[:self.max_length]

    def _clamp(self, value: float) -> float:
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value

    def adjust(self, value: Any, direction: int, big: bool = False) -> Any:
        """Return *value* nudged by one step in *direction* (+1 / -1)."""
        if self.kind == "bool":
            return not bool(value)
        if self.kind == "enum":
            try:
                index = list(self.choices).index(value)
            except ValueError:
                index = 0
            return self.choices[(index + direction) % len(self.choices)]
        if self.kind in ("int", "float"):
            step = self.big_step if big else self.step
            return self.coerce(value + direction * step)
        return value

    def format(self, value: Any) -> str:
        if self.formatter is not None:
            return self.formatter(value)
        if self.kind == "bool":
            return "ON" if value else "OFF"
        if self.kind == "enum":
            if self.choice_labels:
                try:
                    return self.choice_labels[list(self.choices).index(value)]
                except ValueError:
                    pass
            return str(value)
        if self.kind == "secret":
            return "*" * min(6, len(str(value))) if value else "-none-"
        if self.kind == "float":
            return f"{value:g}{self.unit}"
        if self.kind == "int":
            return f"{value}{self.unit}"
        text = str(value)
        return text if text else "-none-"


def _fmt_tokens(value: int) -> str:
    if value >= 1024 and value % 1024 == 0:
        return f"{value // 1024}K"
    return str(value)


def _fmt_gpu_layers(value: int) -> str:
    return "AUTO" if value < 0 else ("CPU" if value == 0 else str(value))


def _fmt_threads(value: int) -> str:
    return "AUTO" if value <= 0 else str(value)


def _fmt_addr(value: int) -> str:
    return "AUTO" if value <= 0 else f"0x{value:02X}"


def _fmt_seconds(value: int) -> str:
    if value <= 0:
        return "NEVER"
    if value >= 60:
        return f"{value // 60}m"
    return f"{value}s"


#: The complete schema, in the order the settings tree presents it.
SETTINGS: List[Setting] = [
    # -- inference ---------------------------------------------------------
    Setting("engine.context", "Context", "int", 4096,
            help="KV cache window in tokens. Larger holds more conversation "
                 "but costs memory and slows the first pass.",
            minimum=CONTEXT_MIN, maximum=CONTEXT_MAX,
            step=CONTEXT_STEP, big_step=CONTEXT_STEP * 8,
            unit="", restart=True, formatter=_fmt_tokens),
    Setting("engine.mode", "Engine", "enum", "local",
            help="LOCAL starts and supervises llama-server on this machine. "
                 "REMOTE attaches to a llama.cpp server already running.",
            choices=("local", "remote"), choice_labels=("LOCAL", "REMOTE"),
            restart=True),
    Setting("engine.model", "Model", "text", "",
            help="GGUF file inside the models directory. Blank selects the "
                 "first one found at startup.", restart=True),
    Setting("engine.gpu_layers", "GPU layers", "int", 0,
            help="Layers offloaded to the GPU. The Pi 5 has no supported "
                 "accelerator, so 0 is correct here; kept for other hosts.",
            minimum=-1, maximum=999, step=1, big_step=10,
            restart=True, formatter=_fmt_gpu_layers),
    Setting("engine.threads", "Threads", "int", 4,
            help="Generation threads. The Pi 5 has four Cortex-A76 cores; "
                 "above four the cores contend and throughput drops.",
            minimum=0, maximum=64, step=1, big_step=4,
            restart=True, formatter=_fmt_threads),
    Setting("engine.batch", "Batch", "int", 256,
            help="Prompt-ingest batch size. Larger is faster to first token "
                 "but uses more scratch memory.",
            minimum=32, maximum=4096, step=32, big_step=256, restart=True),
    Setting("engine.cache_reuse", "Cache reuse", "int", 256,
            help="Minimum chunk the server will salvage from the KV cache "
                 "after an edit. 0 disables partial reuse.",
            minimum=0, maximum=4096, step=64, big_step=256, restart=True),
    Setting("engine.mlock", "Lock in RAM", "bool", False,
            help="mlock the weights so Linux cannot swap them out. Only safe "
                 "when the model comfortably fits in memory.", restart=True),
    Setting("engine.flash_attn", "Flash attn", "bool", True,
            help="Use the fused attention kernel when the build supports it. "
                 "Lower memory, usually faster.", restart=True),
    Setting("engine.prefill", "Prefill", "bool", True,
            help="After each reply, pre-ingest the conversation so the next "
                 "turn only evaluates what you actually typed.",),
    Setting("engine.type_prefill", "Type-ahead", "bool", False,
            help="Also pre-ingest your partial line while you pause typing. "
                 "Fastest replies, but keeps a core busy as you type.",),

    # -- endpoint ----------------------------------------------------------
    Setting("endpoint.host", "Host", "text", "127.0.0.1",
            help="Address of the llama.cpp server.", restart=True),
    Setting("endpoint.port", "Port", "int", 8080,
            help="TCP port of the llama.cpp server.",
            minimum=1, maximum=65535, step=1, big_step=100, restart=True),
    Setting("endpoint.api_key", "API key", "secret", "",
            help="Sent as a bearer token. Leave blank for an unsecured "
                 "server on the loopback interface.", restart=True),
    Setting("endpoint.timeout", "Timeout", "int", 600,
            help="Seconds to wait on a stalled response before giving up.",
            minimum=10, maximum=3600, step=10, big_step=60, unit="s"),

    # -- sampling ----------------------------------------------------------
    Setting("sampling.temperature", "Temperature", "float", 0.7,
            help="Higher is more varied, lower is more deterministic.",
            minimum=0.0, maximum=2.0, step=0.05, big_step=0.25),
    Setting("sampling.top_p", "Top P", "float", 0.95,
            help="Nucleus sampling cutoff.",
            minimum=0.05, maximum=1.0, step=0.05, big_step=0.2),
    Setting("sampling.top_k", "Top K", "int", 40,
            help="Consider only this many candidates. 0 disables the filter.",
            minimum=0, maximum=200, step=5, big_step=20),
    Setting("sampling.repeat_penalty", "Repeat pen", "float", 1.1,
            help="Discourages verbatim repetition. 1.0 is off.",
            minimum=1.0, maximum=2.0, step=0.02, big_step=0.1),
    Setting("sampling.max_tokens", "Max reply", "int", 512,
            help="Hard ceiling on one reply.",
            minimum=32, maximum=8192, step=32, big_step=256),
    Setting("sampling.seed", "Seed", "int", -1,
            help="-1 draws a fresh seed each turn; any other value makes "
                 "replies reproducible.",
            minimum=-1, maximum=2 ** 31 - 1, step=1, big_step=100,
            formatter=lambda v: "RANDOM" if v < 0 else str(v)),

    # -- conversation ------------------------------------------------------
    Setting("chat.persona", "Persona", "enum", "terminal",
            help="System prompt. All of them instruct the model to write for "
                 "a twenty-column display.",
            choices=("terminal", "terse", "technical", "plain", "custom"),
            choice_labels=("TERMINAL", "TERSE", "TECHNICAL", "PLAIN", "CUSTOM")),
    Setting("chat.reserve", "Reply room", "int", 640,
            help="Tokens held back from the context window so a reply always "
                 "has somewhere to go.",
            minimum=128, maximum=4096, step=64, big_step=256),
    Setting("chat.show_reasoning", "Show think", "bool", False,
            help="Display a reasoning model's internal notes as they stream. "
                 "They are summarised on the status row either way."),
    Setting("chat.autoscroll", "Autoscroll", "bool", True,
            help="Follow the newest line while a reply streams. Scrolling up "
                 "by hand suspends it until you return to the bottom."),
    Setting("chat.stream_rate", "Stream fps", "int", 12,
            help="Upper bound on display refreshes per second. The bus, not "
                 "the model, is the limit here.",
            minimum=2, maximum=30, step=1, big_step=5),
    Setting("chat.save_history", "Keep log", "bool", True,
            help="Append each exchange to a transcript file under the state "
                 "directory."),

    # -- display -----------------------------------------------------------
    Setting("display.i2c_bus", "I2C bus", "int", 1,
            help="Bus number. Header pins 3 and 5 are bus 1 on every Pi.",
            minimum=0, maximum=20, step=1, restart=True),
    Setting("display.i2c_addr", "I2C addr", "int", 0,
            help="Backpack address. AUTO probes 0x27 and 0x3F, which is where "
                 "these modules ship.",
            minimum=0, maximum=0x7F, step=1, restart=True,
            formatter=_fmt_addr),
    Setting("display.pinmap", "Pin map", "enum", "standard",
            help="How the backpack wires the expander to the display. Two "
                 "layouts exist; the wrong one shows nothing at all. Run "
                 "./run.sh --doctor to find yours.",
            choices=("standard", "ywrobot", "standard-inv", "ywrobot-inv"),
            choice_labels=("STANDARD", "YWROBOT", "STD-INV", "YW-INV"),
            restart=True),
    Setting("display.backlight", "Backlight", "bool", True,
            help="Panel backlight."),
    Setting("display.dim_after", "Dim after", "int", 0,
            help="Switch the backlight off after this long with no keystroke. "
                 "NEVER keeps it on.",
            minimum=0, maximum=3600, step=30, big_step=300,
            formatter=_fmt_seconds),
    Setting("display.fps", "Refresh", "int", 20,
            help="Render loop target. Frames with nothing to redraw cost "
                 "nothing, so this is a ceiling rather than a load.",
            minimum=5, maximum=40, step=1, big_step=5),

    # -- input -------------------------------------------------------------
    Setting("input.grab", "Grab keys", "bool", True,
            help="Claim keyboards exclusively so keystrokes do not also reach "
                 "a login shell on the console behind this program."),
    Setting("input.repeat_nav", "Key repeat", "bool", True,
            help="Let held arrow keys repeat for menu navigation."),
]

SETTINGS_BY_KEY: Dict[str, Setting] = {s.key: s for s in SETTINGS}


def default_config() -> Dict[str, Any]:
    """Build the nested default document from the schema."""
    root: Dict[str, Any] = {}
    for setting in SETTINGS:
        section, _, name = setting.key.partition(".")
        root.setdefault(section, {})[name] = setting.default
    root["paths"] = {
        "models": os.path.join(_default_root(), "models"),
        "llama_server": "",
        "state": state_dir(),
    }
    root["meta"] = {"version": 1, "first_run": True}
    root["chat"]["custom_prompt"] = ""
    return root


class Config:
    """A validated, atomically-persisted settings document."""

    def __init__(self, path: Optional[str] = None,
                 data: Optional[Dict[str, Any]] = None):
        self.path = path or config_path()
        self.data: Dict[str, Any] = data if data is not None else default_config()
        self.load_error = ""
        self.dirty = False

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        config = cls(path=path)
        try:
            with open(config.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return config
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt file must never be fatal: an appliance with no config
            # should boot on defaults and say so, not refuse to start.
            config.load_error = str(exc)
            return config

        if isinstance(stored, dict):
            config._merge(stored)
        config.validate()
        return config

    def _merge(self, stored: Dict[str, Any]) -> None:
        for section, values in stored.items():
            if not isinstance(values, dict):
                continue
            target = self.data.setdefault(section, {})
            for name, value in values.items():
                target[name] = value

    def save(self) -> bool:
        """Write the document atomically.  Returns True on success."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False,
                dir=os.path.dirname(self.path), prefix=".config-", suffix=".tmp")
            with handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
            self.dirty = False
            return True
        except OSError as exc:
            self.load_error = str(exc)
            return False

    def validate(self) -> None:
        for setting in SETTINGS:
            self.set(setting.key, self.get(setting.key), mark_dirty=False)

    # -- access -------------------------------------------------------------

    def get(self, key: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                setting = SETTINGS_BY_KEY.get(key)
                return setting.default if setting else fallback
            node = node[part]
        return node

    def set(self, key: str, value: Any, mark_dirty: bool = True) -> Any:
        setting = SETTINGS_BY_KEY.get(key)
        if setting is not None:
            value = setting.coerce(value)
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if node.get(parts[-1]) != value and mark_dirty:
            self.dirty = True
        node[parts[-1]] = value
        return value

    def adjust(self, key: str, direction: int, big: bool = False) -> Any:
        setting = SETTINGS_BY_KEY[key]
        return self.set(key, setting.adjust(self.get(key), direction, big))

    def setting(self, key: str) -> Setting:
        return SETTINGS_BY_KEY[key]

    def format(self, key: str) -> str:
        return SETTINGS_BY_KEY[key].format(self.get(key))

    def reset(self, key: str) -> Any:
        return self.set(key, SETTINGS_BY_KEY[key].default)

    def reset_all(self) -> None:
        self.data = default_config()
        self.dirty = True

    # -- derived ------------------------------------------------------------

    @property
    def models_dir(self) -> str:
        return os.path.expanduser(self.get("paths.models"))

    @property
    def base_url(self) -> str:
        return f"http://{self.get('endpoint.host')}:{int(self.get('endpoint.port'))}"

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)


def restart_required(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Settings that changed and only take effect on a fresh engine start."""
    changed = []
    for setting in SETTINGS:
        if not setting.restart:
            continue
        section, _, name = setting.key.partition(".")
        old = before.get(section, {}).get(name)
        new = after.get(section, {}).get(name)
        if old != new:
            changed.append(setting.key)
    return changed
