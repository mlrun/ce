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
"""The live deployment-progress table and the post-install access-URL table."""

import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from rich.live import Live
from rich.table import Table
from rich.text import Text

from .console import out
from .settings import Settings
from .shell import helm, kubectl


def workload_rows(raw: str) -> List[Tuple[str, str, bool]]:
    """Turn `kubectl get deployments|statefulsets` table output into (name, ready, done)."""
    rows = []
    for line in raw.splitlines():
        if not line.strip() or line.startswith("NAME"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        name, ready = fields[0], fields[1]
        match = re.match(r"^(\d+)/(\d+)$", ready)
        done = bool(match) and match.group(1) == match.group(2)
        rows.append((name, ready, done))
    return rows


def progress_table(settings: Settings) -> Table:
    table = Table(
        title="MLRun CE - Deployment progress "
        f"(refreshing every {settings.progress_interval_sec}s)",
        title_style="cyan",
        header_style="cyan",
        expand=False,
    )
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Ready")
    table.add_column("Status")

    for kind, resource in (("Deployment", "deployments"), ("StatefulSet", "statefulsets")):
        result = kubectl(settings, "get", resource, "-n", settings.namespace)
        for name, ready, done in workload_rows(result.out if result.ok else ""):
            status = Text("Ready", style="green") if done else Text("Deploying...", style="yellow")
            table.add_row(kind, name, ready, status)

    if not table.rows:
        table.add_row("", "(no workloads yet)", "", "")
    return table


def run_deployment_progress_ui(settings: Settings, proc: subprocess.Popen) -> None:
    """Live-updating table of deployment/statefulset progress until helm exits.

    Only runs when stdout is a terminal; a piped or redirected run gets helm's own output
    instead, same as the bash version.
    """
    if not out.is_terminal:
        return

    try:
        interval = max(1, int(settings.progress_interval_sec))
    except ValueError:
        interval = 10

    with Live(console=out, refresh_per_second=4, transient=True) as live:
        while proc.poll() is None:
            live.update(progress_table(settings))
            # Poll in short slices rather than one long sleep so the UI exits promptly when
            # helm finishes mid-interval.
            waited = 0.0
            while waited < interval and proc.poll() is None:
                time.sleep(0.25)
                waited += 0.25


def print_notes_table(settings: Settings) -> None:
    """Parse the Helm NOTES and print a Service / URL / Credentials table."""
    result = helm(
        settings, "get", "notes", settings.release_name, "--namespace", settings.namespace
    )
    if not result.ok or not result.out.strip():
        return

    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for raw_line in result.out.splitlines():
        line = raw_line.strip()

        available = re.match(r"^(.+) is available at: *$", line)
        if available:
            if current:
                entries.append(current)
            current = {"service": available.group(1), "url": "", "user": "", "pass": ""}
            continue
        if current is None:
            continue

        user = re.match(r"^-\s+username:\s*(.*)$", line)
        if user:
            current["user"] = user.group(1)
            continue
        password = re.match(r"^-\s+password:\s*(.*)$", line)
        if password:
            current["pass"] = password.group(1)
            continue
        if line and line not in ("You're up and running!", "Happy MLOPSing!!! :]"):
            current["url"] = line
    if current:
        entries.append(current)

    if not entries:
        return

    table = Table(title="MLRun CE - Access URLs", title_style="bold", header_style="bold")
    table.add_column("SERVICE")
    table.add_column("URL")
    table.add_column("CREDENTIALS")
    for entry in entries:
        credentials = ""
        if entry["user"] or entry["pass"]:
            credentials = "{} / {}".format(entry["user"], entry["pass"])
        table.add_row(entry["service"], entry["url"], credentials)

    out.print()
    out.print(table)
    out.print()
