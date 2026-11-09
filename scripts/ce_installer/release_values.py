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
"""What an upgrade does to the values of the release it replaces.

`helm upgrade` without --reuse-values starts again from the chart defaults plus what this
run passes, so a value the current release was installed with and this run omits is reset
without a word. On an upgrade the installer saves the current values beside its own values
file, keeps the release's NodePorts (its URLs), and warns about every other value that
changes.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List

import yaml

from .console import log_debug, log_info, log_warn
from .helm_ops import _lookup, _values_file_contents, build_set_flags, set_flags_to_values
from .settings import NODEPORTS, Settings, previous_values_file, value_changes_file
from .shell import helm

UNSET = "(unset)"
REDACTED = "<redacted>"
# Leaf keys whose values stay out of the console and the changes file. Names like
# `secretName` are references, not secrets, and are shown.
_SENSITIVE = re.compile(r"(?i)(password|passwd|secretkey|accesskey|token|apikey|credentials?)$")

PREVIOUS_VALUES_HEADER = """\
# Written by the MLRun CE installer before upgrading: the values release '{release}' was
# installed with (`helm get values`). May hold secrets you set; readable by you only.
"""

CHANGES_HEADER = """\
# Written by the MLRun CE installer: the values this run changes on release '{release}'.
# `previous` is what the release has now, `new` what this run sets; (unset) is the chart's
# default. To keep a previous value, pass it again (in your -f file, for instance).
"""


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def load_previous_values(settings: Settings) -> None:
    """On an upgrade, save the current release's values and keep its NodePorts.

    Not a validator: --skip-validators skips checks, and an upgrade that silently moved
    every URL back to the chart's default ports would be a change, not a skipped check. A
    port the user sets in -f is theirs and is not overridden.
    """
    result = helm(
        settings, "get", "values", settings.release_name,
        "--namespace", settings.namespace, "-o", "json",
    )  # fmt: skip
    if not result.ok:
        log_debug(f"No current release values ({(result.err or result.out).strip()})")
        return
    try:
        loaded = json.loads(result.out) if result.out.strip() else {}
    except ValueError:
        log_warn("Could not read the current release's values; changes will not be reported.")
        return
    previous = loaded if isinstance(loaded, dict) else {}
    settings.previous_values = previous

    path = previous_values_file(settings)
    _write_private(
        path,
        PREVIOUS_VALUES_HEADER.format(release=settings.release_name)
        + yaml.safe_dump(previous, sort_keys=False),
    )
    log_info(f"Current release values saved to {path}")

    user_values = _values_file_contents(settings)
    for key in NODEPORTS:
        port = _lookup(previous, key)
        if isinstance(port, int) and _lookup(user_values, key) is None:
            settings.node_ports[key] = port


def _merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _flatten(values: dict, prefix: str = "") -> Dict[str, object]:
    flat: Dict[str, object] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _shown(key: str, value: object) -> object:
    if value is UNSET:
        return UNSET
    if _SENSITIVE.search(key.rsplit(".", 1)[-1]):
        return REDACTED
    return value


def _inline(value: object) -> str:
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def value_changes(settings: Settings) -> List[dict]:
    """Every value that differs between the current release and this run, by dotted key."""
    if settings.previous_values is None:
        return []
    new = _merge(_values_file_contents(settings), set_flags_to_values(build_set_flags(settings)))
    before, after = _flatten(settings.previous_values), _flatten(new)
    changes = []
    for key in sorted(set(before) | set(after)):
        old, now = before.get(key, UNSET), after.get(key, UNSET)
        if old != now:
            changes.append({"key": key, "previous": _shown(key, old), "new": _shown(key, now)})
    return changes


def report_value_changes(settings: Settings) -> None:
    """Warn about, and write down, every value this upgrade changes."""
    if settings.previous_values is None:
        return
    changes = value_changes(settings)
    settings.value_changes = len(changes)
    path = value_changes_file(settings)
    if not changes:
        path.unlink(missing_ok=True)
        log_info("Values: no change from the current release.")
        return

    log_warn(
        f"This upgrade changes {len(changes)} value(s) release '{settings.release_name}' "
        f"was installed with ({UNSET} is the chart's default):"
    )
    for change in changes:
        log_warn(f"  {change['key']}: {_inline(change['previous'])} -> {_inline(change['new'])}")
    _write_private(
        path,
        CHANGES_HEADER.format(release=settings.release_name)
        + yaml.safe_dump({"changes": changes}, sort_keys=False),
    )
    log_warn(f"  Written to {path}. To keep a previous value, pass it again.")
