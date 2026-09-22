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

import os
import subprocess
import tempfile
from pathlib import Path
from typing import List

from .cluster import resolve_chart_source
from .console import InstallerError, die, err, log_error, log_info, log_warn, out
from .settings import Settings
from .shell import check_requirements, helm, helm_cmd, kubectl, stream
from .ui import print_notes_table, run_deployment_progress_ui


def build_set_flags(settings: Settings) -> List[str]:
    """Compose the --set flags. Order is part of the contract the differential tests pin."""
    flags: List[str] = []

    # Pure -f-only mode (no --config) stays fully self-contained — the run never resolved
    # REGISTRY_URL/EXTERNAL_HOST_ADDRESS in that case. Every other mode (--config alone, or
    # -f + --config together) sets these as --set, which helm applies after --values, so a
    # config-resolved value always wins over the same key in a -f file.
    if not settings.values_file or settings.config_file:
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
        flags += ["--set", "mpi-operator.rbac.create=false"]
    if settings.disable_model_monitoring:
        flags += ["--set", "strimzi-kafka-operator.enabled=false"]
        flags += ["--set", "kafka.enabled=false"]
        flags += ["--set", "timescaledb.enabled=false"]

    if settings.enable_ingress:
        flags += ["--set", "jupyterNotebook.ingress.enabled=true"]
        flags += [
            "--set",
            f"jupyterNotebook.ingress.ingressClassName={settings.ingress_class}",
        ]
        flags += ["--set", "nuclio.dashboard.ingress.enabled=true"]
        flags += ["--set", "mlrun.api.ingress.enabled=true"]
        flags += ["--set", "mlrun.ui.ingress.enabled=true"]

    if settings.enable_otel_operator:
        flags += ["--set", "opentelemetry-operator.enabled=true"]
    if settings.enable_otel_collector:
        flags += ["--set", "opentelemetry.collector.enabled=true"]
    if settings.enable_otel_namespace_label:
        flags += ["--set", "opentelemetry.namespaceLabel.enabled=true"]
    if settings.enable_otel_instrumentation:
        flags += ["--set", "opentelemetry.instrumentation.enabled=true"]

    if settings.local_registry:
        # registry:2 serves plain HTTP, through the ingress and on its ClusterIP alike, so
        # kaniko needs this in either mode. bash gated it on --enable-ingress as well and
        # the port copied that, which left `--local-registry` on its own pushing to
        # https://local-registry...:5000 and failing on TLS.
        flags += ["--set", "mlrun.api.kaniko.insecureRegistry=true"]

    return flags


def build_helm_install_command(settings: Settings) -> List[str]:
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
    if settings.ce_version and not settings.chart_path:
        cmd += ["--version", settings.ce_version]
    if settings.dry_run:
        cmd += ["--dry-run=server"]
    cmd += build_set_flags(settings)
    return cmd


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
        if helm_exit != 0:
            log_error("Helm upgrade/install failed. Output below:")
            if log_path.is_file():
                err.print(log_path.read_text().rstrip("\n"))
            else:
                log_warn(f"Helm output log file was already removed: {path}")
            raise InstallerError(code=helm_exit)
    finally:
        log_path.unlink(missing_ok=True)
    # The progress UI leaves the cursor right after it; a blank line separates it from the
    # URL table below.
    out.print()


def helm_install(settings: Settings) -> None:
    resolve_chart_source(settings)

    if settings.dry_run:
        log_info("Dry-run mode: rendering chart without deploying...")
    else:
        log_info(f"Installing MLRun CE (release: {settings.release_name})...")

    cmd = build_helm_install_command(settings)

    # `out.is_terminal` is part of the condition, not just a guard inside the progress UI:
    # the UI returns immediately off a terminal, but helm is still writing into a temp file
    # that a successful run then deletes, so `--show-progress` in CI used to throw the
    # install output away. Off a terminal there is nothing to animate anyway.
    if settings.show_progress and not settings.dry_run and out.is_terminal:
        _install_with_progress_ui(settings, cmd)
    else:
        helm_exit = stream(cmd)
        if helm_exit != 0:
            raise die(f"Helm installation failed (exit code {helm_exit}).", helm_exit)

    if settings.dry_run:
        log_info("Dry-run complete (no resources deployed).")
        return

    log_info("Installation complete.")
    if helm(settings, "status", settings.release_name, "--namespace", settings.namespace).ok:
        print_notes_table(settings)


def do_hard_clean(settings: Settings) -> None:
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
    pvs = []
    for line in result.out.splitlines() if result.ok else []:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == settings.namespace:
            pvs.append(fields[0])
    if not pvs:
        log_info(f"No PVs found for namespace '{settings.namespace}'.")
    for pv in pvs:
        log_info(f"  Deleting PV: {pv}")
        graceful = kubectl(settings, "delete", "pv", pv, "--timeout", "60s")
        if not graceful.ok:
            kubectl(settings, "delete", "pv", pv, "--force", "--grace-period=0", "--wait=false")

    log_info("Hard clean complete.")


def do_uninstall(settings: Settings) -> None:
    check_requirements(settings)

    if not helm(settings, "status", settings.release_name, "--namespace", settings.namespace).ok:
        log_warn(
            f"Release '{settings.release_name}' not found in namespace "
            f"'{settings.namespace}' (already uninstalled?)."
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

    if settings.hard_clean:
        do_hard_clean(settings)
