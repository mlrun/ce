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
"""Keep rendered Secret values out of the debug log.

helm's dry-run and install output carries every rendered manifest, and the chart renders
Secrets with real values (database passwords, S3 keys, Grafana's admin password). The
README tells users to attach the log to a bug report as is, so the values under `data:`
and `stringData:` of every `kind: Secret` document are replaced before the log sees them.
The terminal output is untouched.

Line-based rather than a YAML parse: the stream mixes manifests with helm's own text
(NAME:, HOOKS:, NOTES:), and a document is only known to be a Secret once its `kind:` line
is read, which may come after `data:`. So each document is held until the next `---`.
"""

import re
from typing import List

REDACTED = "<redacted>"

_SEPARATOR = re.compile(r"^---(\s|$)")
_KIND_SECRET = re.compile(r"""^kind:\s*["']?Secret["']?\s*(#.*)?$""")
_DATA_BLOCK = re.compile(r"^(data|stringData):(\s+(?P<inline>\S.*))?\s*$")
_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#'"][^:]*?):(\s|$)""")


class SecretDataRedactor:
    """Feed lines as they arrive; get back the lines that are safe to log.

    Output lags by one document, so call flush() at the end of the stream.
    """

    def __init__(self) -> None:
        self._document: List[str] = []

    def feed(self, line: str) -> List[str]:
        if _SEPARATOR.match(line) and self._document:
            done = self._finish()
            self._document = [line]
            return done
        self._document.append(line)
        return []

    def flush(self) -> List[str]:
        done = self._finish()
        self._document = []
        return done

    def _finish(self) -> List[str]:
        lines = self._document
        if not any(_KIND_SECRET.match(line.rstrip("\r\n")) for line in lines):
            return lines
        return _redact_data_blocks(lines)


def _redact_data_blocks(lines: List[str]) -> List[str]:
    out: List[str] = []
    in_block = False
    child_indent = -1
    for line in lines:
        text = line.rstrip("\r\n")
        ending = line[len(text) :]
        indent = len(text) - len(text.lstrip(" "))
        if in_block:
            if not text.strip():
                out.append(line)
                continue
            if indent > 0:
                if child_indent < 0:
                    child_indent = indent
                key = _KEY.match(text[indent:]) if indent == child_indent else None
                if key:
                    out.append(f"{text[:indent]}{key.group('key')}: {REDACTED}{ending}")
                # Anything deeper is the continuation of a value (a `|` block, a folded
                # scalar) and goes with it.
                continue
            in_block = False
        block = _DATA_BLOCK.match(text)
        if block:
            inline = block.group("inline")
            if inline is None:
                in_block, child_indent = True, -1
                out.append(line)
            elif inline in ("{}", "null", "~"):
                out.append(line)
            else:
                out.append(f"{block.group(1)}: {REDACTED}{ending}")
            continue
        out.append(line)
    return out


def redact_secret_data(text: str) -> str:
    """The whole-text form, for output captured in one piece."""
    redactor = SecretDataRedactor()
    kept: List[str] = []
    for line in text.splitlines(keepends=True):
        kept.extend(redactor.feed(line))
    kept.extend(redactor.flush())
    return "".join(kept)
