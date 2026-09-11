"""
Supervisor for a local ``llama-server`` process.

The server is started once and kept alive for the life of the program.  That is
not laziness -- it is the single largest performance decision in the whole
design.  Starting a fresh inference process per message would re-map the
weights and re-evaluate the entire conversation every turn; keeping one server
resident means the KV cache survives between turns and each message costs only
the tokens the operator actually typed.  On a Pi 5 that is the difference
between a reply beginning in under a second and a reply beginning in forty.

Two details make this robust across llama.cpp versions, which move quickly:

  * The command line is built from flags the installed binary actually
    advertises.  ``--flash-attn`` became ``-fa on|off|auto``, ``/slots`` became
    opt-in, and ``--cache-reuse`` did not always exist.  Passing an unknown
    flag makes llama-server exit immediately, so the supervisor reads
    ``--help`` once and only passes what it finds.

  * stderr is drained continuously into a ring buffer.  A subprocess whose
    stderr pipe fills up blocks forever, and llama.cpp is chatty during model
    load -- so not draining it is a hang waiting to happen.  The buffer doubles
    as the source for the diagnostics screen.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

#: Where a llama.cpp build usually ends up, in the order worth trying.
_BINARY_CANDIDATES = (
    "vendor/llama.cpp/build/bin/llama-server",
    "llama.cpp/build/bin/llama-server",
    "~/llama.cpp/build/bin/llama-server",
    "~/.local/bin/llama-server",
    "/usr/local/bin/llama-server",
    "/usr/bin/llama-server",
    "/opt/llama.cpp/build/bin/llama-server",
)

#: Messages llama.cpp prints that deserve a plain-language translation.
_ERROR_PATTERNS = (
    (re.compile(r"unknown argument|invalid argument|error while handling arg",
                re.I),
     "ENGINE REJECTED ARGS"),
    (re.compile(r"failed to load model|unable to load model|error loading model",
                re.I),
     "MODEL WOULD NOT LOAD"),
    (re.compile(r"cannot allocate|out of memory|failed to allocate|oom", re.I),
     "OUT OF MEMORY"),
    (re.compile(r"bind.*(address already in use)|address already in use", re.I),
     "PORT ALREADY IN USE"),
    (re.compile(r"no such file or directory", re.I),
     "MODEL FILE MISSING"),
    (re.compile(r"unsupported model|unknown model architecture", re.I),
     "UNSUPPORTED ARCHITECTURE"),
)


def find_binary(configured: str = "", project_root: str = "") -> str:
    """Locate ``llama-server``.  Returns "" if it cannot be found."""
    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    env = os.environ.get("LLAMA_SERVER_BIN", "")
    if env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)

    found = shutil.which("llama-server")
    if found:
        return found

    roots = [project_root] if project_root else []
    roots.append(os.getcwd())
    for candidate in _BINARY_CANDIDATES:
        if candidate.startswith(("~", "/")):
            path = os.path.expanduser(candidate)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
            continue
        for root in roots:
            path = os.path.join(root, candidate)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return ""


@dataclass
class LaunchPlan:
    """The exact command that will be run, for display before it is."""

    binary: str
    args: List[str]

    def command_line(self) -> str:
        return " ".join([self.binary] + self.args)


class LlamaServer:
    """Owns one ``llama-server`` child process."""

    LOG_LINES = 300

    def __init__(self, binary: str, log_lines: int = LOG_LINES):
        self.binary = binary
        self.process: Optional[subprocess.Popen] = None
        self.log: collections.deque = collections.deque(maxlen=log_lines)
        self.started_at = 0.0
        self.last_error = ""
        self.plan: Optional[LaunchPlan] = None
        self._reader: Optional[threading.Thread] = None
        self._flags: Optional[set] = None
        self._lock = threading.Lock()

    # -- capability probing -------------------------------------------------

    def supported_flags(self) -> set:
        """Flags this binary advertises in ``--help``.

        Cached: the probe costs a process spawn, and the binary does not change
        while the program is running.
        """
        if self._flags is not None:
            return self._flags
        flags: set = set()
        try:
            result = subprocess.run(
                [self.binary, "--help"], capture_output=True, text=True,
                timeout=20, check=False)
            text = (result.stdout or "") + (result.stderr or "")
            for match in re.finditer(r"(--[a-zA-Z0-9][a-zA-Z0-9-]*)", text):
                flags.add(match.group(1))
            for match in re.finditer(r"(?<![\w-])(-[a-zA-Z]{1,4})(?![\w-])", text):
                flags.add(match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
        self._flags = flags
        return flags

    def version(self) -> str:
        try:
            result = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True,
                timeout=15, check=False)
            text = ((result.stdout or "") + (result.stderr or "")).strip()
            return text.splitlines()[0][:60] if text else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            return ""

    # -- command construction ----------------------------------------------

    def build_plan(self, model_path: str, host: str, port: int,
                   n_ctx: int, threads: int, gpu_layers: int, batch: int,
                   cache_reuse: int, mlock: bool, flash_attn: bool,
                   api_key: str = "") -> LaunchPlan:
        """Assemble the command line, using only flags the binary accepts."""
        flags = self.supported_flags()

        def has(*names: str) -> Optional[str]:
            for name in names:
                if name in flags:
                    return name
            return None

        args: List[str] = ["-m", model_path, "--host", host, "--port", str(port)]
        args += ["-c", str(int(n_ctx))]

        # One slot holding the full window.  llama-server divides the context
        # between parallel slots, so asking for more than one would silently
        # halve (or worse) the conversation length the operator configured.
        if has("--parallel", "-np"):
            args += [has("--parallel", "-np"), "1"]

        if threads > 0:
            args += ["-t", str(int(threads))]
        if gpu_layers >= 0 and has("-ngl", "--n-gpu-layers", "--gpu-layers"):
            args += ["-ngl", str(int(gpu_layers))]
        if batch > 0:
            args += ["-b", str(int(batch))]
            if has("-ub", "--ubatch-size"):
                args += ["-ub", str(min(int(batch), 512))]

        # Partial prefix reuse: lets the server salvage the KV cache after the
        # conversation is trimmed from the front, instead of starting over.
        if cache_reuse > 0 and has("--cache-reuse"):
            args += ["--cache-reuse", str(int(cache_reuse))]

        if mlock and has("--mlock"):
            args += ["--mlock"]

        # Flash attention changed from a boolean switch to a tri-state option.
        if flash_attn:
            if "--flash-attn" in flags or "-fa" in flags:
                if _flag_takes_value(self.binary, flags):
                    args += ["-fa", "on"]
                else:
                    args += ["--flash-attn"]

        # Exposes /slots, which the diagnostics screen reads to show how much
        # of the KV cache is live.  Opt-in on recent builds.
        if has("--slots"):
            args += ["--slots"]
        if has("--metrics"):
            args += ["--metrics"]
        if api_key and has("--api-key"):
            args += ["--api-key", api_key]

        plan = LaunchPlan(binary=self.binary, args=args)
        self.plan = plan
        return plan

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def uptime(self) -> float:
        return (time.monotonic() - self.started_at) if self.running else 0.0

    def start(self, plan: Optional[LaunchPlan] = None,
              env: Optional[Dict[str, str]] = None) -> bool:
        """Spawn the server.  Returns False with ``last_error`` set on failure."""
        plan = plan or self.plan
        if plan is None:
            self.last_error = "NO LAUNCH PLAN"
            return False
        if self.running:
            return True

        self.log.clear()
        self.last_error = ""
        environment = dict(os.environ)
        if env:
            environment.update(env)

        self.log.append(f"$ {plan.command_line()}")
        try:
            self.process = subprocess.Popen(
                [plan.binary] + plan.args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
                # Its own process group, so a Ctrl+C in the controlling
                # terminal reaches this program's handler rather than killing
                # the engine out from under it.
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self.last_error = f"CANNOT EXEC ENGINE: {exc}"
            self.process = None
            return False

        self.started_at = time.monotonic()
        self._reader = threading.Thread(target=self._drain, daemon=True,
                                        name="llama-log")
        self._reader.start()
        return True

    def _drain(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                with self._lock:
                    self.log.append(line)
                for pattern, message in _ERROR_PATTERNS:
                    if pattern.search(line):
                        self.last_error = message
                        break
        except (OSError, ValueError):
            pass

    def tail(self, count: int = 20) -> List[str]:
        with self._lock:
            return list(self.log)[-count:]

    def exit_summary(self) -> str:
        """Why the process is not running, in words that fit the panel."""
        if self.process is None:
            return "ENGINE NOT STARTED"
        code = self.process.poll()
        if code is None:
            return ""
        if self.last_error:
            return self.last_error
        if code < 0:
            return f"ENGINE KILLED (SIG{-code})"
        return f"ENGINE EXITED ({code})"

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the server to exit, then insist."""
        process = self.process
        if process is None:
            return
        if process.poll() is not None:
            self.process = None
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.process = None
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None


def _flag_takes_value(binary: str, flags: set) -> bool:
    """Whether this build's flash-attention switch expects ``on``/``off``.

    Detected from the help text rather than from a version number, because the
    distribution packages and the source build drift apart.
    """
    try:
        result = subprocess.run([binary, "--help"], capture_output=True,
                                text=True, timeout=20, check=False)
        text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"-fa,\s*--flash-attn\s+(\S+)", text)
    if match and match.group(1).upper() not in ("", "\n"):
        return "on" in match.group(1).lower() or "{" in match.group(1)
    return bool(re.search(r"--flash-attn\s+(?:on|\{|\[)", text, re.I))


def port_in_use(host: str, port: int, timeout: float = 0.4) -> bool:
    """True if something already answers on *host:port*.

    Checked before launching so that attaching to an engine the operator
    already started is a first-class outcome rather than a port-bind crash.
    """
    import socket
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((target, int(port)), timeout=timeout):
            return True
    except OSError:
        return False
