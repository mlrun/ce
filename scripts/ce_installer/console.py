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
"""Logging and the fatal-error type."""

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


def _emit(console: Console, tag: str, style: str, message: str) -> None:
    # Built as a Text rather than markup: log messages routinely contain square brackets
    # (jsonpath expressions, arrays, "[ERROR]") that rich would try to parse as tags.
    line = Text()
    line.append(tag, style=style)
    line.append(" " + message)
    console.print(line)


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
