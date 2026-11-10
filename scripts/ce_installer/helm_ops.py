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
"""Composing and running the helm install/uninstall, plus the hard clean."""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

import yaml

from .console import (
    InstallerError,
    cancel,
    debug_log_path,
    die,
    err,
    log_debug,
    log_error,
    log_info,
    log_warn,
    out,
)
from .registry import MANAGED_BY_LABEL, hosts_file_ip
from .secret_data import redact_secret_data
from .settings import (
    NODEPORTS,
    Settings,
    confirm,
    previous_values_file,
    value_changes_file,
)
from .shell import helm, helm_cmd, kubectl, kubectl_cmd, stream
from .ui import print_notes_table, run_deployment_progress_ui

README_URL = "https://github.com/mlrun/ce/blob/development/scripts/README.md"
# Headings in scripts/README.md; test_every_readme_anchor_the_installer_prints_exists
# fails if one is renamed.
TROUBLESHOOTING_ANCHOR = "#troubleshooting"
DRY_RUN_CRDS_ANCHOR = "#--dry-run-fails-with-no-matches-for-kind"
STRIMZI_LEFTOVERS_ANCHOR = "#a-hard-clean-left-a-kafka-pod-and-its-pvc"


def build_set_flags(settings: Settings) -> List[str]:
    """Compose the --set flags. Order is part of the contract the differential tests pin."""
    flags: List[str] = []

    # Pure -f-only mode (no --config) stays fully self-contained — the run never resolved
    # REGISTRY_URL/EXTERNAL_HOST_ADDRESS in that case — unless --local-registry names the
    # registry. Every other mode (--config alone, or -f + --config together) sets these as
    # --set, which helm applies after --values, so a config-resolved value always wins over
    # the same key in a -f file.
    if settings.local_registry or not settings.values_file or settings.config_file:
        flags += ["--set", f"global.registry.url={settings.registry_url}"]
        flags += ["--set", f"global.registry.secretName={settings.registry_secret_name}"]
        flags += [
            "--set",
            f"global.externalHostAddress={settings.external_host_address}",
        ]

    if settings.mlrun_version:
        flags += ["--set", f"mlrun.api.image.tag={settings.mlrun_version}"]
        flags += ["--set", f"mlrun.ui.image.tag={settings.mlrun_version}"]
        flags += [
            "--set",
            f"mlrun.api.sidecars.logCollector.image.tag={settings.mlrun_version}",
        ]
    if settings.nuclio_version:
        flags += ["--set", f"nuclio.controller.image.tag={settings.nuclio_version}"]
        flags += ["--set", f"nuclio.dashboard.image.tag={settings.nuclio_version}"]

    if settings.disable_system_monitoring:
        flags += ["--set", "kube-prometheus-stack.enabled=false"]
    if settings.disable_spark:
        flags += ["--set", "spark-operator.enabled=false"]
    if settings.disable_mpi:
        flags += ["--set", "mpi-operator.deployment.create=false"]
        flags += ["--set", "mpi-operator.crd.create=false"]
        # The subchart splits RBAC in two and has no `rbac.create` at all, so the single
        # key bash used was silently doing nothing and --disable-mpi still created the
        # operator's ServiceAccount and RoleBinding. namespaced.create is the one that
        # defaults on; clusterResources.create is set for completeness.
        flags += ["--set", "mpi-operator.rbac.clusterResources.create=false"]
        flags += ["--set", "mpi-operator.rbac.namespaced.create=false"]
    if settings.disable_model_monitoring:
        flags += ["--set", "strimzi-kafka-operator.enabled=false"]
        flags += ["--set", "kafka.enabled=false"]
        flags += ["--set", "timescaledb.enabled=false"]

    if settings.enable_ingress:
        flags += ingress_set_flags(settings)

    # Given on the command line, the OTel selection is the whole state, `off` included, so
    # every key is written: emitting only the trues let a -f file keep what the CLI turned off.
    for key, enabled in (
        ("opentelemetry-operator.enabled", settings.enable_otel_operator),
        ("opentelemetry.collector.enabled", settings.enable_otel_collector),
        ("opentelemetry.namespaceLabel.enabled", settings.enable_otel_namespace_label),
        ("opentelemetry.instrumentation.enabled", settings.enable_otel_instrumentation),
    ):
        if enabled or settings.otel_set_by_cli:
            flags += ["--set", f"{key}={str(enabled).lower()}"]

    if settings.local_registry:
        # registry:2 serves plain HTTP, through the ingress and on its ClusterIP alike, so
        # the builder has to be told not to try TLS in either mode.
        #
        # The builder is nuclio's dashboard, not mlrun — values.yaml sets
        # nuclio.dashboard.containerBuilderKind: kaniko, and dashboard.yaml reads these two
        # keys. bash set `mlrun.api.kaniko.insecureRegistry`, which no chart has ever read;
        # helm accepts an unknown --set path silently, so the flag looked present and did
        # nothing for as long as it existed.
        #
        # nuclio charts up to 0.22 read only `kaniko.*`; 0.23 moved the keys to `build.*`
        # and still honours `kaniko.*` as a deprecated alias. --ce-version can install
        # either, so both are set.
        flags += ["--set", "nuclio.dashboard.build.insecurePushRegistry=true"]
        flags += ["--set", "nuclio.dashboard.build.insecurePullRegistry=true"]
        flags += ["--set", "nuclio.dashboard.kaniko.insecurePushRegistry=true"]
        flags += ["--set", "nuclio.dashboard.kaniko.insecurePullRegistry=true"]

    for key in NODEPORTS:
        if key in settings.node_ports:
            flags += ["--set", f"{key}={settings.node_ports[key]}"]

    # Off in the saved file, so a later plain `helm upgrade -f` with it does not run the
    # migrations again; migration_set_flags turns them on for the one run that asks.
    flags += ["--set", "mlrunMigrations.enabled=false"]

    return flags


def migration_set_flags(settings: Settings) -> List[str]:
    """The --set that runs the MLRun DB migrations Job on this upgrade only."""
    if not settings.trigger_migrations:
        return []
    return ["--set", "mlrunMigrations.enabled=true"]


def ingress_set_flags(settings: Settings) -> List[str]:
    """Enable all four ingresses on one class, each on a host under the external address.

    Every subchart ships a placeholder host (`chart-example.local`, `nuclio.local`), so
    enabling an Ingress without a host produces a rule nothing will ever match, and the
    chart's NOTES then print the placeholder as the access URL. The class has to reach all
    four too: set on Jupyter alone, the other three fall to the cluster's default class,
    which may not be the controller the preflight just confirmed.

    mlrun's hosts are `{host, paths}` objects. `--set hosts[0].host=...` replaces the whole
    default list rather than merging into it, so `paths` is restated or the rule has none.
    nuclio's hosts are plain strings, and Jupyter's template hardcodes its own path.

    --set wins over --values, so a host or class the user wrote in their -f file is left
    out here rather than silently overridden.
    """
    host = settings.external_host_address
    ingress_class = settings.ingress_class
    user_values = _values_file_contents(settings)
    flags: List[str] = []
    for component, subdomain in (
        ("jupyterNotebook", "jupyter"),
        ("nuclio.dashboard", "nuclio"),
        ("mlrun.ui", "mlrun"),
        ("mlrun.api", "mlrun-api"),
    ):
        prefix = f"{component}.ingress"
        user_ingress = _lookup(user_values, prefix)
        user_ingress = user_ingress if isinstance(user_ingress, dict) else {}
        flags += ["--set", f"{prefix}.enabled=true"]
        if not user_ingress.get("ingressClassName"):
            flags += ["--set", f"{prefix}.ingressClassName={ingress_class}"]
        if user_ingress.get("hosts"):
            continue
        if component == "nuclio.dashboard":
            flags += ["--set", f"{prefix}.hosts[0]={subdomain}.{host}"]
            continue
        flags += ["--set", f"{prefix}.hosts[0].host={subdomain}.{host}"]
        if component.startswith("mlrun."):
            flags += [
                "--set", f"{prefix}.hosts[0].paths[0].path=/",
                "--set", f"{prefix}.hosts[0].paths[0].pathType=ImplementationSpecific",
            ]  # fmt: skip
    return flags


_INDEXED = re.compile(r"^(?P<key>[^\[]+)\[(?P<index>\d+)\]$")


def _strval(raw: str) -> object:
    """Type a --set value the way helm's strvals parser does, for the cases emitted here."""
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return raw


def set_flags_to_values(flags: List[str]) -> dict:
    """Fold `--set a.b[0].c=v` pairs into the nested dict a values file would carry.

    A list index replaces the chart's default list just as the --set did, because helm
    replaces lists from a values file wholesale too.
    """
    values: dict = {}
    for flag in flags[1::2]:
        path, _, raw = flag.partition("=")
        node: object = values
        parts = path.split(".")
        for position, part in enumerate(parts):
            last = position == len(parts) - 1
            indexed = _INDEXED.match(part)
            key = indexed.group("key") if indexed else part
            if not indexed:
                if last:
                    node[key] = _strval(raw)  # type: ignore[index]
                else:
                    node = node.setdefault(key, {})  # type: ignore[union-attr]
                continue
            items = node.setdefault(key, [])  # type: ignore[union-attr]
            index = int(indexed.group("index"))
            while len(items) <= index:
                items.append(None if last else {})
            if last:
                items[index] = _strval(raw)
            else:
                node = items[index]
    return values


GENERATED_VALUES_HEADER = """\
# Written by the MLRun CE installer: every value it resolved for this release, and no
# secrets (the registry password lives only in the pull Secret). Rewritten on every run,
# --dry-run included. A plain `helm upgrade` that omits this file drops these values.
"""


def refuse_values_file_collision(settings: Settings) -> None:
    """Stop when -f names the file the installer writes its own values into.

    The installer rewrites that file on every run, so the user's values would be replaced
    before helm read them, and the file they wrote would be gone.
    """
    if not settings.values_file:
        return
    generated = Path(settings.generated_values_file).resolve()
    if Path(settings.values_file).resolve() != generated:
        return
    raise die(
        f"-f {settings.values_file} is the file the installer writes its own values to "
        f"({generated}), and would be overwritten. Rename your file, or set "
        "GENERATED_VALUES_FILE to another path."
    )


def installer_values_text(settings: Settings) -> str:
    """The installer values file's content, or "" when there is nothing to write."""
    values = set_flags_to_values(build_set_flags(settings))
    if not values:
        return ""
    return GENERATED_VALUES_HEADER + yaml.safe_dump(values, sort_keys=False)


def settle_existing_installer_values(settings: Settings) -> None:
    """Ask before replacing an installer values file this run would change.

    It is rewritten every run, but a user may have edited it, or kept it for a plain helm
    upgrade. Read-only until confirm_target, so a "no" leaves everything as it was. Without
    a prompt the old file is kept beside it as .bak.
    """
    path = Path(settings.generated_values_file).resolve()
    if not path.is_file() or path.read_text() == installer_values_text(settings):
        return
    if not settings.non_interactive:
        if not confirm(settings, f"{path} exists from an earlier run and will change. Overwrite?"):
            raise cancel(
                f"Kept {path}; nothing on the cluster was changed. Move it, or set "
                "GENERATED_VALUES_FILE to another path."
            )
        return
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    log_warn(f"{path} exists from an earlier run and will change; the old one is kept as {backup}.")


def write_installer_values(settings: Settings) -> str:
    """Write the installer's values to their file and return its path, or "" if none.

    A file rather than --set so a plain helm user can repeat the install exactly: values
    passed as --set live only in the release, and a later `helm upgrade -f` without
    --reuse-values silently resets every one of them to the chart default.
    """
    text = installer_values_text(settings)
    if not text:
        return ""
    refuse_values_file_collision(settings)
    path = Path(settings.generated_values_file).resolve()
    path.write_text(text)
    log_info(f"Installer values written to {path}")
    return str(path)


def report_ingress_hosts(settings: Settings) -> None:
    """Print the /etc/hosts line the Ingress hosts need, as the local registry does.

    `mlrun.192.168.49.2` or `jupyter.host.docker.internal` resolve nowhere by themselves,
    so without this the install succeeds and every URL it prints is dead.
    """
    if not settings.enable_ingress:
        return
    pairs = [flag.split("=", 1) for flag in build_set_flags(settings)[1::2]]
    hosts = [value for key, value in pairs
             if key.endswith(("ingress.hosts[0]", "ingress.hosts[0].host"))]  # fmt: skip
    ip = hosts_file_ip(settings.external_host_address)
    if not hosts or not ip:
        return
    log_warn("The Ingress hosts resolve only once DNS or /etc/hosts knows them. Add:")
    log_warn(f"  {ip}  {' '.join(hosts)}")


def report_unresolved_host(settings: Settings) -> None:
    """Docker Desktop's pods resolve host.docker.internal; the machine running the browser
    may not, and then every address in the access table is dead."""
    if settings.external_host_address != "host.docker.internal":
        return
    try:
        socket.gethostbyname(settings.external_host_address)
    except OSError:
        log_warn("This machine cannot resolve host.docker.internal, so the access URLs won't open.")
        log_warn('  Add it: echo "127.0.0.1 host.docker.internal" | sudo tee -a /etc/hosts')


def report_dry_run_files(settings: Settings) -> None:
    """Where the dry run left what it produced, to review without the terminal scrollback."""
    generated = str(Path(settings.generated_values_file).resolve())
    files = [
        ("The rendered manifests (Secret values redacted) and every command", debug_log_path()),
        ("The values this run passes to helm", generated),
    ]
    if settings.previous_values is not None:
        files.append(("The current release's values", str(previous_values_file(settings))))
    if settings.value_changes:
        files.append(("The values this upgrade changes", str(value_changes_file(settings))))
    log_info("Review the dry run in these files:")
    for label, path in files:
        if path and Path(path).is_file():
            log_info(f"  {label}: {path}")


def plain_helm_equivalent(settings: Settings, cmd: List[str]) -> List[str]:
    """The commands a user without this installer runs to get the same release."""
    lines = []
    if not settings.chart_path:
        lines.append(shlex.join(["helm", "repo", "add", "mlrun-ce", settings.helm_repo_url]))
    lines.append(shlex.join(arg for arg in cmd if not arg.startswith("--dry-run")))
    return lines


def _values_file_contents(settings: Settings) -> dict:
    if not settings.values_file:
        return {}
    try:
        loaded = yaml.safe_load(Path(settings.values_file).read_text())
    except (OSError, yaml.YAMLError):
        # helm reads the same file next and reports the problem in its own words.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _lookup(values: dict, dotted: str) -> object:
    node: object = values
    for key in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def build_helm_install_command(settings: Settings, installer_values: str = "") -> List[str]:
    """`installer_values` is the file from write_installer_values. It goes after the user's
    -f, so its values win exactly as the --set flags they replaced did."""
    cmd = helm_cmd(
        settings,
        "upgrade",
        "--install",
        settings.release_name,
        settings.chart_ref,
        "--namespace",
        settings.namespace,
        "--wait",
        "--timeout",
        settings.helm_timeout,
    )
    if settings.values_file:
        cmd += ["--values", settings.values_file]
    if installer_values:
        cmd += ["--values", installer_values]
    cmd += migration_set_flags(settings)
    if settings.ce_version and not settings.chart_path:
        cmd += ["--version", settings.ce_version]
    if settings.dry_run:
        # Server mode reads the cluster (API versions, CRDs, lookup(), existing resources)
        # without sending anything to it.
        cmd += ["--dry-run=server"]
    return cmd


PROMETHEUS_CRD = "prometheuses.monitoring.coreos.com"


def dry_run_exclusions(settings: Settings) -> List[str]:
    """The --set flags that keep a first install's server dry run from failing on CRDs.

    The monitoring stack renders Prometheus and PrometheusRule objects whose CRDs the chart
    itself installs, and a server dry run maps every object against the cluster's API
    discovery, so on a cluster that has never had the chart it fails with "no matches for
    kind". An upgrade already has the CRDs and dry-runs the whole chart, and so does a
    cluster that has them from elsewhere. Applied to the dry run only, never written to the
    values file or the printed equivalent.
    """
    if (
        settings.disable_system_monitoring
        or helm(settings, "status", settings.release_name, "--namespace", settings.namespace).ok
        or kubectl(settings, "get", "crd", PROMETHEUS_CRD).ok
    ):
        return []
    log_info(
        "First install on a cluster without the Prometheus CRDs: dry-running without the "
        "monitoring stack, whose CRDs the chart installs itself. The real install includes it."
    )
    return ["--set", "kube-prometheus-stack.enabled=false"]


def _install_with_progress_ui(settings: Settings, cmd: List[str]) -> None:
    """Run helm quietly into a temp log while the progress table owns the terminal."""
    handle, path = tempfile.mkstemp(prefix="mlrun-ce-helm-", suffix=".log")
    os.close(handle)
    log_path = Path(path)
    try:
        with log_path.open("w") as sink:
            proc = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT)
            try:
                run_deployment_progress_ui(settings, proc)
                helm_exit = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                proc.wait()
                raise InstallerError(code=130) from None
        helm_output = log_path.read_text() if log_path.is_file() else ""
        log_debug("--- helm output (progress UI) ---\n" + redact_secret_data(helm_output))
        if helm_exit != 0:
            log_error("Helm upgrade/install failed. Output below:")
            err.print(helm_output.rstrip("\n"))
            print_failure_hints(settings)
            raise InstallerError(code=helm_exit)
    finally:
        log_path.unlink(missing_ok=True)
    # The progress UI leaves the cursor right after it; a blank line separates it from the
    # URL table below.
    out.print()


def print_failure_hints(settings: Settings) -> None:
    """Where to look next. helm's own message names the symptom, rarely the pod behind it."""
    if settings.dry_run:
        # The monitoring stack is left out of a first install's dry run, but other
        # components that ship CRs (OpenTelemetry) can still end it this way.
        log_info(
            "'no matches for kind ... ensure CRDs are installed first' means the cluster does "
            "not have CRDs the chart itself installs; a dry run cannot get past that before "
            f"the first install. See {README_URL}{DRY_RUN_CRDS_ANCHOR}"
        )
        return
    kubectl_prefix = " ".join(kubectl_cmd(settings))
    log_info("To see what did not come up:")
    log_info(f"  {kubectl_prefix} get pods -n {settings.namespace}")
    log_info(f"  {kubectl_prefix} get events -n {settings.namespace} --sort-by=.lastTimestamp")
    log_info(
        f"A timeout (HELM_TIMEOUT={settings.helm_timeout}) is often a slow first image pull; "
        "running the same command again upgrades the release in place."
    )
    log_info(f"Common failures and their fixes: {README_URL}{TROUBLESHOOTING_ANCHOR}")


def helm_install(settings: Settings) -> None:
    """Run helm. The chart source is resolved earlier, in execute(), so that a version that
    is not published stops the run before anything is created."""
    if settings.dry_run:
        log_info("Dry-run mode: rendering chart without deploying...")
    else:
        log_info(f"Installing MLRun CE (release: {settings.release_name})...")

    cmd = build_helm_install_command(settings, write_installer_values(settings))
    log_info("Equivalent plain helm commands:")
    for line in plain_helm_equivalent(settings, cmd):
        log_info(f"  {line}")
    if settings.dry_run:
        cmd += dry_run_exclusions(settings)

    # `out.is_terminal` is part of the condition, not just a guard inside the progress UI:
    # the UI returns immediately off a terminal, but helm is still writing into a temp file
    # that a successful run then deletes, so `--show-progress` in CI used to throw the
    # install output away. Off a terminal there is nothing to animate anyway.
    if settings.show_progress and not settings.dry_run and out.is_terminal:
        _install_with_progress_ui(settings, cmd)
    else:
        helm_exit = stream(cmd)
        if helm_exit != 0:
            log_error(f"Helm installation failed (exit code {helm_exit}).")
            print_failure_hints(settings)
            raise InstallerError(code=helm_exit)

    report_ingress_hosts(settings)
    report_unresolved_host(settings)
    if settings.dry_run:
        log_info("Dry-run complete (no resources deployed).")
        report_dry_run_files(settings)
        return

    log_info("Installation complete.")
    if settings.trigger_migrations:
        log_info(
            "MLRun DB migrations run in a Job of their own; follow it with: kubectl -n "
            f"{settings.namespace} get jobs -l app.kubernetes.io/component=mlrun-migrations"
        )
    if helm(settings, "status", settings.release_name, "--namespace", settings.namespace).ok:
        print_notes_table(settings)


def do_hard_clean(settings: Settings) -> List[str]:
    """Delete the namespace's PVCs and their released PVs; returns the PVCs that remain."""
    # The reads below run under --dry-run too: listing what would be destroyed is the whole
    # point of dry-running a hard clean, and neither `get` touches anything.
    if settings.dry_run:
        log_warn(f"Dry-run: listing what a hard clean would delete in '{settings.namespace}'.")
    log_warn(f"Hard clean: deleting all PVCs in namespace '{settings.namespace}'...")
    result = kubectl(
        settings,
        "get",
        "pvc",
        "--namespace",
        settings.namespace,
        "--no-headers",
        "-o",
        "custom-columns=:metadata.name",
    )
    pvcs = [line.strip() for line in (result.out.splitlines() if result.ok else []) if line.strip()]
    if not pvcs:
        log_info(f"No PVCs found in namespace '{settings.namespace}'.")
    for pvc in pvcs:
        if settings.dry_run:
            log_info(f"  Dry-run: would delete PVC: {pvc}")
            continue
        log_info(f"  Deleting PVC: {pvc}")
        graceful = kubectl(
            settings, "delete", "pvc", pvc, "--namespace", settings.namespace, "--timeout", "60s"
        )
        if not graceful.ok:
            # --wait=false matters: without it kubectl blocks waiting for the object to
            # actually disappear, and --force only skips *graceful* pod deletion, not the
            # wait. A PVC held by a kubernetes.io/pvc-protection finalizer (the orphaned
            # Strimzi Kafka case) would otherwise hang the fallback as long as the primary.
            kubectl(
                settings,
                "delete",
                "pvc",
                pvc,
                "--namespace",
                settings.namespace,
                "--force",
                "--grace-period=0",
                "--wait=false",
            )
    if pvcs and not settings.dry_run:
        # A PV stays Bound until its claim is gone, and the --wait=false fallback returns
        # before that — so the listing below would skip exactly the volumes just orphaned.
        kubectl(
            settings,
            "wait",
            "--for=delete",
            "pvc",
            "--all",
            "--namespace",
            settings.namespace,
            "--timeout",
            "60s",
        )
    leftovers = [] if settings.dry_run or not pvcs else remaining_pvcs(settings)

    log_warn(
        f"Hard clean: deleting released/failed PVs bound to namespace '{settings.namespace}'..."
    )
    result = kubectl(
        settings,
        "get",
        "pv",
        "--no-headers",
        "-o",
        "custom-columns=:metadata.name,:spec.claimRef.namespace,:status.phase",
    )
    # The phase was fetched and then ignored, so this deleted every PV whose claimRef named
    # the namespace — including ones still Bound to a live PVC, which is how a hard clean
    # could take out a volume belonging to something the release does not own. Released and
    # Failed are the phases with nothing left using them.
    pvs = []
    skipped = []
    for line in result.out.splitlines() if result.ok else []:
        fields = line.split()
        if len(fields) < 3 or fields[1] != settings.namespace:
            continue
        if fields[2] in ("Released", "Failed"):
            pvs.append(fields[0])
        else:
            skipped.append(f"{fields[0]} ({fields[2]})")
    if skipped:
        log_info("  Leaving PVs that are not released or failed: " + ", ".join(skipped))
    if not pvs:
        log_info(f"No released or failed PVs found for namespace '{settings.namespace}'.")
    for pv in pvs:
        if settings.dry_run:
            log_info(f"  Dry-run: would delete PV: {pv}")
            continue
        log_info(f"  Deleting PV: {pv}")
        graceful = kubectl(settings, "delete", "pv", pv, "--timeout", "60s")
        if not graceful.ok:
            kubectl(settings, "delete", "pv", pv, "--force", "--grace-period=0", "--wait=false")

    return leftovers


# Namespaced built-in kinds only, so the one `get` cannot fail on a CRD the cluster lacks.
# PVCs are left out: a plain uninstall keeps them on purpose and a hard clean lists every
# PVC itself.
RELEASE_LEFTOVER_KINDS = (
    "jobs,cronjobs,pods,deployments,statefulsets,daemonsets,replicasets,services,"
    "configmaps,secrets,serviceaccounts,roles,rolebindings,ingresses"
)
_LEFTOVER_FIELDS = (
    r"{range .items[*]}{.kind}/{.metadata.name}|{.metadata.annotations.helm\.sh/resource-policy}"
    r'|{.metadata.deletionTimestamp}|{.metadata.ownerReferences[0].kind}{"\n"}{end}'
)


def release_leftovers(settings: Settings) -> Tuple[List[str], List[str]]:
    """(leftovers, kept): objects still carrying the release's instance label.

    Helm only deletes what the last successful revision rendered, so an object from a
    failed upgrade (the migrations Job is named per revision) outlives the uninstall.
    Objects with an owner are skipped, since their owner's deletion is still cascading,
    and so are ones already being deleted. `helm.sh/resource-policy: keep` marks what a
    subchart keeps on purpose (SeaweedFS's credential Secrets).
    """
    result = kubectl(
        settings,
        "get",
        RELEASE_LEFTOVER_KINDS,
        "--namespace",
        settings.namespace,
        "-l",
        f"app.kubernetes.io/instance={settings.release_name}",
        "-o",
        f"jsonpath={_LEFTOVER_FIELDS}",
    )
    leftovers: List[str] = []
    kept: List[str] = []
    for line in result.out.splitlines() if result.ok else []:
        fields = [*line.split("|"), "", "", ""][:4]
        name, policy, deleting, owner = (field.strip() for field in fields)
        if not name or deleting or owner:
            continue
        kind, _, rest = name.partition("/")
        (kept if policy == "keep" else leftovers).append(f"{kind.lower()}/{rest}")
    return leftovers, kept


def report_uninstall_leftovers(settings: Settings, pvcs: List[str]) -> None:
    """Name what is still in the namespace. Reports only; exit 0 all the same, since the
    release is gone and what remains is either kept on purpose or needs a human decision."""
    others, kept = ([], []) if settings.dry_run else release_leftovers(settings)
    namespace = settings.namespace
    if kept:
        log_info(
            f"Kept on purpose (helm.sh/resource-policy: keep, reused by a reinstall): "
            f"{', '.join(kept)}"
        )
    if settings.hard_clean and not pvcs and not others:
        log_info("Hard clean complete.")
        return
    if settings.hard_clean:
        log_warn(f"Hard clean finished with leftovers: {', '.join(pvcs + others)}")
    elif others:
        log_warn(f"Still in '{namespace}' with the release's label: {', '.join(others)}")
    if pvcs:
        log_warn(
            "  A pod still mounting a PVC keeps it Terminating (pvc-protection); orphaned "
            "Strimzi Kafka resources are the usual cause. "
            f"See {README_URL}{STRIMZI_LEFTOVERS_ANCHOR}"
        )
    if others:
        kubectl_prefix = " ".join(kubectl_cmd(settings))
        log_warn(
            "  helm no longer tracks these (typically left by a failed upgrade). "
            f"Delete them with: {kubectl_prefix} delete -n {namespace} {' '.join(others)}"
        )


def remaining_pvcs(settings: Settings) -> List[str]:
    """PVCs still in the namespace, each marked when it is stuck terminating."""
    result = kubectl(
        settings,
        "get",
        "pvc",
        "--namespace",
        settings.namespace,
        "--no-headers",
        "-o",
        "custom-columns=:metadata.name,:metadata.deletionTimestamp",
    )
    leftovers = []
    for line in result.out.splitlines() if result.ok else []:
        fields = line.split()
        if fields:
            terminating = len(fields) > 1 and fields[1] != "<none>"
            leftovers.append(f"{fields[0]} (Terminating)" if terminating else fields[0])
    return leftovers


INSTALLER_RESOURCE_KINDS = "deployment,service,ingress,secret"


def remove_installer_resources(settings: Settings) -> None:
    """Delete what the installer created outside the chart: the local registry and the
    pull secret. helm uninstall cannot see them, so they outlived every uninstall.

    Selected by label, never by name, so a pull secret the user created for --skip-secret
    survives. The registry's PVC is left, like the chart's own PVCs: --hard-clean is the
    step that deletes data.
    """
    listed = kubectl(
        settings,
        "get",
        INSTALLER_RESOURCE_KINDS,
        "--namespace",
        settings.namespace,
        "-l",
        MANAGED_BY_LABEL,
        "--no-headers",
        "-o",
        "name",
    )
    names = [line.strip() for line in listed.out.splitlines() if line.strip()] if listed.ok else []
    if not names:
        return
    if settings.dry_run:
        for name in names:
            log_info(f"  Dry-run: would delete {name}")
        return
    log_info("Removing resources the installer created outside the chart...")
    kubectl(
        settings,
        "delete",
        INSTALLER_RESOURCE_KINDS,
        "--namespace",
        settings.namespace,
        "-l",
        MANAGED_BY_LABEL,
        "--ignore-not-found",
    )


# Every status except uninstalled (kept history, no resources). Helm 4 removed `--all` and
# lists every status by default; Helm 3 without a status flag shows only deployed and
# failed. These flags mean the same on both.
RELEASE_STATUS_FLAGS = ("--deployed", "--failed", "--pending", "--superseded", "--uninstalling")


def other_mlrun_ce_releases(settings: Settings) -> List[str]:
    """`name (chart)` for every mlrun-ce release in the namespace not named RELEASE_NAME.

    A listing that fails is an error, not an empty namespace: both callers guard against
    damaging another release, and an empty answer would switch the guard off.
    """
    args = ["list", "--namespace", settings.namespace, *RELEASE_STATUS_FLAGS, "-o", "json"]
    listed = helm(settings, *args)
    command = " ".join(helm_cmd(settings, *args))
    if not listed.ok:
        detail = (listed.err or listed.out).strip()
        raise die(f"Could not list the releases in '{settings.namespace}' ({command}): {detail}")
    try:
        releases = json.loads(listed.out) if listed.out.strip() else []
    except ValueError:
        raise die(f"'{command}' did not return JSON: {listed.out.strip()[:200]}") from None
    return [
        f"{release.get('name')} ({release.get('chart')})"
        for release in releases
        if re.match(r"mlrun-ce-\d", str(release.get("chart", "")))
        and release.get("name") != settings.release_name
    ]


def refuse_a_second_release(settings: Settings) -> None:
    """Stop when the namespace already holds mlrun-ce under another release name.

    The README installs as `my-mlrun`, the old run.sh as `mlrun`, this installer as
    `mlrun-ce`. A second release collides with the first on the fixed NodePorts and the
    cluster-scoped resources, and helm only finds out halfway through.
    """
    others = other_mlrun_ce_releases(settings)
    if others:
        raise die(
            f"Namespace '{settings.namespace}' already has MLRun CE installed as "
            f"{', '.join(others)}. Set RELEASE_NAME to that name to upgrade it, or uninstall "
            "it first."
        )


def confirm_hard_clean_without_release(settings: Settings) -> None:
    """A hard clean deletes every PVC in the namespace, release or not, so a mistyped
    NAMESPACE would wipe someone else's volumes. With no release to vouch for the namespace,
    make the user name it again — and under --non-interactive, where nobody can, refuse.
    """
    namespace = settings.namespace
    if settings.non_interactive:
        raise die(
            f"--hard-clean refused: release '{settings.release_name}' is not in namespace "
            f"'{namespace}', and a hard clean deletes every PVC there. Check NAMESPACE, or "
            "run interactively to confirm."
        )
    log_warn(f"--hard-clean will delete every PVC in '{namespace}', which has no release.")
    # No brackets: "[mlrun]" reads as a default, and Enter here cancels.
    sys.stderr.write(f"Type '{namespace}' to delete its PVCs (anything else cancels): ")
    sys.stderr.flush()
    try:
        typed = input().strip()
    except EOFError:
        typed = ""
    if typed != namespace:
        raise cancel("Hard clean cancelled; nothing on the cluster was changed.")


def do_uninstall(settings: Settings) -> None:
    if not helm(settings, "status", settings.release_name, "--namespace", settings.namespace).ok:
        log_warn(
            f"Release '{settings.release_name}' not found in namespace "
            f"'{settings.namespace}' (already uninstalled?)."
        )
        # The cleanup label names the installer, not a release, so a mistyped RELEASE_NAME
        # next to a live install would delete that install's registry and pull secret.
        others = other_mlrun_ce_releases(settings)
        if others:
            raise die(
                f"Namespace '{settings.namespace}' has MLRun CE installed as "
                f"{', '.join(others)}, which this uninstall would damage. Set RELEASE_NAME "
                "to that name to uninstall it."
            )
        if settings.hard_clean and not settings.dry_run:
            confirm_hard_clean_without_release(settings)
    elif settings.dry_run:
        # --dry-run used to be read only on the install path, so
        # `--uninstall --hard-clean --dry-run` really removed the release and really
        # deleted every PVC and bound PV. helm uninstall has no --dry-run of its own worth
        # using here, so the report is ours to print.
        log_info(
            f"Dry-run: would uninstall release '{settings.release_name}' "
            f"from namespace '{settings.namespace}'."
        )
    else:
        log_info(
            f"Uninstalling MLRun CE release '{settings.release_name}' "
            f"from namespace '{settings.namespace}'..."
        )
        helm(
            settings,
            "uninstall",
            settings.release_name,
            "--namespace",
            settings.namespace,
            "--timeout",
            settings.helm_timeout,
            check=True,
        )
        log_info("Uninstall complete.")

    remove_installer_resources(settings)

    pvcs = do_hard_clean(settings) if settings.hard_clean else []
    report_uninstall_leftovers(settings, pvcs)
