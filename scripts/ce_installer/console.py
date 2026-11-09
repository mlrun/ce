# Copyright 2025 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Logging, the per-run debug log, and the fatal-error type."""

import os
import threading
from pathlib import Path
from typing import List, Optional, TextIO

from rich.console import Console
from rich.text import Text

# rich suppresses color on its own when stdout is not a terminal and when NO_COLOR is set
# (https://no-color.org), which is the behaviour the bash installer hand-rolled: a piped or
# redirected run — CI logs, `| tee install.log` — reads as text instead of escapes.
out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)


class InstallerError(Exception):
    """Fatal, already-reported error. Carries the exit code the CLI should use."""

    def __init__(self, message: str = "", code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


class Cancelled(InstallerError):
    """The user said no. Exit 1 like click's Abort: a wrapper running `install && next`
    must not carry on, 130 belongs to Ctrl-C and 2 to a usage error. Not a failure, so it
    is reported at info level and without the debug log's path."""


# --------------------------------------------------------------------------------------
# Debug log
#
# One file per run, overwritten each time, holding everything the terminal shows plus what
# it does not: every command with its exit code, timing and output. A failed install points
# at it, so a bug report can carry the whole run instead of the last screenful.
# --------------------------------------------------------------------------------------

_debug_log: Optional[TextIO] = None
_debug_log_path = ""
# Values that must never reach the file, whatever printed them. The registry password is
# kept off argv and out of stdout already; this is the backstop if either ever changes.
_secrets: List[str] = []
# helm's stdout and stderr are teed from two threads.
_debug_lock = threading.Lock()


def open_debug_log(path: str, header: List[str]) -> str:
    """Start the run's debug log at `path`, owner-readable only. Returns the resolved path,
    or "" when the file cannot be written — a missing log must not stop an install."""
    global _debug_log, _debug_log_path  # noqa: PLW0603
    close_debug_log()
    resolved = Path(path).expanduser().resolve()
    try:
        fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # O_CREAT's mode applies only to a new file; one left by an earlier run keeps its own.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        _debug_log = os.fdopen(fd, "w", encoding="utf-8")
    except OSError as exc:
        log_warn(f"Could not write the debug log {resolved} ({exc.strerror}); continuing without.")
        return ""
    _debug_log_path = str(resolved)
    for line in header:
        log_debug(line)
    log_info(f"Debug log: {_debug_log_path}")
    return _debug_log_path


def close_debug_log() -> None:
    global _debug_log  # noqa: PLW0603
    if _debug_log is not None:
        _debug_log.close()
        _debug_log = None


def debug_log_path() -> str:
    return _debug_log_path if _debug_log is not None else ""


def add_secret(value: str) -> None:
    """Mask `value` wherever it would be written to the debug log."""
    if value and value not in _secrets:
        _secrets.append(value)


def log_debug(message: str) -> None:
    """Write to the debug log only; the terminal never sees it."""
    if _debug_log is None:
        return
    for secret in _secrets:
        message = message.replace(secret, "<redacted>")
    with _debug_lock:
        _debug_log.write(message if message.endswith("\n") else message + "\n")
        _debug_log.flush()


def _emit(console: Console, tag: str, style: str, message: str) -> None:
    # Built as a Text rather than markup: log messages routinely contain square brackets
    # (jsonpath expressions, arrays, "[ERROR]") that rich would try to parse as tags.
    line = Text()
    line.append(tag, style=style)
    line.append(" " + message)
    console.print(line)
    log_debug(f"{tag} {message}")


def log_info(message: str) -> None:
    _emit(out, "[INFO]", "green", message)


def log_warn(message: str) -> None:
    _emit(out, "[WARN]", "yellow", message)


def log_error(message: str) -> None:
    _emit(err, "[ERROR]", "red", message)


def die(message: str, code: int = 1) -> InstallerError:
    """Report an error and build the exception to raise, so call sites read as `raise die(...)`."""
    log_error(message)
    return InstallerError(message, code)


def cancel(message: str) -> Cancelled:
    log_info(message)
    return Cancelled(message)
