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

from .cluster import api_server, effective_context, newest_kind
from .console import cancel, log_info, log_result, log_warn, out
from .settings import NODEPORTS, Settings, confirm, value_changes_file
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


def confirm_target(settings: Settings) -> None:
    """Show where the install is about to land, and ask before the first write.

    A kubeconfig left pointing at the wrong cluster is the costliest mistake an installer
    can make, and nothing else in the run names the cluster. Printed every time (at the
    warning log level, only when it asks); asked only when someone is there to answer and
    something would actually change.
    """
    context = effective_context(settings) or "(none)"
    server = api_server(settings)
    if settings.chart_path:
        chart = f"{settings.chart_path} (local chart)"
    else:
        chart = f"{settings.chart_ref or 'mlrun-ce/mlrun-ce'} {settings.ce_version}".rstrip()
        if settings.ce_version_is_newest or not settings.ce_version:
            chart += f" ({newest_kind(settings)})"
    if settings.values_file and not settings.config_file and not settings.local_registry:
        registry = f"from {settings.values_file}"
    else:
        registry = f"{settings.registry_url} (pull secret '{settings.registry_secret_name}')"

    asks = not (settings.dry_run or settings.non_interactive)
    show = log_result if asks else log_info
    show("Dry run of:" if settings.dry_run else "About to install:")
    show(f"  Cluster:   {context}" + (f" ({server})" if server else ""))
    show(f"  Namespace: {settings.namespace}")
    show(f"  Release:   {settings.release_name}")
    show(f"  Chart:     {chart}")
    show(f"  Registry:  {registry}")
    if settings.external_host_address:
        show(f"  Address:   {settings.external_host_address}")
    moved = [f"{port} ({key})" for key, port in settings.node_ports.items()
             if port != NODEPORTS[key]]  # fmt: skip
    if moved:
        log_warn(f"  NodePorts: {', '.join(moved)}")
    if settings.trigger_migrations:
        log_warn("  Migrations: the MLRun DB migrations Job runs with this upgrade")
    if settings.value_changes:
        log_warn(
            f"  Changes:   {settings.value_changes} value(s) differ from the current release; "
            f"see {value_changes_file(settings)}"
        )

    if not asks:
        return
    if not confirm(settings, "Proceed?"):
        raise cancel("Cancelled; nothing on the cluster was changed.")


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

        # A blank line closes the entry. Without this the final service absorbs every
        # remaining line of the NOTES — the otel section alone put a sentence of prose in
        # TimescaleDB's URL cell.
        if not line:
            entries.append(current)
            current = None
            continue

        user = re.match(r"^-\s+username:\s*(.*)$", line)
        if user:
            current["user"] = user.group(1)
            continue
        password = re.match(r"^-\s+password:\s*(.*)$", line)
        if password:
            current["pass"] = password.group(1)
            continue
        # e.g. "-  S3 credentials: seaweed / seaweed123", which carries both halves at once.
        combined = re.match(r"^-\s+.*credentials:\s*(.*)$", line)
        if combined:
            current["pass"] = ""
            current["user"] = combined.group(1)
            continue
        # First non-empty line only. Assigning unconditionally let the trailing "-  ..."
        # detail lines overwrite the address they belong to.
        if not current["url"] and line not in ("You're up and running!", "Happy MLOPSing!!! :]"):
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
        if entry["user"] and entry["pass"]:
            credentials = f"{entry['user']} / {entry['pass']}"
        elif entry["user"] or entry["pass"]:
            # A combined "user / pass" line, or a service that only names one of the two;
            # either way "postgres / " with a dangling separator reads as a bug.
            credentials = entry["user"] or entry["pass"]
        table.add_row(entry["service"], entry["url"], credentials)

    out.print()
    out.print(table)
    out.print()
