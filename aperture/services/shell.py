"""Small helper for running system tools without ever hanging the UI."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass
class Result:
    ok: bool
    out: str = ""
    err: str = ""
    code: int = -1

    @property
    def text(self) -> str:
        return self.out if self.out else self.err

    def lines(self) -> List[str]:
        return [line for line in self.out.splitlines() if line.strip()]


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def run(args: Sequence[str], timeout: float = 8.0,
        stdin_text: Optional[str] = None) -> Result:
    """Run *args*, capturing output.

    Every caller of this runs on a worker thread, but a system tool that blocks
    forever would still leak a thread per attempt, so everything gets a
    timeout.  A missing tool is reported as a normal failure rather than an
    exception: on a minimal Pi OS image, half of these are simply not
    installed, and that is a thing to display, not to crash on.
    """
    if not have(args[0]):
        return Result(False, err=f"{args[0]} not installed")
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            input=stdin_text, check=False)
    except subprocess.TimeoutExpired:
        return Result(False, err=f"{args[0]} timed out")
    except (OSError, ValueError) as exc:
        return Result(False, err=str(exc))
    return Result(completed.returncode == 0,
                  out=completed.stdout or "",
                  err=(completed.stderr or "").strip(),
                  code=completed.returncode)
