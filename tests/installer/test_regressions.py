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
"""One named test per bug that reached a user, plus the output formatting the golden suite
structurally cannot see.

The golden suite compares the *calls* the installer makes. Everything here is about what it
computes or prints, which that comparison reports as unchanged no matter how wrong it is.

Each test name ends in the symptom a user would have reported, so a future failure says
what regressed rather than which assertion tripped.
"""

import glob
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ce_installer import (
    cli,
    cluster,
    config,
    console,
    helm_ops,
    registry,
    release_values,
    secret_data,
    shell,
    ui,
    validators,
)
from ce_installer import settings as cli_settings
from ce_installer.config import check_required_non_interactive
from ce_installer.console import Cancelled, InstallerError
from ce_installer.settings import choose, prompt_or_env
from ce_installer.shell import Result

from .conftest import Recorder

# `helm list` in the namespace, holding the release an uninstall test removes: uninstall
# refuses a release that is not MLRun CE.
CE_RELEASE_LISTED = {
    "list --namespace": Result(
        0, json.dumps([{"name": "mlrun-ce", "namespace": "mlrun", "chart": "mlrun-ce-0.11.0"}])
    )
}
CE_RELEASE_DEPLOYED = {"status": Result(0, "deployed"), **CE_RELEASE_LISTED}

# ------------------------------------------------------------------------------------------
# The access-URL table put the wrong text in the URL column
# ------------------------------------------------------------------------------------------

# Trimmed from a real `helm get notes` on the rke2 lab: SeaweedFS carries a trailing detail
# line, and TimescaleDB is last, so the otel section follows it with nothing in between.
LIVE_NOTES = """\
Jupyter UI is available at:
203.0.113.51:30040

SeaweedFS Admin UI is available at:
203.0.113.51:30093
-  S3 credentials: seaweed / seaweed123

TimescaleDB is available at:
203.0.113.51:30110
-  username: postgres
-  database: postgres

OpenTelemetry Operator is enabled!
-  Namespace selector: opentelemetry.io/inject=enabled

These pods receive OTel auto-instrumentation (runtime metrics, traces, HTTP metrics).

Happy MLOPSing!!! :]
"""

# The two defects overlap on LIVE_NOTES — either fix alone keeps that fixture's URLs right,
# so these isolate them. Each one fails if its own fix is reverted, independently.
PLAIN_DETAIL_NOTES = """\
MLRun UI is available at:
203.0.113.51:30060
Sign in with the admin account.
"""

LATER_SECTION_NOTES = """\
TimescaleDB is available at:
203.0.113.51:30110
-  username: postgres

Some other component is enabled!
-  username: not-timescale
"""


def render_table(monkeypatch, settings, notes):
    """Run the real parser over `notes` and return {service: (url, credentials)}."""
    monkeypatch.setattr(ui, "helm", lambda *a, **k: Result(0, notes))

    rows = {}
    real_add_row = ui.Table.add_row

    def capture(self, *cells, **kwargs):
        rows[cells[0]] = (cells[1], cells[2])
        return real_add_row(self, *cells, **kwargs)

    monkeypatch.setattr(ui.Table, "add_row", capture)
    ui.print_notes_table(settings)
    return rows


def test_notes_table_url_column_is_the_address_not_a_trailing_detail_line(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    # Was "-  S3 credentials: seaweed / seaweed123": every line after the header was
    # assigned to url, so the last one won instead of the first.
    assert rows["SeaweedFS Admin UI"][0] == "203.0.113.51:30093"


def test_notes_table_last_service_does_not_absorb_the_rest_of_the_notes(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    # Was a sentence of OpenTelemetry prose: nothing terminated the final entry, so it kept
    # consuming lines to the end of the NOTES.
    assert rows["TimescaleDB"][0] == "203.0.113.51:30110"
    assert "OTel" not in rows["TimescaleDB"][0]


def test_notes_table_url_survives_a_following_detail_line(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, PLAIN_DETAIL_NOTES)
    # Isolates "first non-empty line wins" from the credentials handling: an ordinary prose
    # line after the address used to overwrite it.
    assert rows["MLRun UI"][0] == "203.0.113.51:30060"


def test_notes_table_credentials_do_not_leak_in_from_a_later_section(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LATER_SECTION_NOTES)
    # Isolates the blank-line terminator: without it, an unrelated later section's
    # "-  username:" is attributed to the last service seen.
    assert rows["TimescaleDB"][1] == "postgres"


def test_notes_table_reads_a_combined_credentials_line(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    assert rows["SeaweedFS Admin UI"][1] == "seaweed / seaweed123"


def test_notes_table_omits_the_separator_when_only_a_username_is_known(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    # "postgres / " with a dangling separator reads as a rendering bug to a user.
    assert rows["TimescaleDB"][1] == "postgres"


def test_notes_table_keeps_every_service_it_is_given(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    assert set(rows) == {"Jupyter UI", "SeaweedFS Admin UI", "TimescaleDB"}


# ------------------------------------------------------------------------------------------
# helm_install's --wait had no --timeout, so a slow image pull failed the release
# ------------------------------------------------------------------------------------------


def test_install_pairs_wait_with_an_explicit_timeout(settings):
    settings.chart_ref = "mlrun-ce/mlrun-ce"
    cmd = helm_ops.build_helm_install_command(settings)
    # Bare --wait silently inherits helm's 5m default; one cold 4.2Gi jupyter pull took
    # 5m40s and marked a working release failed.
    assert "--wait" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "960s"


def test_install_timeout_is_overridable(settings):
    settings.chart_ref = "mlrun-ce/mlrun-ce"
    settings.helm_timeout = "1800s"
    cmd = helm_ops.build_helm_install_command(settings)
    assert cmd[cmd.index("--timeout") + 1] == "1800s"


def test_uninstall_honours_the_same_timeout_setting(monkeypatch, settings):
    settings.helm_timeout = "1800s"
    recorder = Recorder(answers=CE_RELEASE_LISTED)
    monkeypatch.setattr(helm_ops, "helm", recorder)
    monkeypatch.setattr(helm_ops, "kubectl", Recorder())

    helm_ops.do_uninstall(settings)

    # The follow-up to the install fix: uninstall kept a literal 960s, so raising
    # HELM_TIMEOUT reached install but not uninstall.
    uninstall = recorder.argv_containing("uninstall mlrun-ce")
    assert uninstall, "expected a helm uninstall call"
    assert "1800s" in uninstall[0]


# ------------------------------------------------------------------------------------------
# do_hard_clean()'s force-delete fallback could hang indefinitely
# ------------------------------------------------------------------------------------------


def test_hard_clean_force_fallbacks_do_not_wait_for_deletion(monkeypatch, settings):
    # Graceful deletes fail so both force fallbacks are reached; a PVC held by a
    # pvc-protection finalizer is the real case (orphaned Strimzi broker).
    def answer(*args, **kwargs):
        argv = list(args[1:])
        joined = " ".join(argv)
        if "--timeout 60s" in joined or ("delete" in joined and "--force" not in joined):
            return Result(1, "timed out waiting for the condition")
        if "get pvc" in joined:
            return Result(0, "data-kafka-stream-kafka-stream-pool-0")
        if "get pv" in joined:
            return Result(0, "pvc-c5d40c2f")
        return Result(0, "")

    recorder = Recorder()

    def dispatch(*args, **kwargs):
        recorder(*args, **kwargs)
        return answer(*args, **kwargs)

    monkeypatch.setattr(helm_ops, "kubectl", dispatch)
    helm_ops.do_hard_clean(settings)

    forced = [c for c in recorder.calls if "--force" in c]
    assert forced, "expected the force-delete fallback to be reached"
    for call in forced:
        # Without --wait=false, kubectl blocks until the object actually disappears;
        # --force only skips graceful pod deletion. One live run sat blocked 18+ hours.
        assert "--wait=false" in call, f"force delete without --wait=false: {call}"


# ------------------------------------------------------------------------------------------
# validate_node_capacity() silently read ephemeral-storage as 0Gi on some clusters
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_ki"),
    [
        ("7922684Ki", 7922684),
        ("1024Mi", 1024 * 1024),
        ("8Gi", 8 * 1024 * 1024),
        ("1Ti", 1024 * 1024 * 1024),
        # The bug: ephemeral-storage is commonly a bare cAdvisor byte count.
        ("56403987978", 56403987978 // 1024),
        ("", None),
        ("not-a-quantity", None),
    ],
)
def test_allocatable_parser_accepts_bare_byte_counts_as_well_as_suffixes(raw, expected_ki):
    assert validators.allocatable_to_ki(raw) == expected_ki


def test_node_capacity_reports_real_ephemeral_storage_not_zero(monkeypatch, settings):
    def answer(*args, **kwargs):
        joined = " ".join(args[1:])
        if "ephemeral-storage" in joined:
            return Result(0, "56403987978")  # bare bytes, ~52Gi
        if "allocatable.memory" in joined:
            return Result(0, "7922684Ki")
        return Result(0, "")

    monkeypatch.setattr(validators, "kubectl", answer)
    messages = []
    monkeypatch.setattr(validators, "log_info", messages.append)
    monkeypatch.setattr(validators, "log_warn", messages.append)

    validators.validate_node_capacity(settings)

    storage = [m for m in messages if "ephemeral storage" in m]
    assert storage, "expected an ephemeral-storage line"
    assert "~0Gi" not in storage[0], f"regressed to the unit-less-quantity bug: {storage[0]}"
    assert "~52Gi" in storage[0]


# ------------------------------------------------------------------------------------------
# deploy_local_registry() ignored --dry-run entirely
# ------------------------------------------------------------------------------------------


def test_local_registry_dry_run_mutates_nothing(monkeypatch, settings):
    settings.dry_run = True
    settings.external_host_address = "localhost"
    recorder = Recorder()
    monkeypatch.setattr(registry, "kubectl", recorder)

    registry.deploy_local_registry(settings)

    # An unguarded apply either aborted the run (namespace absent) or silently deployed a
    # real registry from a run advertised as rendering-only.
    assert not recorder.argv_containing("apply"), "dry run applied something"


def test_local_registry_dry_run_still_resolves_the_url_for_the_rendered_flags(
    monkeypatch, settings
):
    settings.dry_run = True
    settings.external_host_address = "localhost"
    monkeypatch.setattr(registry, "kubectl", Recorder())

    registry.prepare_local_registry(settings)

    # Resolved in the read-only half on purpose: a dry run has to render the same --set
    # flags the real install would.
    assert settings.local_registry_url


@pytest.mark.parametrize(
    ("address", "warned"),
    [("localhost", True), ("127.0.0.1", True), ("host.docker.internal", False)],
)
def test_a_loopback_address_with_an_ingress_registry_is_warned_about(
    monkeypatch, settings, capture_logs, address, warned
):
    # registry.localhost may resolve to the builder pod itself, not to the ingress.
    logs = capture_logs(registry)
    settings.local_registry = True
    settings.enable_ingress = True
    settings.external_host_address = address
    monkeypatch.setattr(registry, "kubectl", Recorder())

    registry.prepare_local_registry(settings)

    assert any("host.docker.internal" in line for line in logs) is warned


def test_local_registry_real_run_still_applies(monkeypatch, settings):
    settings.dry_run = False
    settings.external_host_address = "localhost"
    recorder = Recorder()
    monkeypatch.setattr(registry, "kubectl", recorder)

    registry.deploy_local_registry(settings)

    assert recorder.argv_containing("apply"), "a real run must deploy the registry"


# ------------------------------------------------------------------------------------------
# The local registry adopted a user's own `local-registry` objects
# ------------------------------------------------------------------------------------------

FOREIGN_DEPLOYMENT = {"get deployment local-registry": Result(0, "exists:")}


def test_a_foreign_local_registry_object_stops_the_run_before_any_apply(monkeypatch, settings):
    settings.external_host_address = "localhost"
    rec = Recorder(answers=FOREIGN_DEPLOYMENT)
    monkeypatch.setattr(registry, "kubectl", rec)

    with pytest.raises(InstallerError):
        registry.prepare_local_registry(settings)

    # apply would have labelled the user's Deployment, and --uninstall deletes by label.
    assert not rec.ran("apply"), "a user's object was applied over"


def test_the_installers_own_registry_objects_are_updated_on_a_rerun(monkeypatch, settings):
    settings.external_host_address = "localhost"
    ours = Result(0, f"exists:{registry.MANAGED_BY_VALUE}")
    rec = Recorder(answers={"local-registry -o jsonpath": ours})
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.prepare_local_registry(settings)
    registry.deploy_local_registry(settings)

    assert rec.ran("apply"), "a rerun over our own registry must still apply"


@pytest.mark.parametrize("with_ingress", [False, True])
def test_the_registry_ingress_is_checked_only_when_one_would_be_applied(
    monkeypatch, settings, with_ingress
):
    settings.external_host_address = "localhost"
    settings.enable_ingress = with_ingress
    rec = Recorder()
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.prepare_local_registry(settings)

    assert rec.ran("get ingress local-registry") is with_ingress


def test_a_dry_run_reports_a_foreign_local_registry_object_too(monkeypatch, settings):
    settings.dry_run = True
    settings.external_host_address = "localhost"
    monkeypatch.setattr(registry, "kubectl", Recorder(answers=FOREIGN_DEPLOYMENT))

    # The dry run exists to show what the real one would do, refusal included.
    with pytest.raises(InstallerError):
        registry.prepare_local_registry(settings)


# ------------------------------------------------------------------------------------------
# resolve_external_host()'s autodetect ignored KUBE_CONTEXT / generic fallback
# ------------------------------------------------------------------------------------------


def test_external_host_with_kube_context_ignores_local_machine_heuristics(monkeypatch, settings):
    settings.kube_context = "remote-cluster"
    settings.non_interactive = True

    def answer(*args, **kwargs):
        if "InternalIP" in " ".join(args[1:]):
            return Result(0, "203.0.113.51")
        return Result(0, "docker-desktop")

    monkeypatch.setattr(cluster, "kubectl", answer)
    # Present and working: the point is that it must not be consulted.
    monkeypatch.setattr(cluster.shutil, "which", lambda _: "/usr/local/bin/minikube")
    monkeypatch.setattr(cluster, "run", lambda *a, **k: Result(0, "192.168.49.2"))

    cluster.resolve_external_host(settings)

    # `kubectl config current-context` reports the ambient context regardless of --context,
    # so these heuristics describe the local machine, not the selected cluster. The value
    # flows into --set global.externalHostAddress, so a wrong guess is not cosmetic.
    assert settings.external_host_address == "203.0.113.51"


def test_external_host_without_kube_context_keeps_the_docker_desktop_heuristic(
    monkeypatch, settings
):
    settings.non_interactive = True

    monkeypatch.setattr(cluster, "kubectl", lambda *a, **k: Result(0, "docker-desktop"))
    monkeypatch.setattr(cluster.shutil, "which", lambda _: None)

    cluster.resolve_external_host(settings)

    # The KUBE_CONTEXT fix narrowed when the local heuristics apply; it must not have
    # removed them. host.docker.internal resolves to the host from pods and the terminal
    # alike. (The equivalent bash test, install_tests.bats #87, has been failing since
    # before the port — this is the Python-side guarantee.)
    assert settings.external_host_address == "host.docker.internal"


def test_external_host_generic_cluster_suggests_localhost(monkeypatch, settings):
    settings.non_interactive = True

    # No KUBE_CONTEXT, no minikube, not docker-desktop — kind/k3d and friends.
    monkeypatch.setattr(cluster, "kubectl", lambda *a, **k: Result(0, "kind-kind"))
    monkeypatch.setattr(cluster.shutil, "which", lambda _: None)

    cluster.resolve_external_host(settings)

    # Those tools NodePort-map to localhost; a node-IP lookup is usually unreachable.
    assert settings.external_host_address == "localhost"


# ------------------------------------------------------------------------------------------
# A failing command printed its own argv, credentials included
# ------------------------------------------------------------------------------------------


def test_a_failing_command_does_not_print_a_password_it_was_given():
    # The registry secret no longer travels in argv, so nothing reaches this today. The
    # redaction stays because the failure path prints whatever it was handed, and a CI log
    # outlives the process table by a very long way.
    masked = " ".join(shell.redact(["kubectl", "create", "--docker-password", "supersecret"]))

    assert "supersecret" not in masked
    assert masked == "kubectl create --docker-password <redacted>"


def test_redaction_covers_the_equals_form_too():
    masked = shell.redact(["helm", "--password=supersecret", "install"])

    assert masked == ["helm", "--password=<redacted>", "install"]


def test_redaction_leaves_ordinary_arguments_alone():
    cmd = ["helm", "upgrade", "--install", "mlrun-ce", "--set", "global.registry.url=x"]

    assert shell.redact(cmd) == cmd


def test_a_failed_command_message_is_redacted_end_to_end(monkeypatch):
    monkeypatch.setattr(
        shell.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
    )

    with pytest.raises(InstallerError) as excinfo:
        shell.run(["kubectl", "--docker-password", "supersecret"], check=True)

    assert "supersecret" not in excinfo.value.message


# ------------------------------------------------------------------------------------------
# install.py.lock does not govern the uvx install path
# ------------------------------------------------------------------------------------------


def _declared_dependencies():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    pyproject = (scripts / "pyproject.toml").read_text()
    block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.MULTILINE | re.DOTALL)
    header = re.search(r"^# dependencies = \[(.*?)^# \]", (scripts / "install.py").read_text(),
                       re.MULTILINE | re.DOTALL)  # fmt: skip
    return re.findall(r'"([^"]+)"', block.group(1)), re.findall(r'"([^"]+)"', header.group(1))


def test_every_declared_dependency_has_an_upper_bound():
    # `uvx --from "git+...#subdirectory=scripts"` resolves this list, not install.py.lock,
    # so an unbounded floor lets a release-tagged install pick up a future major of typer
    # or click and behave differently from the one that was tested.
    pyproject, _ = _declared_dependencies()

    assert pyproject, "no dependencies found — did the pyproject layout change?"
    for requirement in pyproject:
        assert "<" in requirement, f"{requirement} has no upper bound"


def test_both_dependency_declarations_agree():
    # The clone path (install.py's PEP 723 header, then its lock) and the uvx/editable path
    # (pyproject.toml) declare the same four packages separately. The header carried no
    # upper bounds while pyproject capped each at the next major, so a regenerated lock
    # could pick a major the uvx path refused.
    pyproject, header = _declared_dependencies()

    assert sorted(header) == sorted(pyproject)


# ------------------------------------------------------------------------------------------
# The documented install commands pin a tag that has to match the chart
# ------------------------------------------------------------------------------------------


def test_the_documented_install_tag_matches_the_chart_version():
    # Every `uvx --from git+...@mlrun-ce-<v>` line in the docs names a tag cut at release
    # from Chart.yaml. Bump the chart without bumping these and the recommended command
    # points at a tag that will never exist, so it fails before the installer starts.
    root = Path(__file__).resolve().parents[2]
    chart_version = re.search(
        r"^version:\s*(\S+)", (root / "charts" / "mlrun-ce" / "Chart.yaml").read_text(), re.M
    ).group(1)

    for relative in ("README.md", "scripts/README.md", "scripts/install.py"):
        pins = re.findall(r"mlrun-ce-(\d\S*?)(?=[\"#\s])", (root / relative).read_text())
        assert pins, f"{relative} no longer pins a tag — did the install example move?"
        for pinned in pins:
            assert pinned == chart_version, (
                f"{relative} pins mlrun-ce-{pinned}, but Chart.yaml is {chart_version}"
            )


# ------------------------------------------------------------------------------------------
# kaniko was told the registry is plain HTTP only in the ingress mode
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("with_ingress", [False, True])
def test_kaniko_is_told_the_local_registry_is_insecure_in_either_mode(settings, with_ingress):
    # registry:2 serves plain HTTP on its ClusterIP just as it does behind the ingress, so
    # gating this on --enable-ingress left `--local-registry` alone pushing to an https://
    # URL and failing on TLS.
    settings.local_registry = True
    settings.enable_ingress = with_ingress

    flags = helm_ops.build_set_flags(settings)
    for block in ("build", "kaniko"):
        assert f"nuclio.dashboard.{block}.insecurePushRegistry=true" in flags
        assert f"nuclio.dashboard.{block}.insecurePullRegistry=true" in flags


def test_mlrun_builds_are_told_the_local_registry_is_insecure_too(settings):
    # MLRun's own builder pod pushes job images: nuclio's keys never reach it, so it tried
    # TLS against the plain-HTTP registry.
    settings.local_registry = True

    flags = helm_ops.build_set_flags(settings)
    for mode in ("PULL", "PUSH"):
        env = f"MLRUN_HTTPDB__BUILDER__INSECURE_{mode}_REGISTRY_MODE"
        assert f"mlrun.api.extraEnvKeyValue.{env}=enabled" in flags


def test_mlrun_insecure_registry_env_reaches_the_chief_and_the_workers(settings):
    chart_dir = Path(__file__).resolve().parents[2] / "charts" / "mlrun-ce"
    if not glob.glob(str(chart_dir / "charts" / "*.tgz")):
        pytest.skip("subcharts not vendored — run `make helm-update-dependencies` first")
    settings.local_registry = True
    flags = helm_ops.build_set_flags(settings)

    rendered = subprocess.run(
        [
            "helm",
            "template",
            "mlrun",
            str(chart_dir),
            *flags,
            "--show-only",
            "charts/mlrun/templates/api-chief-deployment.yaml",
            "--show-only",
            "charts/mlrun/templates/api-worker-deployment.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for mode in ("PULL", "PUSH"):
        env = f"MLRUN_HTTPDB__BUILDER__INSECURE_{mode}_REGISTRY_MODE"
        assert rendered.count(f"name: {env}") == 2, env


def test_the_insecure_registry_flag_names_a_key_a_chart_actually_reads(settings):
    # The bug this pins: bash set `mlrun.api.kaniko.insecureRegistry`, the mlrun chart has
    # never had a kaniko key, and helm accepts an unknown --set path in silence — so the
    # flag read as present and did nothing. nuclio's dashboard is the container builder.
    settings.local_registry = True

    assert not [flag for flag in helm_ops.build_set_flags(settings) if "mlrun.api.kaniko" in flag]


def test_kaniko_is_not_told_anything_when_the_registry_is_not_local(settings):
    settings.local_registry = False

    flags = helm_ops.build_set_flags(settings)
    assert not [flag for flag in flags if "kaniko" in flag or "insecure" in flag]


INGRESS_COMPONENTS = ("jupyterNotebook", "nuclio.dashboard", "mlrun.ui", "mlrun.api")


def ingress_hosts(flags, prefix=""):
    """The host each `<prefix>...ingress.hosts[0]` flag sets, in either chart's shape."""
    pairs = [flag.split("=", 1) for flag in flags if "=" in flag]
    return [value for key, value in pairs
            if key.startswith(prefix) and ".ingress.hosts[0]" in key
            and key.endswith(("hosts[0]", ".host"))]  # fmt: skip


@pytest.mark.parametrize("component", INGRESS_COMPONENTS)
def test_every_ingress_gets_the_class_and_a_host_under_the_external_address(settings, component):
    # Only Jupyter was given the class, and every host was the chart's placeholder
    # (`jupyter.example.com`, `nuclio.mlrun.local`, ...), so three ingresses went to the
    # cluster default class and none answered on the address the user gave.
    settings.enable_ingress = True
    settings.ingress_class = "traefik"
    settings.external_host_address = "10.0.0.5.nip.io"

    flags = helm_ops.build_set_flags(settings)

    assert f"{component}.ingress.ingressClassName=traefik" in flags
    hosts = ingress_hosts(flags, f"{component}.")
    assert len(hosts) == 1 and hosts[0].endswith(".10.0.0.5.nip.io"), hosts


def test_ingress_hosts_are_distinct_so_the_controller_can_route_them(settings):
    settings.enable_ingress = True
    settings.external_host_address = "example.test"

    hosts = ingress_hosts(helm_ops.build_set_flags(settings))
    assert len(hosts) == len(INGRESS_COMPONENTS) == len(set(hosts)), hosts


def test_ingress_hosts_and_class_from_a_values_file_are_kept(settings, tmp_path):
    # --set beats --values, so emitting a host here would silently replace the user's own.
    values = tmp_path / "values.yaml"
    values.write_text(
        "mlrun:\n  ui:\n    ingress:\n      ingressClassName: mine\n"
        "      hosts: [{host: ui.corp.example, paths: [{path: /}]}]\n"
        "nuclio:\n  dashboard:\n    ingress:\n      hosts: [nuclio.corp.example]\n"
    )
    settings.values_file = str(values)
    settings.enable_ingress = True
    settings.external_host_address = "example.test"

    flags = helm_ops.build_set_flags(settings)

    assert ingress_hosts(flags, "mlrun.ui.") == []
    assert ingress_hosts(flags, "nuclio.dashboard.") == []
    assert not [f for f in flags if f.startswith("mlrun.ui.ingress.ingressClassName")]
    assert "mlrun.ui.ingress.enabled=true" in flags
    assert "nuclio.dashboard.ingress.ingressClassName=nginx" in flags
    assert ingress_hosts(flags, "mlrun.api.") == ["mlrun-api.example.test"]
    assert ingress_hosts(flags, "jupyterNotebook.") == ["jupyter.example.test"]


def test_mlrun_ingress_paths_survive_the_host_override(settings):
    # `--set hosts[0].host=` replaces the chart's whole default hosts list, paths included;
    # an mlrun ingress rule with no paths fails the API server's validation.
    settings.enable_ingress = True

    flags = helm_ops.build_set_flags(settings)
    for component in ("mlrun.ui", "mlrun.api"):
        assert f"{component}.ingress.hosts[0].paths[0].path=/" in flags


def deep_merge(base: dict, over: dict) -> dict:
    """Merge `over` onto `base` the way helm layers a parent's values onto a subchart's.

    Recursive, not shallow: the umbrella declares `nuclio.dashboard.ingress` without
    repeating the subchart's `nuclio.dashboard.kaniko`, and a shallow merge would drop the
    half it does not mention.
    """
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ------------------------------------------------------------------------------------------
# --dry-run was ignored on the uninstall path, which really deleted things
# ------------------------------------------------------------------------------------------


def test_a_dry_run_uninstall_reports_instead_of_uninstalling(monkeypatch, settings, recorder):
    # execute() routed to do_uninstall before anything considered dry_run, and neither it
    # nor do_hard_clean read the flag — so this combination really removed the release.
    helm_rec = recorder(answers={"status": Result(0, "STATUS: deployed"), **CE_RELEASE_LISTED})
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", recorder())
    settings.uninstall = True
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not helm_rec.ran("uninstall mlrun-ce")
    assert helm_rec.ran("status"), "the read that reports what would go should still run"


def test_a_dry_run_hard_clean_lists_the_volumes_without_deleting_them(
    monkeypatch, settings, recorder
):
    kube_rec = recorder(
        answers={
            "get pvc": Result(0, "data-mlrun-db-0\n"),
            "get pv": Result(0, "pvc-abc mlrun Released\n"),
        }
    )
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.dry_run = True

    helm_ops.do_hard_clean(settings)

    assert kube_rec.ran("get pvc") and kube_rec.ran("get pv")
    assert not kube_rec.ran("delete")


def _uninstall_without_release(monkeypatch, settings, recorder):
    kube_rec = recorder(answers={"get pvc": Result(0, "someone-elses-data\n")})
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"status": Result(1, "not found")}))
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.uninstall = True
    settings.hard_clean = True
    return kube_rec


def test_a_hard_clean_with_no_release_is_refused_non_interactively(monkeypatch, settings, recorder):
    # A mistyped NAMESPACE finds no release, and the hard clean went on to delete every PVC
    # in whatever namespace that turned out to be.
    kube_rec = _uninstall_without_release(monkeypatch, settings, recorder)
    settings.non_interactive = True

    with pytest.raises(InstallerError):
        helm_ops.do_uninstall(settings)
    assert not kube_rec.ran("delete")


@pytest.mark.parametrize("typed, proceeds", [("mlrun", True), ("mlurn", False), ("", False)])
def test_a_hard_clean_with_no_release_needs_the_namespace_typed_back(
    monkeypatch, settings, recorder, typed, proceeds
):
    kube_rec = _uninstall_without_release(monkeypatch, settings, recorder)
    monkeypatch.setattr("builtins.input", lambda: typed)

    if proceeds:
        helm_ops.do_uninstall(settings)
    else:
        with pytest.raises(InstallerError):
            helm_ops.do_uninstall(settings)
    assert kube_rec.ran("delete pvc someone-elses-data") is proceeds


def test_a_dry_run_hard_clean_with_no_release_still_lists(monkeypatch, settings, recorder):
    kube_rec = _uninstall_without_release(monkeypatch, settings, recorder)
    settings.non_interactive = True
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert kube_rec.ran("get pvc") and not kube_rec.ran("delete")


def test_hard_clean_waits_for_the_claims_before_looking_for_released_volumes(
    monkeypatch, settings, recorder
):
    # The --wait=false fallback returns while the claim still exists, so its PV was still
    # Bound when listed and the hard clean skipped exactly the volume it had just orphaned.
    kube_rec = recorder(answers={"get pvc": Result(0, "data-0\n")})
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)

    helm_ops.do_hard_clean(settings)

    joined = kube_rec.joined
    wait = next(i for i, call in enumerate(joined) if call.startswith("wait --for=delete pvc"))
    assert wait < next(i for i, call in enumerate(joined) if call.startswith("get pv "))


# ------------------------------------------------------------------------------------------
# The local registry and the pull secret outlived every uninstall
# ------------------------------------------------------------------------------------------


def test_uninstall_removes_only_what_the_installer_labelled(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "deployment.apps/x\n")}
    )
    monkeypatch.setattr(helm_ops, "helm", recorder(answers=CE_RELEASE_DEPLOYED))
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)

    helm_ops.do_uninstall(settings)

    [delete] = kube_rec.argv_containing("delete deployment,service,ingress,secret")
    # By label, never by name: a --skip-secret pull secret is the user's, not ours.
    assert delete[delete.index("-l") + 1] == registry.MANAGED_BY_LABEL
    assert "registry-credentials" not in delete


def test_a_dry_run_uninstall_lists_the_installer_resources_without_deleting(
    monkeypatch, settings, recorder
):
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "deployment.apps/x\n")}
    )
    monkeypatch.setattr(helm_ops, "helm", recorder(answers=CE_RELEASE_DEPLOYED))
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not kube_rec.ran("delete")


LIVE_OTHER_RELEASE = json.dumps([{"name": "mlrun-ce", "chart": "mlrun-ce-0.12.1"}])


def test_a_mistyped_release_name_does_not_clean_up_a_live_install(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "secret/registry\n")}
    )
    helm_rec = recorder(
        answers={"status": Result(1, "", "not found"), "list": Result(0, LIVE_OTHER_RELEASE)}
    )
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.release_name = "typo"

    with pytest.raises(InstallerError):
        helm_ops.do_uninstall(settings)

    # The cleanup label names the installer, not a release: going on would delete the live
    # install's pull secret and registry.
    assert not kube_rec.ran("delete")


@pytest.mark.parametrize("hard_clean", [False, True])
def test_a_release_name_that_is_not_mlrun_ce_is_not_uninstalled(
    monkeypatch, settings, recorder, hard_clean
):
    # RELEASE_NAME=metrics next to CE as mlrun-ce passed the status check, removed
    # `metrics`, then deleted CE's labelled registry and pull secret (and with
    # --hard-clean, every PVC in the namespace).
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "secret/registry\n")}
    )
    helm_rec = recorder(
        answers={"status": Result(0, "STATUS: deployed"), "list": Result(0, LIVE_OTHER_RELEASE)}
    )
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.release_name = "metrics"
    settings.hard_clean = hard_clean
    settings.non_interactive = True

    with pytest.raises(InstallerError) as excinfo:
        helm_ops.do_uninstall(settings)

    assert "'metrics' in namespace 'mlrun' is not MLRun CE" in str(excinfo.value)
    assert not helm_rec.ran("uninstall metrics")
    assert not kube_rec.ran("delete")


def test_a_dry_run_uninstall_of_a_release_that_is_not_mlrun_ce_is_refused(
    monkeypatch, settings, recorder
):
    helm_rec = recorder(
        answers={"status": Result(0, "STATUS: deployed"), "list": Result(0, LIVE_OTHER_RELEASE)}
    )
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", recorder())
    settings.release_name = "metrics"
    settings.dry_run = True

    with pytest.raises(InstallerError):
        helm_ops.do_uninstall(settings)


def test_a_release_list_that_fails_stops_the_uninstall(monkeypatch, settings, recorder):
    # An unreadable list is not proof the release is MLRun CE.
    kube_rec = recorder()
    helm_rec = recorder(
        answers={"status": Result(0, "STATUS: deployed"), "list": Result(1, "", "forbidden")}
    )
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)

    with pytest.raises(InstallerError):
        helm_ops.do_uninstall(settings)

    assert not helm_rec.ran("uninstall mlrun-ce")
    assert not kube_rec.ran("delete")


def test_leftovers_are_still_cleaned_when_no_release_remains(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "secret/registry\n")}
    )
    helm_rec = recorder(answers={"status": Result(1, "", "not found"), "list": Result(0, "[]")})
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)

    helm_ops.do_uninstall(settings)

    # A rerun after a half-finished uninstall is how leftovers get removed at all.
    assert kube_rec.ran("delete deployment,service,ingress,secret")


# ------------------------------------------------------------------------------------------
# --enable-otel off / collector did not override a -f file that enabled OTel
# ------------------------------------------------------------------------------------------

OTEL_KEYS = (
    "opentelemetry-operator.enabled",
    "opentelemetry.collector.enabled",
    "opentelemetry.namespaceLabel.enabled",
    "opentelemetry.instrumentation.enabled",
)


def test_otel_set_on_the_cli_writes_every_key_off_included(settings):
    settings.otel_set_by_cli = True
    settings.enable_otel_collector = True

    flags = helm_ops.build_set_flags(settings)

    # `--enable-otel collector` names the whole state; a -f file enabling instrumentation
    # must not survive it, so the falses have to reach helm as well.
    pairs = [flags[i + 1] for i in range(0, len(flags), 2) if flags[i] == "--set"]
    for key in OTEL_KEYS:
        expected = "true" if key == "opentelemetry.collector.enabled" else "false"
        assert f"{key}={expected}" in pairs


def test_otel_not_on_the_cli_leaves_the_values_file_alone(settings):
    settings.otel_set_by_cli = False

    flags = helm_ops.build_set_flags(settings)

    assert not any(key in " ".join(flags) for key in OTEL_KEYS)


# ------------------------------------------------------------------------------------------
# install.py's bootstrap skipped uv when click was missing
# ------------------------------------------------------------------------------------------


def test_the_bootstrap_probes_click_too():
    source = (Path(__file__).resolve().parents[2] / "scripts" / "install.py").read_text()
    probe = source[source.index("def _bootstrap") : source.index("except ImportError")]

    # cli.py imports click directly; a typer that vendors its own would pass a probe without
    # it and then crash on the import instead of re-running under uv.
    assert "import click" in probe


@pytest.mark.parametrize(
    "manifest",
    [registry.LOCAL_REGISTRY_MANIFEST, registry.LOCAL_REGISTRY_INGRESS,
     registry.DOCKER_CONFIG_SECRET],
)  # fmt: skip
def test_everything_the_installer_creates_carries_the_uninstall_label(manifest):
    rendered = manifest.format(
        namespace="mlrun", ingress_class="nginx", host="h", name="n", payload="p"
    )
    key, value = registry.MANAGED_BY_LABEL.split("=")
    for doc in yaml.safe_load_all(rendered):
        assert doc["metadata"]["labels"][key] == value, doc["kind"]


def test_the_local_registry_keeps_its_images_and_is_pinned():
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(
        registry.LOCAL_REGISTRY_MANIFEST.format(namespace="mlrun"))}  # fmt: skip
    pod = docs["Deployment"]["spec"]["template"]["spec"]
    [container] = pod["containers"]
    assert container["image"] != "registry:2" and ":" in container["image"]
    assert container["resources"]["limits"]["memory"]
    [mount] = container["volumeMounts"]
    assert mount["mountPath"] == "/var/lib/registry"
    [volume] = pod["volumes"]
    assert (
        volume["persistentVolumeClaim"]["claimName"]
        == docs["PersistentVolumeClaim"]["metadata"]["name"]
    )
    assert docs["Deployment"]["spec"]["strategy"]["type"] == "Recreate"


# ------------------------------------------------------------------------------------------
# Blocking validators ran after the namespace, registry and secret already existed
# ------------------------------------------------------------------------------------------


def test_a_blocking_validator_failure_leaves_the_cluster_untouched(monkeypatch, settings):
    mutated = []
    for step in ("ensure_namespace", "deploy_local_registry", "apply_registry_secret"):
        monkeypatch.setattr(cli, step, lambda s, step=step: mutated.append(step))
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "resolve_external_host", lambda s: None)
    monkeypatch.setattr(cli, "check_required_non_interactive", lambda s: None)
    monkeypatch.setattr(cli, "refuse_a_second_release", lambda s: None)

    def fail(s):
        mutated.append("validated")
        raise InstallerError(code=1)

    monkeypatch.setattr(cli, "run_blocking_validators", fail)
    settings.local_registry = True

    with pytest.raises(InstallerError):
        cli.execute(settings)
    assert mutated == ["validated"]


# ------------------------------------------------------------------------------------------
# --non-interactive was unusable on the documented CI path
# ------------------------------------------------------------------------------------------


def test_a_registry_with_no_email_does_not_stop_a_non_interactive_run(monkeypatch, settings):
    # Registries stopped requiring an email years ago, but prompt_or_env treated "empty and
    # no default" as fatal, so a CI run with a complete set of credentials still died here.
    monkeypatch.delenv("REGISTRY_EMAIL", raising=False)
    settings.non_interactive = True
    settings.config_registry_email = ""

    assert (
        prompt_or_env(settings, "REGISTRY_EMAIL", "Docker registry email", "", allow_empty=True)
        == ""
    )


def test_a_genuinely_required_value_still_stops_a_non_interactive_run(monkeypatch, settings):
    monkeypatch.delenv("REGISTRY_URL", raising=False)
    settings.non_interactive = True

    with pytest.raises(InstallerError):
        prompt_or_env(settings, "REGISTRY_URL", "Docker registry URL")


def test_a_non_interactive_uninstall_does_not_demand_registry_credentials(
    monkeypatch, settings, recorder
):
    # check_required_non_interactive ran from inside load_config, ahead of the uninstall
    # branch, so tearing a release down asked for a registry URL and a password.
    for var in ("REGISTRY_URL", "REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(helm_ops, "helm", recorder(answers=CE_RELEASE_DEPLOYED))
    monkeypatch.setattr(helm_ops, "kubectl", recorder())
    settings.non_interactive = True
    settings.uninstall = True
    settings.dry_run = True

    cli.execute(settings)  # the regression was a SystemExit out of this call


def test_missing_credentials_are_reported_up_front_without_a_config_file(
    monkeypatch, settings, capture_logs
):
    # The check lived at the end of load_config, which returns early when there is no
    # --config — so an env-var-driven CI run got no up-front check and instead failed
    # later, one missing variable at a time, from inside prompt_or_env.
    for var in ("REGISTRY_URL", "REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    settings.non_interactive = True
    settings.config_file = ""

    logs = capture_logs(config)
    with pytest.raises(InstallerError):
        check_required_non_interactive(settings)

    text = "\n".join(logs)
    assert "REGISTRY_URL" in text and "REGISTRY_PASSWORD" in text, (
        "every missing field should be named in one report, not one exit per variable"
    )


# ------------------------------------------------------------------------------------------
# A Service with no ports hid every NodePort conflict behind it
# ------------------------------------------------------------------------------------------


def test_a_service_with_no_ports_does_not_hide_a_later_nodeport_conflict(
    monkeypatch, settings, recorder, capture_logs
):
    # The newline used to sit inside the ports range, so the portless Service below emitted
    # no line terminator and the next Service's "default/" prefix was read as its port.
    listing = "kube-system/dns/ \ndefault/web/ 30040 \n"
    monkeypatch.setattr(validators, "kubectl", recorder(answers={"get svc": Result(0, listing)}))
    logs = capture_logs(validators)

    validators.resolve_nodeport_conflicts(settings)

    assert any("30040" in line for line in logs), (
        "a conflict on a chart NodePort should be reported, not swallowed"
    )


def test_a_nodeport_owned_by_this_release_is_not_a_conflict(
    monkeypatch, settings, recorder, capture_logs
):
    listing = f"{settings.namespace}/jupyter/{settings.release_name} 30040 30050 \n"
    monkeypatch.setattr(validators, "kubectl", recorder(answers={"get svc": Result(0, listing)}))
    logs = capture_logs(validators)

    validators.resolve_nodeport_conflicts(settings)

    assert any("no conflicts" in line for line in logs)


# ------------------------------------------------------------------------------------------
# Things that happened in the wrong order, or that a dry run should not have done
# ------------------------------------------------------------------------------------------


def test_a_bad_chart_path_is_caught_before_the_namespace_is_created(monkeypatch, settings):
    # resolve_chart_source validates the path, but used to run only after ensure_namespace
    # and deploy_local_registry — so a typo left a namespace and a running registry behind.
    settings.chart_path = "/no/such/chart"

    with pytest.raises(InstallerError):
        cluster.validate_chart_path(settings)


def test_a_directory_without_a_chart_yaml_is_rejected(settings, tmp_path):
    settings.chart_path = str(tmp_path)

    with pytest.raises(InstallerError):
        cluster.validate_chart_path(settings)


def test_a_dry_run_does_not_log_in_to_the_registry(monkeypatch, settings, recorder):
    # A successful `docker login` rewrites ~/.docker/config.json, which is the one thing a
    # dry run is promising not to do to the machine it runs on.
    rec = recorder()
    monkeypatch.setattr(validators, "run", rec)
    monkeypatch.setattr(validators, "docker_available", lambda: True)
    settings.dry_run = True
    settings.registry_username_value = "user"
    settings.registry_password_value = "pass"

    validators.validate_registry_auth(settings)

    assert not rec.ran("login"), f"no login may be attempted: {rec.joined}"


# ------------------------------------------------------------------------------------------
# Wrong source, first-match-only, called twice, unfiltered, and written to the user's home
# ------------------------------------------------------------------------------------------


def test_the_reported_kubernetes_version_is_the_api_servers(
    monkeypatch, settings, recorder, capture_logs
):
    # A kubelet may trail the control plane by two minor versions, and .items[0] picks an
    # arbitrary node — so a mid-upgrade cluster reported whichever side it landed on.
    payload = '{"serverVersion": {"gitVersion": "v1.31.4"}}'
    rec = recorder(answers={"version": Result(0, payload)})
    monkeypatch.setattr(validators, "kubectl", rec)
    logs = capture_logs(validators)

    validators.validate_k8s_version(settings)

    assert not rec.ran("get nodes"), "the node list is neither authoritative nor needed here"
    assert any("1.31" in message for message in logs)


def test_every_default_storage_class_is_named_not_just_the_first(
    monkeypatch, settings, recorder, capture_logs
):
    # Two defaults is a misconfiguration Kubernetes accepts and then resolves arbitrarily.
    # Reporting only the first made it look like a healthy single default.
    listing = "fast=true\nslow=true\n"
    monkeypatch.setattr(
        validators, "kubectl", recorder(answers={"get storageclass": Result(0, listing)})
    )
    logs = capture_logs(validators)

    assert validators.validate_storage_class(settings)

    reported = "\n".join(logs)
    assert "fast" in reported and "slow" in reported


def test_minikube_ip_is_asked_once(monkeypatch, settings, recorder):
    rec = recorder(answers={"minikube ip": Result(0, "192.168.49.2")})
    monkeypatch.setattr(cluster, "run", rec)
    monkeypatch.setattr(cluster.shutil, "which", lambda name: "/usr/bin/minikube")
    monkeypatch.setattr(
        cluster, "kubectl", recorder(answers={"current-context": Result(0, "minikube")})
    )
    monkeypatch.setenv("EXTERNAL_HOST_ADDRESS", "")
    settings.non_interactive = True
    settings.kube_context = ""

    cluster.resolve_external_host(settings)

    assert settings.external_host_address == "192.168.49.2"
    assert rec.joined == ["minikube ip -p minikube"]


def test_a_hard_clean_leaves_a_bound_pv_alone(monkeypatch, settings, recorder):
    # The phase was fetched and then ignored, so a PV still Bound to a live PVC — possibly
    # one this release does not own — was deleted along with the released ones.
    rec = recorder(
        answers={
            "get pvc": Result(0, ""),
            "get pv": Result(
                0, f"pv-gone {settings.namespace} Released\npv-live {settings.namespace} Bound\n"
            ),
        }
    )
    monkeypatch.setattr(helm_ops, "kubectl", rec)

    helm_ops.do_hard_clean(settings)

    assert rec.ran("delete pv pv-gone")
    assert not rec.ran("pv-live"), f"a Bound PV must survive: {rec.joined}"


def test_the_users_helm_repository_list_is_not_rewritten(monkeypatch, tmp_path):
    # `helm repo add --force-update mlrun-ce` rebinds the alias in the user's own
    # repositories.yaml, and nothing put it back afterwards. The cache is the same leak one
    # level down: `repo update` writes `mlrun-ce-index.yaml` into the shared cache.
    # Set to "" rather than deleted, so monkeypatch restores what the installer writes.
    monkeypatch.setenv("HELM_REPOSITORY_CONFIG", "")
    monkeypatch.setenv("HELM_REPOSITORY_CACHE", "")

    cluster.isolate_helm_repo_config()

    configured = Path(os.environ["HELM_REPOSITORY_CONFIG"])
    cache = Path(os.environ["HELM_REPOSITORY_CACHE"])
    assert configured != Path.home() / ".config" / "helm" / "repositories.yaml"
    assert not configured.exists(), "helm creates it; the installer only names the location"
    assert cache.parent == configured.parent, "the cache must be isolated alongside the list"


def test_explicit_helm_repository_locations_are_respected(monkeypatch):
    monkeypatch.setenv("HELM_REPOSITORY_CONFIG", "/somewhere/mine.yaml")
    monkeypatch.setenv("HELM_REPOSITORY_CACHE", "/somewhere/cache")

    cluster.isolate_helm_repo_config()

    assert os.environ["HELM_REPOSITORY_CONFIG"] == "/somewhere/mine.yaml"
    assert os.environ["HELM_REPOSITORY_CACHE"] == "/somewhere/cache"


def chart_values_with_subcharts(chart_dir: Path) -> dict:
    """The umbrella's values.yaml with each vendored subchart's own values under its name."""
    merged = yaml.safe_load((chart_dir / "values.yaml").read_text()) or {}
    for tarball in sorted(glob.glob(str(chart_dir / "charts" / "*.tgz"))):
        with tarfile.open(tarball) as archive:
            names = archive.getnames()
            chart_yaml = [n for n in names if re.fullmatch(r"[^/]+/Chart\.yaml", n)]
            values_yaml = [n for n in names if re.fullmatch(r"[^/]+/values\.yaml", n)]
            if not chart_yaml:
                continue
            name = yaml.safe_load(archive.extractfile(chart_yaml[0]).read())["name"]
            sub = (
                yaml.safe_load(archive.extractfile(values_yaml[0]).read() or b"{}")
                if values_yaml
                else {}
            )
            over = merged.get(name)
            merged[name] = deep_merge(sub or {}, over if isinstance(over, dict) else {})
    return merged


def test_every_set_flag_names_a_key_the_chart_declares(settings):
    # helm accepts any --set path, whether or not something reads it, which is how the
    # kaniko flag stayed a no-op. Resolving each path against the chart's own values is
    # the only cheap way to notice; `global.*` is helm's cross-chart namespace and is
    # deliberately not declared anywhere, so it is exempt. So is the old `kaniko.*` pair:
    # nuclio 0.23 no longer declares it but its dashboard template still reads it, and
    # older nuclio charts read nothing else.
    read_but_undeclared = {
        "nuclio.dashboard.kaniko.insecurePushRegistry",
        "nuclio.dashboard.kaniko.insecurePullRegistry",
    }
    chart_dir = Path(__file__).resolve().parents[2] / "charts" / "mlrun-ce"
    if not glob.glob(str(chart_dir / "charts" / "*.tgz")):
        pytest.skip("subcharts not vendored — run `make helm-update-dependencies` first")

    values = chart_values_with_subcharts(chart_dir)
    for field in (
        "local_registry",
        "enable_ingress",
        "disable_system_monitoring",
        "disable_spark",
        "disable_mpi",
        "disable_model_monitoring",
        "enable_otel_operator",
        "enable_otel_collector",
        "enable_otel_namespace_label",
        "enable_otel_instrumentation",
    ):
        setattr(settings, field, True)
    settings.mlrun_version = "1.10.0"
    settings.nuclio_version = "1.15.0"
    settings.node_ports = {key: port + 1 for key, port in cli_settings.NODEPORTS.items()}

    flags = helm_ops.build_set_flags(settings)
    paths = [
        value.split("=", 1)[0]
        for flag, value in zip(flags, flags[1:])
        if flag == "--set"
        and not value.startswith("global.")
        and value.split("=", 1)[0] not in read_but_undeclared
    ]
    assert paths, "no --set paths to check — did build_set_flags change shape?"

    missing = []
    for path in paths:
        node = values
        for part in path.split("."):
            key, _, index = part.partition("[")
            # A map declared empty, like mlrun.api.extraEnvKeyValue, takes any key.
            if node == {} and node is not values:
                break
            if not isinstance(node, dict) or key not in node:
                missing.append(path)
                break
            node = node[key]
            if index:
                position = int(index.rstrip("]"))
                if not isinstance(node, list) or position >= len(node):
                    missing.append(path)
                    break
                node = node[position]
    assert not missing, "--set paths no chart declares: " + ", ".join(missing)


# ------------------------------------------------------------------------------------------
# --show-progress off a terminal threw the helm output away
# ------------------------------------------------------------------------------------------


def test_show_progress_off_a_terminal_falls_back_to_helm_output(monkeypatch, settings):
    # The progress UI returns immediately when stdout is not a tty, but helm was still
    # writing into a temp file that a successful run deleted — so --show-progress in CI
    # produced no install output at all.
    settings.show_progress = True
    # rich exposes is_terminal as a read-only property, so the class is the only seam.
    monkeypatch.setattr(type(helm_ops.out), "is_terminal", property(lambda self: False))
    monkeypatch.setattr(
        helm_ops, "_install_with_progress_ui", lambda s, cmd: pytest.fail("used the progress UI")
    )
    streamed = []
    monkeypatch.setattr(helm_ops, "stream", lambda cmd, **_: streamed.append(cmd) or 0)
    monkeypatch.setattr(helm_ops, "helm", lambda *a, **k: Result(1, ""))

    helm_ops.helm_install(settings)

    assert streamed, "helm was never streamed to the terminal"


# ------------------------------------------------------------------------------------------
# A dry run printed the chart's Secrets, unredacted, to the terminal
# ------------------------------------------------------------------------------------------

SECRET_RENDER = (
    "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: grafana\n"
    "data:\n  admin-password: c3VwZXItc2VjcmV0\n"
)


def test_a_stream_without_stdout_keeps_it_redacted_in_the_log(debug_log, capsys, tmp_path):
    render = tmp_path / "render.yaml"
    render.write_text(SECRET_RENDER)
    script = (
        f"import sys; sys.stdout.write(open({str(render)!r}).read()); "
        "sys.stderr.write('a warning\\n')"
    )
    shell.stream([sys.executable, "-c", script], show_stdout=False)
    console.close_debug_log()

    captured = capsys.readouterr()
    assert "kind: Secret" not in captured.out and "a warning" in captured.err
    text = debug_log.read_text()
    assert "name: grafana" in text and "<redacted>" in text
    assert "c3VwZXItc2VjcmV0" not in text


@pytest.mark.parametrize(("dry_run", "shown"), [(True, False), (False, True)])
def test_only_a_dry_run_keeps_helm_stdout_off_the_terminal(monkeypatch, settings, dry_run, shown):
    settings.dry_run = dry_run
    streamed = []
    monkeypatch.setattr(
        helm_ops, "stream", lambda cmd, show_stdout=True: streamed.append(show_stdout) or 0
    )
    monkeypatch.setattr(helm_ops, "helm", lambda *a, **k: Result(1, ""))
    monkeypatch.setattr(helm_ops, "kubectl", lambda *a, **k: Result(1, ""))

    helm_ops.helm_install(settings)

    assert streamed == [shown]


# ------------------------------------------------------------------------------------------
# The pull secret was deleted before being re-applied
# ------------------------------------------------------------------------------------------


def test_an_existing_pull_secret_is_updated_without_being_deleted_first(
    monkeypatch, settings, recorder
):
    # Left over from the `kubectl create secret` era, which refused to overwrite. `apply`
    # updates in place, so the delete only opened a window with no credentials — and lost
    # them entirely if anything failed before the re-create.
    rec = recorder(answers=OURS)
    monkeypatch.setattr(registry, "kubectl", rec)
    settings.local_registry = True
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"

    _create_secret(settings)

    assert not rec.ran("delete secret")
    assert rec.ran("apply")


# The pull secret already exists and carries the installer's label.
OURS = {"get secret": Result(0, f"exists:{registry.MANAGED_BY_VALUE}")}


class _ImmutableOnFirstApply(Recorder):
    """Fails the first `apply` the way the API server rejects a Secret type change."""

    def __call__(self, *args, **kwargs):
        result = super().__call__(*args, **kwargs)
        if self.calls[-1][:1] == ["apply"] and len(self.argv_containing("apply")) == 1:
            return Result(1, "", IMMUTABLE_TYPE_ERROR)
        return result


IMMUTABLE_TYPE_ERROR = (
    'The Secret "registry-credentials" is invalid: type: Invalid value: '
    '"kubernetes.io/dockerconfigjson": field is immutable'
)


def _create_secret(settings):
    if registry.prepare_registry_secret(settings):
        registry.apply_registry_secret(settings)


def _secret_settings(settings):
    settings.local_registry = True
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"
    return settings


def test_a_failed_secret_update_does_not_delete_the_existing_secret(monkeypatch, settings):
    # Any apply failure used to be read as "type is immutable", so a Forbidden, a conflict
    # or an API timeout deleted working credentials — and the retry then failed the same way.
    rec = Recorder(
        answers={**OURS, "apply": Result(1, "", 'secrets "registry-credentials" is forbidden')}
    )
    monkeypatch.setattr(registry, "kubectl", rec)

    with pytest.raises(InstallerError):
        _create_secret(_secret_settings(settings))

    assert not rec.ran("delete"), f"a working secret must survive: {rec.joined}"


def test_a_secret_that_cannot_be_deleted_is_not_reapplied(monkeypatch, settings):
    rec = _ImmutableOnFirstApply(answers={**OURS, "delete": Result(1, "", "forbidden")})
    monkeypatch.setattr(registry, "kubectl", rec)

    with pytest.raises(InstallerError):
        _create_secret(_secret_settings(settings))

    assert len(rec.argv_containing("apply")) == 1, "no retry once the delete has failed"


def test_a_secret_that_cannot_be_updated_in_place_is_replaced(monkeypatch, settings):
    # A name already taken by a Secret of another type cannot be applied over, because
    # `type` is immutable — that case, and only that case, still warrants the delete.
    rec = _ImmutableOnFirstApply(answers=OURS)
    monkeypatch.setattr(registry, "kubectl", rec)
    settings.local_registry = True
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"

    _create_secret(settings)

    assert rec.ran("delete secret")
    assert len(rec.argv_containing("apply")) == 2


# ------------------------------------------------------------------------------------------
# The managed-by label adopted a pull secret the user created by hand
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("local", [True, False])
def test_a_users_own_pull_secret_is_neither_labelled_nor_changed(
    monkeypatch, settings, recorder, capture_logs, local
):
    # apply added the label to an existing secret, and --uninstall deletes by that label:
    # a secret created by hand per the README was taken over on install and deleted on
    # uninstall.
    for name, value in (("REGISTRY_USERNAME", "u"), ("REGISTRY_PASSWORD", "p")):
        monkeypatch.setenv(name, value)
    logs = capture_logs(registry)
    rec = recorder(answers={"get secret": Result(0, "exists:")})
    monkeypatch.setattr(registry, "kubectl", rec)
    settings.non_interactive = True
    settings.local_registry = local
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"

    _create_secret(settings)

    assert not rec.ran("apply") and not rec.ran("delete"), rec.joined
    assert "was not created by this installer" in "\n".join(logs)


def test_a_missing_pull_secret_is_created_with_the_label(monkeypatch, settings, recorder):
    rec = recorder(answers={"get secret": Result(1, "", "NotFound")})
    monkeypatch.setattr(registry, "kubectl", rec)

    _create_secret(_secret_settings(settings))

    [manifest] = [rec.inputs[i] for i, c in enumerate(rec.calls) if c[:1] == ["apply"]]
    assert yaml.safe_load(manifest)["metadata"]["labels"] == {
        registry.MANAGED_BY_KEY: registry.MANAGED_BY_VALUE
    }


# With no --ce-version the summary could not say which chart helm would install
# ------------------------------------------------------------------------------------------


def test_without_ce_version_the_newest_stable_chart_is_pinned(monkeypatch, settings, recorder):
    rec = _search(monkeypatch, recorder, pinned=[], newest="0.12.0")

    cluster.pin_newest_chart_version(settings)

    assert settings.ce_version == "0.12.0" and settings.ce_version_is_newest
    [search] = [c for c in rec.calls if c[:1] == ["search"]]
    assert "--devel" not in search, "an rc is not the newest stable chart"
    cmd = helm_ops.build_helm_install_command(settings)
    assert cmd[cmd.index("--version") + 1] == "0.12.0"


def test_a_failed_search_leaves_the_choice_to_helm(monkeypatch, settings, recorder, capture_logs):
    monkeypatch.setattr(cluster, "helm", recorder(answers={"search": Result(1, "", "boom")}))
    logs = capture_logs(cluster)

    cluster.pin_newest_chart_version(settings)

    assert settings.ce_version == ""
    assert "--version" not in helm_ops.build_helm_install_command(settings)
    assert any("newest stable" in line for line in logs)


def test_devel_lets_a_prerelease_be_the_newest(monkeypatch, settings, recorder):
    rec = _search(monkeypatch, recorder, pinned=[], newest="0.12.1-rc.8")
    settings.devel = True

    cluster.pin_newest_chart_version(settings)

    [search] = [c for c in rec.calls if c[:1] == ["search"]]
    assert "--devel" in search
    assert settings.ce_version == "0.12.1-rc.8"


def test_devel_reaches_helm_when_the_search_cannot_pin(monkeypatch, settings, recorder):
    # Unpinned, helm would otherwise pick the newest stable chart.
    monkeypatch.setattr(cluster, "helm", recorder(answers={"search": Result(1, "", "boom")}))
    settings.devel = True

    cluster.pin_newest_chart_version(settings)

    assert "--devel" in helm_ops.build_helm_install_command(settings)


@pytest.mark.parametrize("field, value", [("ce_version", "0.11.0"), ("chart_path", "/c")])
def test_devel_is_ignored_when_the_chart_is_named(
    monkeypatch, settings, recorder, capture_logs, field, value
):
    monkeypatch.setattr(cluster, "isolate_helm_repo_config", lambda: None)
    monkeypatch.setattr(cluster, "validate_chart_path", lambda s: None)
    monkeypatch.setattr(cluster, "chart_deps_satisfied", lambda d: True)
    _search(monkeypatch, recorder, pinned=[{"name": "mlrun-ce/mlrun-ce", "version": "0.11.0"}])
    logs = capture_logs(cluster)
    settings.devel = True
    setattr(settings, field, value)

    cluster.resolve_chart_source(settings)

    assert any("--devel is ignored" in line for line in logs)
    assert "--devel" not in helm_ops.build_helm_install_command(settings)


@pytest.mark.parametrize("field, value", [("ce_version", "0.11.0"), ("chart_path", "/c")])
def test_an_explicit_chart_choice_is_not_overridden(monkeypatch, settings, recorder, field, value):
    monkeypatch.setattr(cluster, "isolate_helm_repo_config", lambda: None)
    monkeypatch.setattr(cluster, "validate_chart_path", lambda s: None)
    monkeypatch.setattr(cluster, "chart_deps_satisfied", lambda d: True)
    _search(monkeypatch, recorder, pinned=[{"name": "mlrun-ce/mlrun-ce", "version": "0.11.0"}])
    setattr(settings, field, value)

    cluster.resolve_chart_source(settings)

    assert settings.ce_version == ("0.11.0" if field == "ce_version" else "")
    assert not settings.ce_version_is_newest


# ------------------------------------------------------------------------------------------
# Values passed only as --set were lost by the next plain `helm upgrade`
# ------------------------------------------------------------------------------------------


def test_set_flags_fold_into_the_values_helm_would_have_parsed():
    flags = [
        "--set", "a.enabled=true",
        "--set", "a.off=false",
        "--set", "a.port=30010",
        "--set", "a.url=registry:5000",
        "--set", "a.hosts[0]=nuclio.example",
        "--set", "b.hosts[0].host=mlrun.example",
        "--set", "b.hosts[0].paths[0].path=/",
    ]  # fmt: skip

    assert helm_ops.set_flags_to_values(flags) == {
        "a": {"enabled": True, "off": False, "port": 30010, "url": "registry:5000",
              "hosts": ["nuclio.example"]},
        "b": {"hosts": [{"host": "mlrun.example", "paths": [{"path": "/"}]}]},
    }  # fmt: skip


def test_installer_values_go_to_helm_as_a_file_after_the_users_own(settings, tmp_path):
    settings.values_file = "mine.yaml"
    settings.config_file = "ce-config.yaml"  # -f alone emits no registry values at all
    settings.generated_values_file = str(tmp_path / "out.yaml")
    settings.enable_ingress = True

    path = helm_ops.write_installer_values(settings)
    cmd = helm_ops.build_helm_install_command(settings, path)

    assert "--set" not in cmd
    values = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--values"]
    assert values == ["mine.yaml", path], "the installer's file must win, as --set did"
    written = yaml.safe_load(Path(path).read_text())
    assert written["global"]["registry"]["url"] == settings.registry_url
    assert written["mlrun"]["api"]["ingress"]["enabled"] is True


def test_the_installer_values_file_carries_no_secret(settings, tmp_path):
    settings.generated_values_file = str(tmp_path / "out.yaml")
    settings.registry_password_value = "hunter2-do-not-write"

    path = helm_ops.write_installer_values(settings)

    assert "hunter2-do-not-write" not in Path(path).read_text()


def test_the_plain_helm_equivalent_is_runnable_as_printed(settings):
    settings.chart_ref = "mlrun-ce/mlrun-ce"
    settings.dry_run = True
    settings.ce_version = "0.12.1"
    cmd = ["helm", "upgrade", "--install", "mlrun-ce", "mlrun-ce/mlrun-ce", "--values",
           "/tmp/my values.yaml", "--version", "0.12.1", "--dry-run=server"]  # fmt: skip

    repo, install = helm_ops.plain_helm_equivalent(settings, cmd)

    assert repo == f"helm repo add mlrun-ce {settings.helm_repo_url}"
    assert "--dry-run" not in install
    assert "'/tmp/my values.yaml'" in install, "paths must survive a copy-paste into a shell"


# ------------------------------------------------------------------------------------------
# A second mlrun-ce release under another name collided with the first
# ------------------------------------------------------------------------------------------


def _helm_list(monkeypatch, recorder, releases):
    monkeypatch.setattr(
        helm_ops, "helm", recorder(answers={"list": Result(0, json.dumps(releases))})
    )


def test_an_mlrun_ce_release_under_another_name_is_refused(monkeypatch, settings, recorder):
    _helm_list(monkeypatch, recorder, [{"name": "my-mlrun", "chart": "mlrun-ce-0.12.0"}])

    with pytest.raises(InstallerError) as excinfo:
        helm_ops.refuse_a_second_release(settings)
    assert "my-mlrun" in str(excinfo.value)


def test_an_mlrun_ce_release_in_another_namespace_is_refused(monkeypatch, settings, recorder):
    # The chart's PriorityClass and NodePorts are cluster-wide: helm fails halfway otherwise.
    release = {"name": "mlrun-ce", "namespace": "team-a", "chart": "mlrun-ce-0.12.0"}
    rec = recorder(answers={"list": Result(0, json.dumps([release]))})
    monkeypatch.setattr(helm_ops, "helm", rec)

    with pytest.raises(InstallerError) as excinfo:
        helm_ops.refuse_a_second_release(settings)

    assert "--all-namespaces" in rec.calls[0]
    assert "mlrun-ce in 'team-a'" in excinfo.value.message
    assert "NAMESPACE" in excinfo.value.message


def test_without_cluster_wide_access_this_namespace_is_still_checked(
    monkeypatch, settings, recorder, capture_logs
):
    forbidden = Result(1, "", "secrets is forbidden: cannot list resource at the cluster scope")
    rec = recorder(answers={"--all-namespaces": forbidden, "list": Result(0, "[]")})
    monkeypatch.setattr(helm_ops, "helm", rec)
    logs = capture_logs(helm_ops)

    helm_ops.refuse_a_second_release(settings)

    assert ["--namespace", settings.namespace] == rec.calls[1][1:3]
    assert any("Checking only" in line for line in logs)


@pytest.mark.parametrize(
    "releases",
    [
        [{"name": "mlrun-ce", "chart": "mlrun-ce-0.12.0"}],
        [{"name": "mlrun-ce", "namespace": "mlrun", "chart": "mlrun-ce-0.12.0"}],
        [{"name": "other", "chart": "mlrun-ce-extras-1.0.0"}],
        [{"name": "redis", "chart": "redis-18.0.0"}],
        [],
    ],
)
def test_upgrades_and_unrelated_releases_pass(monkeypatch, settings, recorder, releases):
    _helm_list(monkeypatch, recorder, releases)

    helm_ops.refuse_a_second_release(settings)


def test_the_release_check_survives_skip_validators(monkeypatch, settings):
    checked = []
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "check_required_non_interactive", lambda s: None)

    def refuse(s):
        checked.append(True)
        raise InstallerError(code=1)

    monkeypatch.setattr(cli, "refuse_a_second_release", refuse)
    settings.skip_validators = True

    with pytest.raises(InstallerError):
        cli.execute(settings)
    assert checked


# ------------------------------------------------------------------------------------------
# Ingress hosts under a bare IP resolved nowhere, and nothing said so
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, ip",
    [
        ("192.168.49.2", "192.168.49.2"),
        ("host.docker.internal", "127.0.0.1"),
        ("localhost", "127.0.0.1"),
        ("192.168.49.2.nip.io", ""),
        ("mlrun.corp.example", "<ingress controller IP>"),
    ],
)
def test_hosts_file_lines_carry_an_ip_never_a_name(host, ip):
    assert registry.hosts_file_ip(host) == ip


def test_the_ingress_hosts_are_printed_for_etc_hosts(settings, capture_logs):
    logs = capture_logs(helm_ops)
    settings.enable_ingress = True
    settings.external_host_address = "192.168.49.2"

    helm_ops.report_ingress_hosts(settings)

    line = next(entry for entry in logs if entry.strip().startswith("192.168.49.2"))
    for sub in ("mlrun", "mlrun-api", "jupyter", "nuclio"):
        assert f"{sub}.192.168.49.2" in line.split()


# ------------------------------------------------------------------------------------------
# -f naming the installer's own values file was overwritten before helm read it
# ------------------------------------------------------------------------------------------


def test_a_values_file_that_is_the_generated_file_is_refused_up_front(
    monkeypatch, settings, tmp_path
):
    generated = tmp_path / "mlrun-ce-values.yaml"
    generated.write_text("mine: true\n")
    settings.values_file = str(generated)
    settings.generated_values_file = str(generated)
    touched = []
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "check_required_non_interactive", lambda s: None)
    monkeypatch.setattr(cli, "refuse_a_second_release", lambda s: touched.append("cluster"))

    with pytest.raises(InstallerError) as excinfo:
        cli.execute(settings)

    assert "GENERATED_VALUES_FILE" in str(excinfo.value)
    assert touched == [], "refused only after querying the release"
    assert generated.read_text() == "mine: true\n", "the user's file was rewritten"


@pytest.mark.parametrize("written", ["previous", "changes"])
def test_a_values_file_that_an_upgrade_writes_is_refused_up_front(
    monkeypatch, settings, tmp_path, written
):
    # Restoring the snapshot with `-f mlrun-ce-values.previous.yaml` had the upgrade
    # overwrite it with the current release's values before reading it.
    settings.generated_values_file = str(tmp_path / "mlrun-ce-values.yaml")
    snapshot = tmp_path / f"mlrun-ce-values.{written}.yaml"
    snapshot.write_text("mine: true\n")
    settings.values_file = str(snapshot)
    touched = []
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "check_required_non_interactive", lambda s: None)
    monkeypatch.setattr(cli, "refuse_a_second_release", lambda s: touched.append("cluster"))
    monkeypatch.setattr(cli, "load_previous_values", lambda s: touched.append("snapshot"))

    with pytest.raises(InstallerError) as excinfo:
        cli.execute(settings)

    assert "Copy it to another name" in str(excinfo.value)
    assert touched == []
    assert snapshot.read_text() == "mine: true\n"


def test_writing_the_installer_values_never_replaces_the_users_file(settings, tmp_path):
    generated = tmp_path / "values.yaml"
    generated.write_text("mine: true\n")
    settings.values_file = str(generated)
    settings.config_file = "ce-config.yaml"
    settings.generated_values_file = str(generated)

    with pytest.raises(InstallerError):
        helm_ops.write_installer_values(settings)
    assert generated.read_text() == "mine: true\n"


# ------------------------------------------------------------------------------------------
# An unpublished chart version failed at the very end, after namespace, registry and secret
# ------------------------------------------------------------------------------------------


def _search(monkeypatch, recorder, pinned, newest="0.12.0"):
    rec = recorder(
        answers={
            "--version": Result(0, json.dumps(pinned)),
            "search": Result(0, json.dumps([{"name": "mlrun-ce/mlrun-ce", "version": newest}])),
        }
    )
    monkeypatch.setattr(cluster, "helm", rec)
    return rec


def test_an_unpublished_chart_version_names_the_newest_stable_one(
    monkeypatch, settings, recorder, capture_logs
):
    _search(monkeypatch, recorder, pinned=[])
    logs = capture_logs(cluster)
    settings.ce_version = "0.12.1-rc.8"

    with pytest.raises(InstallerError):
        cluster.verify_chart_version_published(settings)

    text = "\n".join(logs)
    assert "0.12.1-rc.8 is not published" in text
    assert "--ce-version 0.12.0" in text


def test_a_published_version_passes(monkeypatch, settings, recorder):
    _search(monkeypatch, recorder, pinned=[{"name": "mlrun-ce/mlrun-ce", "version": "0.12.0"}])
    settings.ce_version = "0.12.0"

    cluster.verify_chart_version_published(settings)


def test_a_similarly_named_chart_does_not_count_as_published(monkeypatch, settings, recorder):
    # `helm search repo` matches the keyword as a substring.
    _search(monkeypatch, recorder, pinned=[{"name": "mlrun-ce/mlrun-ce-extras", "version": "1"}])
    settings.ce_version = "1"

    with pytest.raises(InstallerError):
        cluster.verify_chart_version_published(settings)


def test_a_search_that_fails_does_not_block_the_install(
    monkeypatch, settings, recorder, capture_logs
):
    monkeypatch.setattr(cluster, "helm", recorder(answers={"search": Result(1, "", "boom")}))
    logs = capture_logs(cluster)
    settings.ce_version = "0.12.0"

    cluster.verify_chart_version_published(settings)

    assert any("Could not check" in line for line in logs)


# ------------------------------------------------------------------------------------------
# The namespace was created before the prompts, and nothing named the target cluster
# ------------------------------------------------------------------------------------------


def _record_execute_steps(monkeypatch, steps):
    for name in ("check_requirements", "refuse_a_second_release", "run_blocking_validators",
                 "resolve_chart_source", "resolve_external_host", "prepare_local_registry",
                 "prepare_registry_secret", "gather_install_params", "load_previous_values",
                 "run_validators", "confirm_target", "ensure_namespace",
                 "deploy_local_registry", "apply_registry_secret",
                 "helm_install"):  # fmt: skip
        monkeypatch.setattr(cli, name, lambda s, name=name: steps.append(name) or True)
    monkeypatch.setattr(cli, "check_required_non_interactive", lambda s: None)


WRITES = {"ensure_namespace", "deploy_local_registry", "apply_registry_secret", "helm_install"}


def test_migrations_requested_on_a_first_install_are_dropped_with_a_warning(
    monkeypatch, settings, capture_logs
):
    _record_execute_steps(monkeypatch, [])
    monkeypatch.setattr(cli, "report_value_changes", lambda s: None)
    logs = capture_logs(cli)
    settings.trigger_migrations = True

    cli.execute(settings)

    assert settings.trigger_migrations is False
    assert any("no release to upgrade" in line for line in logs)


def test_every_prompt_and_read_comes_before_the_first_write(monkeypatch, settings):
    steps = []
    _record_execute_steps(monkeypatch, steps)
    settings.local_registry = True

    cli.execute(settings)

    first_write = min(steps.index(step) for step in WRITES)
    assert steps.index("confirm_target") == first_write - 1, steps
    for step in ("resolve_chart_source", "prepare_registry_secret", "gather_install_params",
                 "resolve_external_host", "prepare_local_registry",
                 "load_previous_values"):  # fmt: skip
        assert steps.index(step) < first_write, f"{step} ran after a write: {steps}"


@pytest.mark.parametrize("failing", ["resolve_chart_source", "prepare_registry_secret",
                                     "confirm_target"])  # fmt: skip
def test_a_run_stopped_before_the_summary_changes_nothing(monkeypatch, settings, failing):
    steps = []
    _record_execute_steps(monkeypatch, steps)

    def stop(s):
        raise InstallerError(code=1)

    monkeypatch.setattr(cli, failing, stop)

    with pytest.raises(InstallerError):
        cli.execute(settings)
    assert not WRITES & set(steps), steps


def _target(monkeypatch, recorder, settings):
    monkeypatch.setattr(
        cluster,
        "kubectl",
        recorder(
            answers={
                "current-context": Result(0, "prod-east"),
                "cluster.server": Result(0, "https://10.1.2.3:6443"),
            }
        ),
    )
    settings.registry_url = "index.docker.io/me"


def test_the_summary_names_the_cluster_and_asks_before_writing(
    monkeypatch, settings, recorder, capture_logs
):
    _target(monkeypatch, recorder, settings)
    logs = capture_logs(ui)
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda s, q: asked.append(q) or False)

    with pytest.raises(InstallerError):
        ui.confirm_target(settings)

    text = "\n".join(logs)
    assert "prod-east (https://10.1.2.3:6443)" in text
    assert "Namespace: mlrun" in text and "index.docker.io/me" in text
    assert asked == ["Proceed?"]


@pytest.mark.parametrize("field", ["dry_run", "non_interactive"])
def test_the_summary_does_not_ask_when_nothing_changes_or_nobody_answers(
    monkeypatch, settings, recorder, field
):
    _target(monkeypatch, recorder, settings)
    monkeypatch.setattr(ui, "confirm", lambda s, q: pytest.fail("asked"))
    setattr(settings, field, True)

    ui.confirm_target(settings)


@pytest.mark.parametrize("answer, expected", [("", True), ("y", True), ("n", False)])
def test_confirm_defaults_to_yes(monkeypatch, settings, answer, expected):
    monkeypatch.setattr("builtins.input", lambda: answer)

    assert cli_settings.confirm(settings, "Proceed?") is expected


def test_confirm_with_stdin_closed_is_a_no(monkeypatch, settings):
    def closed():
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

    assert cli_settings.confirm(settings, "Proceed?") is False


# ------------------------------------------------------------------------------------------
# The external-host guess followed whatever was installed, not the cluster targeted
# ------------------------------------------------------------------------------------------


def _host_guess(monkeypatch, recorder, *, context, provider="", kube_context=""):
    ran = recorder(answers={"minikube ip": Result(0, "192.168.58.2")})
    monkeypatch.setattr(cluster, "run", ran)
    monkeypatch.setattr(cluster.shutil, "which", lambda name: f"/usr/bin/{name}")
    kubectl = recorder(
        answers={
            "current-context": Result(0, context),
            "extension.provider": Result(0, provider),
            "InternalIP": Result(0, "192.168.65.3"),
        }
    )
    monkeypatch.setattr(cluster, "kubectl", kubectl)
    return ran, kubectl


def test_an_installed_minikube_does_not_decide_the_host_for_a_kind_cluster(
    monkeypatch, settings, recorder
):
    ran, _ = _host_guess(monkeypatch, recorder, context="kind-kind")

    assert cluster.suggest_external_host(settings) == "localhost"
    assert not ran.calls, "minikube was asked about a cluster it does not run"


def test_a_minikube_profile_is_recognised_by_its_provider_extension(
    monkeypatch, settings, recorder
):
    ran, _ = _host_guess(monkeypatch, recorder, context="dev", provider=cluster.MINIKUBE_PROVIDER)

    assert cluster.suggest_external_host(settings) == "192.168.58.2"
    assert ran.joined == ["minikube ip -p dev"]


def test_docker_desktop_chosen_through_kube_context_still_gets_its_host_name(
    monkeypatch, settings, recorder
):
    settings.kube_context = "docker-desktop"
    _, kubectl = _host_guess(monkeypatch, recorder, context="other", kube_context="x")

    # The node IP (192.168.65.3 here) is inside Docker Desktop's VM, not reachable from
    # the host.
    assert cluster.suggest_external_host(settings) == "host.docker.internal"
    assert not kubectl.ran("InternalIP")


def test_an_explicit_remote_context_suggests_its_node_ip(monkeypatch, settings, recorder):
    settings.kube_context = "lab"
    _host_guess(monkeypatch, recorder, context="docker-desktop")

    assert cluster.suggest_external_host(settings) == "192.168.65.3"


# ------------------------------------------------------------------------------------------
# A failed install said only "Helm installation failed"
# ------------------------------------------------------------------------------------------


def test_a_failed_install_says_where_to_look_next(monkeypatch, settings, capture_logs):
    settings.kube_context = "lab"
    monkeypatch.setattr(helm_ops, "stream", lambda cmd, **_: 1)
    monkeypatch.setattr(helm_ops, "helm", lambda *a, **k: Result(0, "v3.14.0"))
    logs = capture_logs(helm_ops)

    with pytest.raises(InstallerError):
        helm_ops.helm_install(settings)

    text = "\n".join(logs)
    assert "kubectl --context lab get pods -n mlrun" in text
    assert "get events -n mlrun --sort-by=.lastTimestamp" in text
    assert "HELM_TIMEOUT=960s" in text and "upgrades the release in place" in text
    assert helm_ops.README_URL + helm_ops.TROUBLESHOOTING_ANCHOR in text


# ------------------------------------------------------------------------------------------
# A re-run asked for a registry password it had already stored
# ------------------------------------------------------------------------------------------


@pytest.fixture
def interactive_secret(monkeypatch, settings):
    monkeypatch.setattr(registry, "kubectl", Recorder(answers=OURS))
    prompted = []
    monkeypatch.setattr(
        registry, "prompt_or_env", lambda s, var, *a, **k: prompted.append(var) or "x"
    )
    return prompted


def test_a_rerun_can_reuse_the_secret_it_created(monkeypatch, settings, interactive_secret):
    monkeypatch.setattr(registry, "confirm", lambda s, q: True)

    assert registry.prepare_registry_secret(settings) is False
    assert interactive_secret == [], "asked for credentials it was going to ignore"


def test_declining_the_reuse_asks_for_new_credentials(monkeypatch, settings, interactive_secret):
    monkeypatch.setattr(registry, "confirm", lambda s, q: False)

    assert registry.prepare_registry_secret(settings) is True
    assert "REGISTRY_PASSWORD" in interactive_secret


def test_credentials_handed_to_the_run_replace_the_stored_ones(
    monkeypatch, settings, interactive_secret
):
    monkeypatch.setenv("REGISTRY_PASSWORD", "new")
    monkeypatch.setattr(registry, "confirm", lambda s, q: pytest.fail("asked"))

    assert registry.prepare_registry_secret(settings) is True


def test_a_users_own_secret_asks_for_no_credentials(monkeypatch, settings):
    monkeypatch.setattr(registry, "kubectl", Recorder(answers={"get secret": Result(0, "exists:")}))
    monkeypatch.setattr(registry, "prompt_or_env", lambda *a, **k: pytest.fail("prompted"))

    assert registry.prepare_registry_secret(settings) is False


# ------------------------------------------------------------------------------------------
# A failure left nothing to attach to a bug report beyond the last screenful
# ------------------------------------------------------------------------------------------


@pytest.fixture
def debug_log(tmp_path):
    path = tmp_path / "install.log"
    console.open_debug_log(str(path), ["header line"])
    yield path
    console.close_debug_log()


def test_the_debug_log_records_a_failing_command_and_its_stderr(debug_log):
    shell.run([sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(3)"])
    console.close_debug_log()

    text = debug_log.read_text()
    assert "header line" in text
    assert "exit 3" in text and "kaboom" in text


def test_the_debug_log_never_holds_the_password(debug_log, monkeypatch, settings):
    monkeypatch.setattr(registry, "kubectl", Recorder())
    monkeypatch.setenv("REGISTRY_USERNAME", "me")
    monkeypatch.setenv("REGISTRY_PASSWORD", "hunter2-secret")
    settings.non_interactive = True

    registry.collect_registry_credentials(settings)
    registry.apply_registry_secret(settings)
    shell.run([sys.executable, "-c", "print('hunter2-secret')"], input_data="hunter2-secret")
    console.log_info("echoing hunter2-secret by mistake")
    console.close_debug_log()

    assert "hunter2-secret" not in debug_log.read_text()


def test_the_release_values_an_upgrade_reads_stay_out_of_the_debug_log(
    debug_log, monkeypatch, settings, tmp_path
):
    # `helm get values -o json` is not a Secret manifest, so the redactor let a configured
    # storage.s3.secretKey into the log the user shares.
    # Read from a file: argv is logged, and would hold the secret itself.
    values = tmp_path / "values.json"
    values.write_text('{"storage": {"s3": {"secretKey": "hunter2-secret"}}}')
    reader = f"import sys; sys.stdout.write(open({str(values)!r}).read())"

    def helm(_settings, *args, **kwargs):
        return shell.run([sys.executable, "-c", reader], **kwargs)

    monkeypatch.setattr(release_values, "helm", helm)
    settings.generated_values_file = str(tmp_path / "mlrun-ce-values.yaml")

    release_values.load_previous_values(settings)
    console.close_debug_log()

    assert settings.previous_values == {"storage": {"s3": {"secretKey": "hunter2-secret"}}}
    text = debug_log.read_text()
    assert "hunter2-secret" not in text
    assert "stdout not logged" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_debug_log_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "install.log"
    path.write_text("left by an earlier run")
    path.chmod(0o644)

    console.open_debug_log(str(path), [])
    console.close_debug_log()

    assert path.stat().st_mode & 0o777 == 0o600
    assert "earlier run" not in path.read_text()


def test_streamed_helm_output_reaches_both_the_terminal_and_the_log(debug_log, capsys):
    script = "import sys; print('to-out'); sys.stderr.write('to-err\\n'); sys.exit(2)"
    code = shell.stream([sys.executable, "-c", script])
    console.close_debug_log()

    captured = capsys.readouterr()
    assert code == 2
    assert "to-out" in captured.out and "to-err" in captured.err
    text = debug_log.read_text()
    assert "to-out" in text and "to-err" in text and "exit 2" in text


# ------------------------------------------------------------------------------------------
# Only warnings on the terminal: --log-level warning
# ------------------------------------------------------------------------------------------


def test_at_the_warning_level_helm_stdout_goes_to_the_log_only(debug_log, capsys):
    console.set_log_level("warning")
    script = "import sys; print('to-out'); sys.stderr.write('to-err\\n')"
    shell.stream([sys.executable, "-c", script])
    console.close_debug_log()

    captured = capsys.readouterr()
    assert "to-out" not in captured.out and "to-err" in captured.err
    assert "to-out" in debug_log.read_text()


def test_at_the_warning_level_info_lines_go_to_the_log_only(debug_log, capsys):
    console.set_log_level("warning")
    console.log_info("routine detail")
    console.log_warn("worth a look")
    console.log_result("Installation complete.")
    console.close_debug_log()

    shown = capsys.readouterr().out
    assert "routine detail" not in shown
    assert "worth a look" in shown and "Installation complete." in shown
    assert "[INFO] routine detail" in debug_log.read_text()


def test_at_the_warning_level_an_indented_warning_keeps_its_headline(capsys):
    console.set_log_level("warning")
    console.log_info("Checking the StorageClass...")
    console.log_info("  found: standard")
    console.log_warn("  it is not the default")
    console.log_info("Checking the NodePorts...")
    console.log_warn("Port 30060 is taken.")

    shown = capsys.readouterr().out
    assert "Checking the StorageClass..." in shown and "found: standard" not in shown
    assert shown.index("StorageClass") < shown.index("not the default")
    assert "Checking the NodePorts" not in shown and "30060" in shown


@pytest.mark.parametrize(("non_interactive", "shown"), [(False, True), (True, False)])
def test_at_the_warning_level_the_summary_shows_only_when_it_asks(
    monkeypatch, settings, recorder, capsys, non_interactive, shown
):
    _target(monkeypatch, recorder, settings)
    monkeypatch.setattr(ui, "confirm", lambda s, q: True)
    settings.non_interactive = non_interactive
    console.set_log_level("warning")

    ui.confirm_target(settings)

    assert ("Namespace: mlrun" in capsys.readouterr().out) is shown


def _level_of_a_run(monkeypatch, tmp_path, argv):
    seen = []
    monkeypatch.setattr(cli, "execute", lambda settings: seen.append(console.quiet()))
    monkeypatch.setenv("INSTALL_LOG_FILE", str(tmp_path / "run.log"))
    code = cli.main([*argv, "--non-interactive"])
    return code, seen


@pytest.mark.parametrize(
    ("argv", "env", "quiet"),
    [
        ([], "", False),
        (["--log-level", "warning"], "", True),
        (["--log-level", "WARNING"], "", True),
        ([], "warning", True),
        (["--log-level", "info"], "warning", False),
    ],
)
def test_the_log_level_comes_from_the_flag_before_the_environment(
    monkeypatch, tmp_path, argv, env, quiet
):
    if env:
        monkeypatch.setenv("LOG_LEVEL", env)

    assert _level_of_a_run(monkeypatch, tmp_path, argv) == (0, [quiet])


def test_an_unknown_log_level_stops_the_run(monkeypatch, tmp_path, capsys):
    code, seen = _level_of_a_run(monkeypatch, tmp_path, ["--log-level", "loud"])

    assert (code, seen) == (1, [])
    assert "Unknown log level 'loud'" in capsys.readouterr().err


def _main_failing_with(monkeypatch, tmp_path, error):
    def execute(settings):
        raise error

    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setenv("INSTALL_LOG_FILE", str(tmp_path / "run.log"))
    return cli.main(["--non-interactive"])


def test_a_failed_run_points_at_its_debug_log(monkeypatch, tmp_path, capsys):
    code = _main_failing_with(monkeypatch, tmp_path, InstallerError("nope", code=4))

    assert code == 4
    assert f"Full debug log: {tmp_path / 'run.log'}" in capsys.readouterr().err


def test_an_unexpected_error_puts_its_traceback_in_the_log(monkeypatch, tmp_path, capsys):
    code = _main_failing_with(monkeypatch, tmp_path, KeyError("surprise"))

    assert code == 1
    assert "Full debug log" in capsys.readouterr().err
    text = (tmp_path / "run.log").read_text()
    assert "Traceback" in text and "KeyError: 'surprise'" in text


# ------------------------------------------------------------------------------------------
# A first install's dry run always failed on the Prometheus CRDs the chart itself installs
# ------------------------------------------------------------------------------------------


def _cluster(monkeypatch, recorder, *, release=False, crds=False):
    helm = recorder(answers={"status": Result(0 if release else 1, "")})
    kubectl = recorder(answers={"get crd": Result(0 if crds else 1, "")})
    monkeypatch.setattr(helm_ops, "helm", helm)
    monkeypatch.setattr(helm_ops, "kubectl", kubectl)
    return helm, kubectl


def test_a_first_install_dry_runs_without_the_monitoring_stack(monkeypatch, settings, recorder):
    _cluster(monkeypatch, recorder)

    assert helm_ops.dry_run_exclusions(settings) == [
        "--set", "kube-prometheus-stack.enabled=false",
    ]  # fmt: skip


def test_an_upgrade_dry_runs_the_whole_chart(monkeypatch, settings, recorder):
    _, kubectl = _cluster(monkeypatch, recorder, release=True)

    assert helm_ops.dry_run_exclusions(settings) == []
    assert not kubectl.calls


def test_a_cluster_that_has_the_crds_dry_runs_the_whole_chart(monkeypatch, settings, recorder):
    _cluster(monkeypatch, recorder, crds=True)

    assert helm_ops.dry_run_exclusions(settings) == []


def test_nothing_is_excluded_with_the_monitoring_stack_disabled(monkeypatch, settings, recorder):
    helm, kubectl = _cluster(monkeypatch, recorder)
    settings.disable_system_monitoring = True

    assert helm_ops.dry_run_exclusions(settings) == []
    assert not helm.calls and not kubectl.calls


def test_the_exclusion_reaches_the_dry_run_but_not_the_printed_equivalent(
    monkeypatch, settings, recorder, capture_logs, tmp_path
):
    settings.dry_run = True
    settings.chart_ref = "mlrun-ce/mlrun-ce"
    settings.generated_values_file = str(tmp_path / "values.yaml")
    helm = recorder(answers={"status": Result(1, ""), "version": Result(0, "v3.14.0")})
    monkeypatch.setattr(helm_ops, "helm", helm)
    monkeypatch.setattr(helm_ops, "kubectl", recorder(answers={"get crd": Result(1, "")}))
    streamed = []
    monkeypatch.setattr(helm_ops, "stream", lambda cmd, **_: streamed.append(cmd) or 0)
    logs = capture_logs(helm_ops)

    helm_ops.helm_install(settings)

    assert "kube-prometheus-stack.enabled=false" in streamed[0]
    printed = [line for line in logs if "helm upgrade --install" in line]
    assert printed and "kube-prometheus-stack" not in printed[0]
    assert "kube-prometheus-stack" not in Path(settings.generated_values_file).read_text()


# ------------------------------------------------------------------------------------------
# Helm 4 rejected `helm list --all`, and the empty answer switched the second-release guard off
# ------------------------------------------------------------------------------------------


def _stub_helm(monkeypatch, helm_version):
    spec = importlib.util.spec_from_file_location("ce_stub", Path(__file__).parent / "stub.py")
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)
    monkeypatch.setenv("STUB_HELM_VERSION", helm_version)
    return stub.handle_helm


def test_the_release_listing_uses_status_flags_both_helm_majors_accept(
    monkeypatch, settings, recorder
):
    helm = recorder(answers={"list": Result(0, "[]")})
    monkeypatch.setattr(helm_ops, "helm", helm)

    helm_ops.other_mlrun_ce_releases(settings)

    listed = helm.calls[0]
    assert "--all" not in listed, "Helm 4 removed --all: 'unknown flag: --all'"
    # Helm 3 without a status flag lists only deployed and failed releases.
    assert {"--deployed", "--failed", "--pending"} <= set(listed)


@pytest.mark.parametrize("helm_version", ["v3.14.2+g2a2fb3b", "v4.1.1+g5caf004"])
def test_the_stub_accepts_the_listing_on_either_helm_major(monkeypatch, settings, helm_version):
    handle_helm = _stub_helm(monkeypatch, helm_version)
    args = ["list", "--namespace", "mlrun", *helm_ops.RELEASE_STATUS_FLAGS, "-o", "json"]

    assert handle_helm(args)[0] == 0
    assert handle_helm(["list", "--all"])[0] == (1 if helm_version.startswith("v4") else 0)


HELM_4_LIST_ERROR = Result(1, "", "Error: unknown flag: --all")


def test_a_failed_release_listing_stops_the_install(monkeypatch, settings, recorder):
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"list": HELM_4_LIST_ERROR}))

    with pytest.raises(InstallerError) as excinfo:
        helm_ops.refuse_a_second_release(settings)

    assert "unknown flag: --all" in str(excinfo.value)
    assert "helm list --namespace" in str(excinfo.value), "name the command that failed"


def test_a_failed_release_listing_stops_an_uninstall_cleanup(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={"get deployment,service,ingress,secret": Result(0, "secret/registry\n")}
    )
    helm_rec = recorder(answers={"status": Result(1, "", "not found"), "list": HELM_4_LIST_ERROR})
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.release_name = "typo"

    with pytest.raises(InstallerError):
        helm_ops.do_uninstall(settings)

    assert not kube_rec.ran("delete")


def test_a_release_listing_that_is_not_json_stops_the_run(monkeypatch, settings, recorder):
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"list": Result(0, "NAME  CHART")}))

    with pytest.raises(InstallerError):
        helm_ops.other_mlrun_ce_releases(settings)


# ------------------------------------------------------------------------------------------
# The Helm floor (3.6) let through a Helm older than the MLRun CE docs require
# ------------------------------------------------------------------------------------------


def _helm_check(monkeypatch, recorder, settings, version):
    monkeypatch.setattr(validators, "helm", recorder(answers={"version": Result(0, version)}))
    logs = {"info": [], "warn": [], "error": []}
    for level, messages in logs.items():
        monkeypatch.setattr(validators, f"log_{level}", messages.append)
    return validators.validate_helm_version(settings), logs


@pytest.mark.parametrize("version", ["v3.17.0+g301108e", "v3.19.0", "v4.0.5"])
def test_a_helm_below_41_is_refused_naming_the_mlrun_docs(monkeypatch, settings, recorder, version):
    ok, logs = _helm_check(monkeypatch, recorder, settings, version)

    assert ok is False
    error = " ".join(logs["error"])
    assert "below the minimum supported version (4.1)" in error
    assert cli_settings.MLRUN_CE_INSTALL_DOCS_URL in error, "say where the floor comes from"


@pytest.mark.parametrize("version", ["v4.1.1+g5caf004", "v4.2.0"])
def test_helm_41_and_later_pass_without_a_warning(monkeypatch, settings, recorder, version):
    ok, logs = _helm_check(monkeypatch, recorder, settings, version)

    assert ok is True
    assert logs["warn"] == []


def capture_by_level(monkeypatch, module):
    """Like the capture_logs fixture, but keeps info, warn and error apart. log_result
    counts as info."""
    logs = {"info": [], "warn": [], "error": []}
    for level, messages in logs.items():
        if hasattr(module, f"log_{level}"):
            monkeypatch.setattr(module, f"log_{level}", messages.append)
    if hasattr(module, "log_result"):
        monkeypatch.setattr(module, "log_result", logs["info"].append)
    return logs


# ------------------------------------------------------------------------------------------
# A non-interactive re-run could not reuse the installer's own pull secret
# ------------------------------------------------------------------------------------------


def _non_interactive_without_credentials(monkeypatch, settings):
    monkeypatch.setenv("REGISTRY_URL", "index.docker.io/someone")
    settings.non_interactive = True
    settings.local_registry = False
    config.check_required_non_interactive(settings)


def test_missing_credentials_alone_wait_for_the_secret_lookup(monkeypatch, settings):
    _non_interactive_without_credentials(monkeypatch, settings)

    # No raise: whether they are needed depends on a secret only the cluster knows about.
    assert any("REGISTRY_PASSWORD" in item for item in settings.missing_registry_credentials)


def test_other_missing_configuration_still_fails_up_front(settings):
    settings.non_interactive = True
    settings.local_registry = False

    with pytest.raises(InstallerError):
        config.check_required_non_interactive(settings)


def test_a_non_interactive_rerun_reuses_the_installers_secret(monkeypatch, settings):
    _non_interactive_without_credentials(monkeypatch, settings)
    monkeypatch.setattr(registry, "registry_secret_owner", lambda s: registry.SECRET_OURS)
    monkeypatch.setattr(registry, "collect_registry_credentials", pytest.fail)

    assert registry.prepare_registry_secret(settings) is False
    assert settings.registry_secret_reused


def test_without_a_secret_credentials_are_still_needed(monkeypatch, settings):
    _non_interactive_without_credentials(monkeypatch, settings)
    monkeypatch.setattr(registry, "registry_secret_owner", lambda s: registry.SECRET_ABSENT)

    with pytest.raises(InstallerError):
        registry.prepare_registry_secret(settings)


def test_a_foreign_secret_is_used_without_credentials(monkeypatch, settings):
    # It is used as it stands, so nothing is asked for it, in CI either.
    _non_interactive_without_credentials(monkeypatch, settings)
    monkeypatch.setattr(registry, "registry_secret_owner", lambda s: registry.SECRET_FOREIGN)
    monkeypatch.setattr(registry, "collect_registry_credentials", pytest.fail)

    assert registry.prepare_registry_secret(settings) is False


def test_supplied_credentials_still_replace_the_installers_secret(monkeypatch, settings):
    monkeypatch.setenv("REGISTRY_URL", "index.docker.io/someone")
    monkeypatch.setenv("REGISTRY_USERNAME", "someone")
    monkeypatch.setenv("REGISTRY_PASSWORD", "new-password")
    settings.non_interactive = True
    settings.local_registry = False
    config.check_required_non_interactive(settings)
    monkeypatch.setattr(registry, "registry_secret_owner", lambda s: registry.SECRET_OURS)
    collected = []
    monkeypatch.setattr(registry, "collect_registry_credentials", collected.append)

    assert registry.prepare_registry_secret(settings) is True
    assert collected


# ------------------------------------------------------------------------------------------
# The debug log held rendered Secrets
# ------------------------------------------------------------------------------------------

# Shaped like `helm upgrade --install --dry-run` output: helm's own header, a hook, Secrets
# with single-line, `|` block and inline values, one with kind after data, and a ConfigMap
# whose data must survive.
HELM_DRY_RUN_OUTPUT = """\
Release "mlrun-ce" does not exist. Installing it now.
NAME: mlrun-ce
STATUS: pending-install
HOOKS:
---
# Source: mlrun-ce/charts/seaweedfs/templates/s3/s3-secret.yaml
apiVersion: v1
kind: Secret
type: Opaque
metadata:
  name: seaweedfs-s3-secret
  annotations:
    "helm.sh/resource-policy": keep
stringData:
  admin_access_key_id: "hook-access-key"
  seaweedfs_s3_config: |
    {
      "identities": [{"name": "anvAdmin", "credentials": [{"secretKey": "json-secret"}]}]
    }
MANIFEST:
---
# Source: mlrun-ce/templates/config/storage-secret.yaml
apiVersion: v1
data:
  AWS_ACCESS_KEY_ID: YWNjZXNzLWtleQ==
  AWS_SECRET_ACCESS_KEY: c2VjcmV0LWtleQ==
kind: Secret
metadata:
  name: storage-credentials
---
# Source: mlrun-ce/charts/grafana/templates/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: grafana
data: {admin-password: Z3JhZmFuYS1wYXNz}
---
# Source: mlrun-ce/templates/config/mlrun-env-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mlrun-common-env
data:
  MLRUN_HTTPDB__DSN: "mysql+pymysql://root@mlrun-db:3306/mlrun"
  S3_ENDPOINT_URL: http://seaweedfs-s3:8333

NOTES:
Happy MLOPSing!!! :]
"""

SECRET_VALUES = (
    "hook-access-key", "json-secret", "YWNjZXNzLWtleQ==", "c2VjcmV0LWtleQ==",
    "Z3JhZmFuYS1wYXNz",
)  # fmt: skip


def test_rendered_secret_values_are_redacted(monkeypatch):
    redacted = secret_data.redact_secret_data(HELM_DRY_RUN_OUTPUT)

    for value in SECRET_VALUES:
        assert value not in redacted
    assert "  AWS_SECRET_ACCESS_KEY: <redacted>" in redacted, "keep the key, drop the value"
    assert "  seaweedfs_s3_config: <redacted>" in redacted
    assert '"identities"' not in redacted, "a | block goes with its key"
    assert "data: <redacted>" in redacted


def test_a_configmap_and_the_rest_of_the_output_are_left_alone():
    redacted = secret_data.redact_secret_data(HELM_DRY_RUN_OUTPUT)

    assert "  S3_ENDPOINT_URL: http://seaweedfs-s3:8333" in redacted
    assert "MLRUN_HTTPDB__DSN" in redacted
    assert "name: seaweedfs-s3-secret" in redacted and "Happy MLOPSing" in redacted
    assert '"helm.sh/resource-policy": keep' in redacted, "metadata is not data"


def test_the_streamed_helm_output_reaches_the_terminal_whole_and_the_log_redacted(monkeypatch):
    logged = []
    monkeypatch.setattr(shell, "log_debug", logged.append)
    terminal = io.StringIO()

    shell._tee(io.StringIO(HELM_DRY_RUN_OUTPUT), terminal)

    assert terminal.getvalue() == HELM_DRY_RUN_OUTPUT
    log = "".join(logged)
    assert not any(value in log for value in SECRET_VALUES)
    assert "S3_ENDPOINT_URL" in log and "Happy MLOPSing" in log, "nothing lost at the end"


def test_captured_command_output_is_redacted_too(monkeypatch):
    logged = []
    monkeypatch.setattr(shell, "log_debug", logged.append)

    shell._log_command(["helm", "template"], 0, 0.1, HELM_DRY_RUN_OUTPUT, "")

    assert not any(value in logged[0] for value in SECRET_VALUES)


# ------------------------------------------------------------------------------------------
# --hard-clean said "complete" with PVCs left
# ------------------------------------------------------------------------------------------


def test_a_hard_clean_names_the_pvcs_it_could_not_remove(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={
            "deletionTimestamp": Result(0, "data-kafka-0   2026-11-04T12:00:00Z\n"),
            "get pvc": Result(0, "data-kafka-0\n"),
        }
    )
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.hard_clean = True

    helm_ops.report_uninstall_leftovers(settings, helm_ops.do_hard_clean(settings))

    warning = " ".join(logs["warn"])
    assert "finished with leftovers: data-kafka-0 (Terminating)" in warning
    assert helm_ops.STRIMZI_LEFTOVERS_ANCHOR in warning
    assert "Hard clean complete." not in logs["info"]


def test_a_clean_hard_clean_still_says_complete(monkeypatch, settings, recorder):
    kube_rec = recorder(
        answers={"deletionTimestamp": Result(0, ""), "get pvc": Result(0, "data-0\n")}
    )
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.hard_clean = True

    helm_ops.report_uninstall_leftovers(settings, helm_ops.do_hard_clean(settings))

    assert "Hard clean complete." in logs["info"]
    assert not any("leftovers" in message for message in logs["warn"])


README = Path(__file__).resolve().parents[2] / "scripts" / "README.md"


def _anchor(heading):
    """GitHub's anchor for a markdown heading: lowercase, punctuation dropped, spaces to -."""
    return "#" + re.sub(r"[^\w\- ]", "", heading.lstrip("#").strip().lower()).replace(" ", "-")


def _readme_headings():
    return [line for line in README.read_text().splitlines() if re.match(r"#{2,4} ", line)]


def _readme_section(heading_prefix):
    """The body of the README section whose heading starts with `heading_prefix`."""
    text = README.read_text()
    start = text.index(heading_prefix)
    end = re.search(r"\n#{2,3} ", text[start + 1 :])
    return text[start : start + 1 + end.start()] if end else text[start:]


def test_every_readme_anchor_the_installer_prints_exists():
    anchors = {_anchor(line) for line in _readme_headings()}
    for printed in (
        helm_ops.TROUBLESHOOTING_ANCHOR,
        helm_ops.DRY_RUN_CRDS_ANCHOR,
        helm_ops.STRIMZI_LEFTOVERS_ANCHOR,
    ):
        assert printed in anchors, f"scripts/README.md has no heading for {printed}"
    assert helm_ops.README_URL.endswith("/scripts/README.md")


# ------------------------------------------------------------------------------------------
# "Registry auth: skipped" blamed -f-only mode whatever the reason
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup, reason",
    [
        ({"skip_registry_secret": True}, "--skip-secret, using the existing secret"),
        ({"registry_secret_reused": True}, "reusing the installer's secret"),
        ({"values_file": "v.yaml"}, "-f-only mode"),
        ({}, "already exists and is used as it stands"),
    ],
)
def test_the_registry_auth_skip_names_its_reason(monkeypatch, settings, setup, reason):
    settings.local_registry = False
    for field_name, value in setup.items():
        setattr(settings, field_name, value)
    logs = capture_by_level(monkeypatch, validators)

    assert validators.validate_registry_auth(settings) is True
    assert reason in " ".join(logs["info"])


# ------------------------------------------------------------------------------------------
# "n" at "Proceed?" was reported as an error
# ------------------------------------------------------------------------------------------


def test_no_at_proceed_is_a_cancel_not_an_error(monkeypatch, settings):
    monkeypatch.setattr(ui, "effective_context", lambda s: "ctx")
    monkeypatch.setattr(ui, "api_server", lambda s: "")
    monkeypatch.setattr(ui, "confirm", lambda s, q: False)
    settings.non_interactive = False
    logs = capture_by_level(monkeypatch, console)

    with pytest.raises(console.Cancelled) as excinfo:
        ui.confirm_target(settings)

    assert excinfo.value.code == 1
    assert "Cancelled; nothing on the cluster was changed." in logs["info"]
    assert logs["error"] == []


def test_a_cancelled_run_exits_1_without_pointing_at_the_debug_log(monkeypatch, capsys):
    def cancelled(settings):
        raise console.Cancelled("Cancelled; nothing on the cluster was changed.")

    monkeypatch.setattr(cli, "execute", cancelled)

    assert cli.main(["--non-interactive"]) == 1
    assert "Full debug log" not in capsys.readouterr().err


def test_a_failure_still_points_at_the_debug_log(monkeypatch, capsys):
    def failed(settings):
        raise InstallerError("boom")

    monkeypatch.setattr(cli, "execute", failed)

    assert cli.main(["--non-interactive"]) == 1
    assert "Full debug log" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------
# The chart's NOTES had no SeaweedFS S3 row
# ------------------------------------------------------------------------------------------

CHART = Path(__file__).resolve().parents[2] / "charts" / "mlrun-ce"


def test_the_notes_s3_row_follows_the_all_in_one_s3_switch():
    values = yaml.safe_load((CHART / "values.yaml").read_text())["seaweedfs"]
    notes = (CHART / "templates" / "NOTES.txt").read_text()

    # The default enables S3 only through allInOne; the row's condition has to look there.
    assert values["allInOne"]["s3"]["enabled"] and "enabled" not in values.get("s3", {})
    assert 'dig "allInOne" "s3" "enabled"' in notes


def test_the_access_table_lists_the_s3_endpoint(monkeypatch, settings):
    notes = (
        "SeaweedFS S3 API is available at:\n203.0.113.51:30094\n\n"
        "SeaweedFS Admin UI is available at:\n203.0.113.51:30093\n"
        "-  S3 credentials: seaweed / seaweed123\n"
    )

    table = render_table(monkeypatch, settings, notes)

    assert table["SeaweedFS S3 API"][0] == "203.0.113.51:30094"


# ------------------------------------------------------------------------------------------
# Uninstall and --hard-clean were silent about non-PVC leftovers
# ------------------------------------------------------------------------------------------

# One line per object, as RELEASE_LEFTOVER_KINDS' jsonpath prints them: kind/name, the
# resource policy, the deletion timestamp, the first owner's kind.
LABELLED_OBJECTS = """\
Job/mlrun-ce-mlrun-migrations-4|||
Pod/mlrun-ce-mlrun-migrations-4-x7k2p|||Job
Secret/seaweedfs-s3-secret|keep||
Service/mlrun-api|||
ReplicaSet/mlrun-api-6d9f|||Deployment
ConfigMap/being-deleted||2026-11-04T12:00:00Z|
"""


def _uninstall_with_release(monkeypatch, settings, recorder, labelled=LABELLED_OBJECTS):
    kube_rec = recorder(answers={"app.kubernetes.io/instance=": Result(0, labelled)})
    monkeypatch.setattr(helm_ops, "helm", recorder(answers=CE_RELEASE_LISTED))
    monkeypatch.setattr(helm_ops, "kubectl", kube_rec)
    settings.uninstall = True
    settings.non_interactive = True
    return kube_rec, capture_by_level(monkeypatch, helm_ops)


def test_leftovers_are_found_by_the_release_label_without_crds_or_pvcs(
    monkeypatch, settings, recorder
):
    kube_rec, _ = _uninstall_with_release(monkeypatch, settings, recorder)

    leftovers, kept = helm_ops.release_leftovers(settings)

    query = kube_rec.argv_containing("app.kubernetes.io/instance=")[0]
    assert "app.kubernetes.io/instance=mlrun-ce" in query
    kinds = query[1].split(",")
    assert "jobs" in kinds and "persistentvolumeclaims" not in kinds and "pvc" not in kinds
    assert all("." not in kind for kind in kinds), "a CRD kind fails on clusters without it"
    # Owned objects are still being cascaded away; deleting ones are on their way out.
    assert leftovers == ["job/mlrun-ce-mlrun-migrations-4", "service/mlrun-api"]
    assert kept == ["secret/seaweedfs-s3-secret"]


def test_a_plain_uninstall_names_the_leftovers_and_how_to_delete_them(
    monkeypatch, settings, recorder
):
    kube_rec, logs = _uninstall_with_release(monkeypatch, settings, recorder)

    helm_ops.do_uninstall(settings)

    warning = " ".join(logs["warn"])
    assert "job/mlrun-ce-mlrun-migrations-4" in warning
    assert "kubectl delete -n mlrun job/mlrun-ce-mlrun-migrations-4 service/mlrun-api" in warning
    assert "seaweedfs-s3-secret" not in warning, "kept on purpose is not a surprise"
    assert any("Kept on purpose" in line and "seaweedfs-s3-secret" in line for line in logs["info"])
    assert not kube_rec.ran("delete jobs") and not kube_rec.ran("delete job/"), "report only"


def test_a_hard_clean_folds_them_into_its_leftovers_line(monkeypatch, settings, recorder):
    kube_rec, logs = _uninstall_with_release(monkeypatch, settings, recorder)
    kube_rec.answers["deletionTimestamp"] = Result(0, "data-kafka-0   2026-11-04T12:00:00Z\n")
    kube_rec.answers["get pvc"] = Result(0, "data-kafka-0\n")
    settings.hard_clean = True

    helm_ops.do_uninstall(settings)

    assert any(
        "finished with leftovers: data-kafka-0 (Terminating), job/mlrun-ce-mlrun-migrations-4"
        in line
        for line in logs["warn"]
    )
    assert "Hard clean complete." not in logs["info"]


def test_only_kept_objects_still_make_a_complete_hard_clean(monkeypatch, settings, recorder):
    _, logs = _uninstall_with_release(
        monkeypatch, settings, recorder, labelled="Secret/seaweedfs-s3-secret|keep||\n"
    )
    settings.hard_clean = True

    helm_ops.do_uninstall(settings)

    assert "Hard clean complete." in logs["info"]
    assert not any("leftovers" in line or "Still in" in line for line in logs["warn"])


def test_a_dry_run_uninstall_reports_no_leftovers(monkeypatch, settings, recorder):
    kube_rec, _ = _uninstall_with_release(monkeypatch, settings, recorder)
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    # Nothing was removed, so everything would read as left over.
    assert not kube_rec.ran("app.kubernetes.io/instance=")


# From a lab: after a non-interactive hard clean the namespace still held three Nuclio
# build Jobs and the Spark webhook certificate, and the report named none of them. Same
# line format as above, without the resource policy.
UNLABELLED_OBJECTS = """\
Job/nuclio-buildjob-processor-demo-fn-latest-2mqxf||
Pod/nuclio-buildjob-processor-demo-fn-latest-2mqxjx9v9||Job
Secret/spark-operator-webhook-certs||
Job/nuclio-buildjob-processor-gone||2026-11-04T12:00:00Z
Secret/artifactory-registry-secret||
"""


def test_a_plain_uninstall_names_what_ce_left_without_a_label(monkeypatch, settings, recorder):
    kube_rec, logs = _uninstall_with_release(monkeypatch, settings, recorder, labelled="")
    kube_rec.answers["get jobs,pods,secrets"] = Result(0, UNLABELLED_OBJECTS)

    helm_ops.do_uninstall(settings)

    ours = "job/nuclio-buildjob-processor-demo-fn-latest-2mqxf secret/spark-operator-webhook-certs"
    warning = " ".join(logs["warn"])
    assert "created by MLRun CE at run time: " + ours.replace(" ", ", ") in warning
    assert "kubectl delete -n mlrun " + ours in warning
    # A Job's pod goes with it, one being deleted is on its way, the user's own is theirs.
    assert "2mqxjx9v9" not in warning and "processor-gone" not in warning
    assert "artifactory-registry-secret" not in warning
    assert not kube_rec.ran("delete job/"), "report only"


def test_a_hard_clean_with_only_unlabelled_leftovers_is_not_complete(
    monkeypatch, settings, recorder
):
    kube_rec, logs = _uninstall_with_release(monkeypatch, settings, recorder, labelled="")
    kube_rec.answers["get jobs,pods,secrets"] = Result(0, "Secret/spark-operator-webhook-certs||\n")
    settings.hard_clean = True

    helm_ops.do_uninstall(settings)

    assert any(
        line.endswith("finished with leftovers: secret/spark-operator-webhook-certs")
        for line in logs["warn"]
    )
    assert "Hard clean complete." not in logs["info"]


def test_a_dry_run_uninstall_does_not_look_for_unlabelled_leftovers(
    monkeypatch, settings, recorder
):
    kube_rec, _ = _uninstall_with_release(monkeypatch, settings, recorder)
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not kube_rec.ran("get jobs,pods,secrets")


# ------------------------------------------------------------------------------------------
# --hard-clean left Nuclio functions and MLRun run pods running, and said nothing
# ------------------------------------------------------------------------------------------


def _hard_clean_with_functions(monkeypatch, settings, recorder, answers=None):
    # One recorder for helm and kubectl, so the order between them is visible.
    rec = recorder(
        answers={
            "get nucliofunctions.nuclio.io": Result(0, "nucliofunction.nuclio.io/demo-fn\n"),
            "-l mlrun/class": Result(0, "pod/demo-job-x7k2p\n"),
            **CE_RELEASE_LISTED,
            **(answers or {}),
        }
    )
    monkeypatch.setattr(helm_ops, "helm", rec)
    monkeypatch.setattr(helm_ops, "kubectl", rec)
    settings.uninstall = True
    settings.hard_clean = True
    settings.non_interactive = True
    return rec


def test_nuclio_functions_are_deleted_while_nuclio_still_runs(monkeypatch, settings, recorder):
    # helm uninstall removed the Nuclio controller and its CRDs together, so nothing was
    # left to delete a function's Deployment, Service and HPA, which have no owner.
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)

    helm_ops.do_uninstall(settings)

    joined = rec.joined
    delete = joined.index(
        "delete nucliofunction.nuclio.io/demo-fn --namespace mlrun --ignore-not-found"
    )
    wait = next(i for i, c in enumerate(joined) if c.startswith("wait --for=delete deployments"))
    uninstall = next(i for i, c in enumerate(joined) if c.startswith("uninstall mlrun-ce"))
    assert delete < wait < uninstall
    assert "-l nuclio.io/function-name" in joined[wait]


def test_the_run_time_sweep_matches_none_of_the_charts_own_nuclio_objects(
    monkeypatch, settings, recorder
):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)

    helm_ops.do_hard_clean(settings)

    selectors = [c[c.index("-l") + 1] for c in rec.calls if c[0] == "get" and "-l" in c]
    assert selectors == ["nuclio.io/function-name", "mlrun/class", "mlrun/function"]
    assert not rec.ran("nuclio.io/class"), "the chart's own Nuclio pods are class=service"
    kinds = rec.argv_containing("-l mlrun/class")[0][1].split(",")
    assert "pods" in kinds and all("." not in kind for kind in kinds)


def test_run_time_leftovers_go_before_the_pvcs_and_only_once(monkeypatch, settings, recorder):
    # A run's pod still mounting a PVC holds it on pvc-protection.
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={"-l mlrun/function": Result(0, "pod/demo-job-x7k2p\npod/build-abc\n")},
    )

    helm_ops.do_hard_clean(settings)

    deletes = [c for c in rec.joined if c.startswith("delete pod/")]
    assert deletes == [
        "delete pod/demo-job-x7k2p pod/build-abc --namespace mlrun --ignore-not-found --timeout 60s"
    ]
    assert rec.joined.index(deletes[0]) < next(
        i for i, c in enumerate(rec.joined) if c.startswith("get pvc")
    )


def test_a_plain_uninstall_leaves_functions_and_runs_alone(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)
    settings.hard_clean = False

    helm_ops.do_uninstall(settings)

    assert not rec.ran("nucliofunctions") and not rec.ran("mlrun/class")


def test_a_dry_run_hard_clean_lists_functions_and_runs_without_deleting(
    monkeypatch, settings, recorder
):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not rec.ran("delete") and not rec.ran("wait") and not rec.ran("uninstall mlrun-ce")
    assert "  Dry-run: would delete nucliofunction.nuclio.io/demo-fn" in logs["info"]
    assert "  Dry-run: would delete pod/demo-job-x7k2p" in logs["info"]


def test_no_nuclio_crd_means_no_delete_and_no_wait(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={"get nucliofunctions.nuclio.io": Result(1, "doesn't have a resource type")},
    )

    helm_ops.delete_nuclio_resources(settings)

    assert not rec.ran("delete") and not rec.ran("wait")


# ------------------------------------------------------------------------------------------
# --hard-clean left the chart's Kafka running: its CRs are helm hooks, which uninstall skips
# ------------------------------------------------------------------------------------------

# name|helm.sh/chart|strimzi.io/cluster, as _STRIMZI_FIELDS prints them.
KAFKA_CRS = {
    "get kafka.kafka.strimzi.io": Result(0, "kafka-stream|mlrun-ce-0.11.0|\n"),
    "get kafkanodepool.kafka.strimzi.io": Result(
        0, "kafka-stream-pool|mlrun-ce-0.11.0|kafka-stream\n"
    ),
}
KAFKA_NAMES = "kafka.kafka.strimzi.io/kafka-stream kafkanodepool.kafka.strimzi.io/kafka-stream-pool"
KAFKA_DELETE = f"delete {KAFKA_NAMES} --namespace mlrun --ignore-not-found --timeout 60s"
KAFKA_WAIT = (
    "wait --for=delete strimzipodsets.core.strimzi.io,pods -l strimzi.io/cluster in "
    "(kafka-stream) --namespace mlrun --timeout 120s"
)


def test_the_charts_kafka_is_deleted_before_helm_uninstall_removes_strimzi(
    monkeypatch, settings, recorder
):
    # From a lab: the KafkaNodePool's StrimziPodSet kept the broker pod running after the
    # uninstall, and the pod kept its PVC Terminating for good.
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder, answers=KAFKA_CRS)

    helm_ops.do_uninstall(settings)

    joined = rec.joined
    delete = joined.index(KAFKA_DELETE)
    wait = joined.index(KAFKA_WAIT)
    uninstall = next(i for i, c in enumerate(joined) if c.startswith("uninstall mlrun-ce"))
    assert delete < wait < uninstall


def test_the_kafka_crs_are_hooks_carrying_the_chart_label_the_selection_reads():
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    common = helpers[helpers.index('define "mlrun-ce.common.labels"') :].split("{{- end")[0]
    assert 'helm.sh/chart: {{ include "mlrun-ce.chart" . }}' in common
    for template in ("kafka-cluster.yaml", "kafka-nodepool.yaml"):
        text = (CHART / "templates" / "kafka" / template).read_text()
        assert 'include "mlrun-ce.common.labels"' in text
        assert "helm.sh/hook: post-install,post-upgrade" in text, "else helm deletes them itself"


def test_a_kafka_another_chart_created_is_left_alone(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={
            "get kafka.kafka.strimzi.io": Result(
                0, "kafka-stream|mlrun-ce-0.11.0|\nteam-kafka|other-chart-1.0.0|\nbare||\n"
            ),
            "get kafkanodepool.kafka.strimzi.io": Result(
                0, "kafka-stream-pool|mlrun-ce-0.11.0|kafka-stream\nteam-pool||team-kafka\n"
            ),
        },
    )

    helm_ops.delete_strimzi_resources(settings)

    deletes = [c for c in rec.joined if c.startswith("delete")]
    assert deletes == [KAFKA_DELETE]
    assert rec.ran(KAFKA_WAIT)


def test_no_strimzi_crd_means_no_kafka_delete_and_no_wait(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={"get kafka": Result(1, 'the server doesn\'t have a resource type "kafka"')},
    )

    helm_ops.do_uninstall(settings)

    assert not rec.ran("delete kafka") and not rec.ran("strimzipodsets")
    assert rec.ran("uninstall mlrun-ce")


def test_a_dry_run_hard_clean_lists_the_kafka_without_deleting_it(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder, answers=KAFKA_CRS)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not rec.ran("delete") and not rec.ran("wait") and not rec.ran("uninstall mlrun-ce")
    for name in KAFKA_NAMES.split():
        assert f"  Dry-run: would delete {name}" in logs["info"]


def test_a_plain_uninstall_leaves_the_kafka_and_says_so(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder, answers=KAFKA_CRS)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.hard_clean = False

    helm_ops.do_uninstall(settings)

    assert not rec.ran("delete kafka") and not rec.ran("strimzipodsets")
    warning = " ".join(logs["warn"])
    assert ", ".join(KAFKA_NAMES.split()) in warning and "helm hooks" in warning
    assert "--hard-clean deletes them" in warning
    assert f"kubectl delete -n mlrun {KAFKA_NAMES}" in warning


def test_a_hard_clean_without_a_release_deletes_the_kafka_an_earlier_uninstall_left(
    monkeypatch, settings, recorder
):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={"status mlrun-ce": Result(1, "not found"), **KAFKA_CRS},
    )
    settings.non_interactive = False
    # The namespace typed back, yes to the run-time objects, and keep the namespace.
    _answer(monkeypatch, "mlrun", "y", "")

    helm_ops.do_uninstall(settings)

    joined = rec.joined
    delete = joined.index(KAFKA_DELETE)
    wait = joined.index(KAFKA_WAIT)
    pvcs = next(i for i, c in enumerate(joined) if c.startswith("get pvc"))
    assert delete < wait < pvcs
    assert not rec.ran("uninstall mlrun-ce")


def test_a_broker_pod_that_outlasts_the_wait_is_forced(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={
            **KAFKA_CRS,
            "wait --for=delete strimzipodsets": Result(1, "timed out waiting for the condition"),
            "get strimzipodsets": Result(0, "pod/kafka-stream-kafka-stream-pool-0\n"),
        },
    )

    helm_ops.delete_strimzi_resources(settings)

    assert rec.ran(
        "delete pod/kafka-stream-kafka-stream-pool-0 --namespace mlrun --force "
        "--grace-period=0 --wait=false"
    )


def _answer(monkeypatch, *answers):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda: next(replies))


@pytest.mark.parametrize("answer, deleted", [("n", False), ("", True), ("y", True)])
def test_an_interactive_hard_clean_asks_before_deleting_functions_and_runs(
    monkeypatch, settings, recorder, answer, deleted
):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)
    settings.non_interactive = False
    logs = capture_by_level(monkeypatch, helm_ops)
    _answer(monkeypatch, answer, "")  # the second answer keeps the namespace

    helm_ops.do_uninstall(settings)

    assert any("nucliofunction.nuclio.io/demo-fn" in line for line in logs["warn"])
    assert rec.ran("delete nucliofunction.nuclio.io/demo-fn") is deleted
    assert rec.ran("delete pod/demo-job-x7k2p") is deleted
    assert rec.ran("uninstall mlrun-ce"), "a no keeps the functions, not the release"


def test_nothing_to_ask_about_means_no_functions_prompt(monkeypatch, settings, recorder):
    _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={"get nucliofunctions.nuclio.io": Result(0, ""), "-l mlrun/class": Result(0, "")},
    )
    settings.non_interactive = False
    asked = []
    monkeypatch.setattr(helm_ops, "confirm", lambda _s, question, **_kw: asked.append(question))

    helm_ops.ask_about_runtime_objects(settings)

    assert asked == [] and settings.delete_runtime


def _listing(*objects):
    """`kubectl get ... -o json` output for (kind, name, labels) triples."""
    items = [
        {"kind": kind, "metadata": {"name": name, "labels": labels}}
        for kind, name, labels in objects
    ]
    return Result(0, json.dumps({"items": items}))


@pytest.mark.parametrize("answer, deleted", [("", False), ("n", False), ("y", True)])
def test_an_interactive_hard_clean_asks_before_deleting_the_namespace(
    monkeypatch, settings, recorder, answer, deleted
):
    rec = _hard_clean_with_functions(
        monkeypatch,
        settings,
        recorder,
        answers={
            "get deployments,statefulsets": _listing(
                ("ConfigMap", "kube-root-ca.crt", {}), ("Secret", "my-own-secret", {})
            )
        },
    )
    settings.non_interactive = False
    logs = capture_by_level(monkeypatch, helm_ops)
    _answer(monkeypatch, "y", answer)

    helm_ops.do_uninstall(settings)

    assert any(
        "did not create" in line and line.endswith(": secret/my-own-secret")
        for line in logs["warn"]
    )
    assert rec.ran("delete namespace mlrun") is deleted
    # Deleted, the namespace has nothing left to report on.
    assert rec.ran("app.kubernetes.io/instance=") is not deleted


def test_the_namespace_prompt_names_in_full_what_mlrun_ce_did_not_create(
    monkeypatch, settings, recorder
):
    # From a lab namespace: the pull secret and Ingress a person applied by hand sat among
    # 22 CE leftovers, past the first 8 the prompt showed, and a yes would have taken them.
    strimzi = {"app.kubernetes.io/managed-by": "strimzi-cluster-operator", "strimzi.io/kind": "x"}
    chart = {"app.kubernetes.io/managed-by": "Helm", "app.kubernetes.io/instance": "mlrun-ce"}
    leftovers = [("Secret", f"kafka-stream-ca-{i}", strimzi) for i in range(9)] + [
        ("Pod", "kafka-stream-kafka-stream-pool-0", strimzi),
        ("Secret", "seaweedfs-s3-secret", chart),
        ("Job", "nuclio-buildjob-processor-demo-fn-latest-2mqxf", {}),
        ("Pod", "nuclio-buildjob-processor-demo-fn-latest-2mqxjx9v9", {"job-name": "x"}),
        ("Secret", "spark-operator-webhook-certs", {}),
        ("ConfigMap", "kube-root-ca.crt", {}),
    ]
    others = [
        ("Secret", "artifactory-registry-secret", {}),
        ("Ingress", "seaweedfs-s3-ingress", {}),
        ("Secret", "other-app", {"app.kubernetes.io/instance": "other-app"}),
    ]
    monkeypatch.setattr(
        helm_ops, "kubectl", recorder(answers={"get deployments": _listing(*leftovers, *others)})
    )
    logs = capture_by_level(monkeypatch, helm_ops)

    helm_ops._warn_about_namespace_contents(settings)

    ours, theirs = logs["warn"]
    assert "MLRun CE leftovers" in ours and "and 6 more" in ours
    assert theirs.endswith(
        ": secret/artifactory-registry-secret, ingress/seaweedfs-s3-ingress, secret/other-app"
    )


def test_a_namespace_that_cannot_be_listed_is_said_to_go_whole(monkeypatch, settings, recorder):
    monkeypatch.setattr(helm_ops, "kubectl", recorder(answers={"get deployments": Result(1, "")}))
    logs = capture_by_level(monkeypatch, helm_ops)

    helm_ops._warn_about_namespace_contents(settings)

    assert logs["warn"] == ["Could not list what 'mlrun' holds; deleting it deletes all of it."]


@pytest.mark.parametrize("flag, deleted", [(False, False), (True, True)])
def test_a_non_interactive_hard_clean_deletes_the_namespace_only_with_the_flag(
    monkeypatch, settings, recorder, flag, deleted
):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)
    settings.delete_namespace = flag

    helm_ops.do_uninstall(settings)

    assert rec.ran("delete namespace mlrun") is deleted
    assert not rec.ran("get deployments,statefulsets"), "nothing to list without a question"


def test_a_dry_run_names_the_namespace_without_deleting_it(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(monkeypatch, settings, recorder)
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.delete_namespace = True
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert "Dry-run: would delete namespace 'mlrun'" in logs["info"]
    assert not rec.ran("delete namespace")


def test_a_namespace_that_does_not_go_in_time_is_reported(monkeypatch, settings, recorder):
    rec = _hard_clean_with_functions(
        monkeypatch, settings, recorder, answers={"delete namespace": Result(1, "timed out")}
    )
    logs = capture_by_level(monkeypatch, helm_ops)
    settings.delete_namespace = True

    helm_ops.do_uninstall(settings)

    assert any("still terminating" in line for line in logs["warn"])
    assert not rec.ran("app.kubernetes.io/instance=")


def test_the_chart_renders_none_of_the_run_time_labels():
    chart_dir = Path(__file__).resolve().parents[2] / "charts" / "mlrun-ce"
    if not glob.glob(str(chart_dir / "charts" / "*.tgz")):
        pytest.skip("subcharts not vendored — run `make helm-update-dependencies` first")
    rendered = subprocess.run(
        ["helm", "template", "mlrun-ce", str(chart_dir)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    labels = [
        line.strip()
        for line in rendered.splitlines()
        if any(line.strip().startswith(f"{label}:") for label in helm_ops.RUNTIME_LABELS)
    ]
    assert labels == []


def test_the_migrations_job_carries_the_label_the_query_selects_on():
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    selector = helpers[helpers.index('define "mlrun-ce.common.selectorLabels"') :]
    migrations = helpers[helpers.index('define "mlrun-ce.mlrunMigrations.selectorLabels"') :]

    assert "app.kubernetes.io/instance: {{ .Release.Name }}" in selector.split("{{- end")[0]
    assert 'include "mlrun-ce.common.selectorLabels"' in migrations.split("{{- end")[0]


# ------------------------------------------------------------------------------------------
# The hard-clean namespace prompt showed "[mlrun]", which reads as a default
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["", "mlurn"])
def test_the_namespace_prompt_shows_no_default_and_anything_else_cancels(
    monkeypatch, settings, capsys, typed
):
    monkeypatch.setattr("builtins.input", lambda: typed)
    settings.non_interactive = False

    with pytest.raises(console.Cancelled):
        helm_ops.confirm_hard_clean_without_release(settings)

    prompt = capsys.readouterr().err
    assert "[mlrun]" not in prompt and "'mlrun'" in prompt
    assert "anything else cancels" in prompt


def test_typing_the_namespace_back_confirms(monkeypatch, settings):
    monkeypatch.setattr("builtins.input", lambda: "mlrun")
    settings.non_interactive = False

    helm_ops.confirm_hard_clean_without_release(settings)


# ------------------------------------------------------------------------------------------
# The docs said "run the hard clean again", which --non-interactive refuses without a release
# ------------------------------------------------------------------------------------------


def test_the_strimzi_entry_sends_the_rerun_through_the_interactive_confirmation():
    entry = _readme_section("### A hard clean left a Kafka pod")

    assert "kubectl get pvc -n mlrun" in entry
    assert "interactively" in entry and "type the namespace" in entry
    refuses = next(h for h in _readme_headings() if h.startswith("### `--hard-clean` refused"))
    assert f"]({_anchor(refuses)})" in entry, "the link must land on the refused entry"


# ------------------------------------------------------------------------------------------
# host.docker.internal resolves in Docker Desktop's pods, not always on the Mac itself
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("resolves", "hinted"), [(False, True), (True, False)])
def test_an_unresolvable_host_docker_internal_gets_the_hosts_line(
    monkeypatch, settings, capture_logs, resolves, hinted
):
    def lookup(name):
        if not resolves:
            raise OSError("nodename nor servname provided")
        return "127.0.0.1"

    monkeypatch.setattr(helm_ops.socket, "gethostbyname", lookup)
    logs = capture_logs(helm_ops)
    settings.external_host_address = "host.docker.internal"

    helm_ops.report_unresolved_host(settings)

    hint = 'echo "127.0.0.1 host.docker.internal" | sudo tee -a /etc/hosts'
    assert any(hint in line for line in logs) is hinted


def test_other_addresses_are_not_looked_up(monkeypatch, settings):
    monkeypatch.setattr(helm_ops.socket, "gethostbyname", lambda n: pytest.fail("looked up"))
    settings.external_host_address = "203.0.113.10"

    helm_ops.report_unresolved_host(settings)


# ------------------------------------------------------------------------------------------
# An existing installer values file was overwritten without a word
# ------------------------------------------------------------------------------------------


def _existing_values(settings, tmp_path, text="global: {}\n"):
    path = tmp_path / "mlrun-ce-values.yaml"
    path.write_text(text)
    settings.generated_values_file = str(path)
    settings.registry_url = "index.docker.io/me"
    return path


def test_an_existing_values_file_is_overwritten_only_after_asking(
    monkeypatch, settings, tmp_path, capture_logs
):
    path = _existing_values(settings, tmp_path)
    logs = capture_logs(helm_ops)
    asked = []
    monkeypatch.setattr(helm_ops, "confirm", lambda s, q: asked.append(q) or False)

    with pytest.raises(Cancelled):
        helm_ops.settle_existing_installer_values(settings)

    assert asked and any(str(path) in line for line in logs)
    assert path.read_text() == "global: {}\n"
    assert not (tmp_path / "mlrun-ce-values.yaml.bak").exists()


OLD_VALUES = """\
global:
  registry:
    url: index.docker.io/old
nuclio:
  dashboard:
    resources:
      limits:
        memory: 2Gi
"""


@pytest.mark.parametrize("answer", ["m", "merge"])
def test_a_merge_keeps_the_old_only_keys_and_this_runs_values_win(
    monkeypatch, settings, tmp_path, answer
):
    path = _existing_values(settings, tmp_path, OLD_VALUES)
    monkeypatch.setattr("builtins.input", lambda: answer)

    helm_ops.settle_existing_installer_values(settings)
    helm_ops.write_installer_values(settings)

    written = yaml.safe_load(path.read_text())
    assert written["global"]["registry"]["url"] == "index.docker.io/me"
    assert written["nuclio"]["dashboard"]["resources"]["limits"]["memory"] == "2Gi"
    assert (tmp_path / "mlrun-ce-values.yaml.bak").read_text() == OLD_VALUES


@pytest.mark.parametrize("answer", ["", "o", "override"])
def test_an_override_drops_the_old_only_keys(monkeypatch, settings, tmp_path, answer):
    path = _existing_values(settings, tmp_path, OLD_VALUES)
    monkeypatch.setattr("builtins.input", lambda: answer)

    helm_ops.settle_existing_installer_values(settings)
    helm_ops.write_installer_values(settings)

    written = yaml.safe_load(path.read_text())
    assert "nuclio" not in written or "resources" not in written["nuclio"].get("dashboard", {})
    assert (tmp_path / "mlrun-ce-values.yaml.bak").read_text() == OLD_VALUES


@pytest.mark.parametrize("answer", ["c", "cancel", "x"])
def test_a_cancel_leaves_the_old_values_file_alone(monkeypatch, settings, tmp_path, answer):
    path = _existing_values(settings, tmp_path, OLD_VALUES)
    monkeypatch.setattr("builtins.input", lambda: answer)

    with pytest.raises(Cancelled):
        helm_ops.settle_existing_installer_values(settings)

    assert path.read_text() == OLD_VALUES
    assert not (tmp_path / "mlrun-ce-values.yaml.bak").exists()


def test_the_prompt_names_what_differs_and_what_a_merge_keeps(
    monkeypatch, settings, tmp_path, capture_logs
):
    _existing_values(settings, tmp_path, OLD_VALUES)
    logs = capture_logs(helm_ops)
    monkeypatch.setattr("builtins.input", lambda: "c")

    with pytest.raises(Cancelled):
        helm_ops.settle_existing_installer_values(settings)

    assert "  global.registry.url: index.docker.io/old -> index.docker.io/me" in logs
    assert "  nuclio.dashboard.resources.limits.memory: 2Gi (only in the old file)" in logs


def test_a_closed_stdin_cancels_the_choice(monkeypatch, settings):
    def closed():
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

    assert choose(settings, "Merge?", helm_ops.MERGE_CHOICES, "override") == "cancel"


def test_an_unreadable_old_file_is_only_offered_an_override(monkeypatch, settings, tmp_path):
    _existing_values(settings, tmp_path, "global: [unclosed\n")
    asked = []
    monkeypatch.setattr(helm_ops, "confirm", lambda s, q: asked.append(q) or True)
    monkeypatch.setattr(helm_ops, "choose", lambda *a: pytest.fail("offered a merge"))

    helm_ops.settle_existing_installer_values(settings)

    assert asked == ["Override it with this run's values?"]
    assert settings.kept_installer_values == {}


def test_the_upgrade_change_report_sees_the_kept_keys(settings):
    settings.registry_url = "index.docker.io/me"
    settings.previous_values = {}
    settings.kept_installer_values = {"nuclio": {"dashboard": {"replicas": 2}}}

    changes = release_values.value_changes(settings)

    assert {"key": "nuclio.dashboard.replicas", "previous": "(unset)", "new": 2} in changes


def test_without_a_prompt_the_old_values_file_is_kept_as_bak(settings, tmp_path, capture_logs):
    path = _existing_values(settings, tmp_path)
    logs = capture_logs(helm_ops)
    settings.non_interactive = True

    helm_ops.settle_existing_installer_values(settings)

    assert (tmp_path / "mlrun-ce-values.yaml.bak").read_text() == "global: {}\n"
    assert any(str(path) in line and ".bak" in line for line in logs)


def test_an_unchanged_values_file_is_not_asked_about(monkeypatch, settings, tmp_path):
    settings.registry_url = "index.docker.io/me"
    path = _existing_values(settings, tmp_path, helm_ops.installer_values_text(settings))
    monkeypatch.setattr(helm_ops, "confirm", lambda s, q: pytest.fail("asked"))
    settings.non_interactive = True

    helm_ops.settle_existing_installer_values(settings)

    assert not (tmp_path / "mlrun-ce-values.yaml.bak").exists()
    assert path.exists()


# ------------------------------------------------------------------------------------------
# An upgrade without --ce-version moved a release installed from an rc to an older chart
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "installed, target, downgrade",
    [
        ("0.12.1-rc.8", "0.12.0", True),
        ("0.12.0-rc.8", "0.12.0", False),  # a release is newer than its rc
        ("0.12.0-rc.10", "0.12.0-rc.9", True),  # numerically, not as text
        ("0.12.0", "0.12.0", False),
        ("0.11.3", "0.12.0", False),
        ("not-a-version", "0.12.0", False),
        ("", "0.12.0", False),
    ],
)
def test_is_downgrade_follows_semver(installed, target, downgrade):
    assert helm_ops.is_downgrade(installed, target) is downgrade


def _downgrade(settings, *, newest=True):
    settings.installed_chart_version = "0.12.1-rc.8"
    settings.ce_version = "0.12.0"
    settings.ce_version_is_newest = newest


def test_an_unasked_downgrade_stops_a_non_interactive_run(settings):
    _downgrade(settings)
    settings.non_interactive = True

    with pytest.raises(InstallerError) as excinfo:
        helm_ops.refuse_an_unasked_downgrade(settings)

    assert "--ce-version 0.12.1-rc.8" in excinfo.value.message
    assert "--devel" in excinfo.value.message


@pytest.mark.parametrize("answer", [True, False])
def test_an_unasked_downgrade_is_asked_about(monkeypatch, settings, answer):
    _downgrade(settings)
    asked = []
    monkeypatch.setattr(
        helm_ops, "confirm", lambda s, q, default_yes: asked.append(default_yes) or answer
    )

    if answer:
        helm_ops.refuse_an_unasked_downgrade(settings)
    else:
        with pytest.raises(Cancelled):
            helm_ops.refuse_an_unasked_downgrade(settings)
    assert asked == [False], "the default must be no"


def test_a_dry_run_only_warns_about_the_downgrade(monkeypatch, settings, capture_logs):
    _downgrade(settings)
    settings.dry_run = True
    monkeypatch.setattr(helm_ops, "confirm", lambda *a, **k: pytest.fail("asked"))
    logs = capture_logs(helm_ops)

    helm_ops.refuse_an_unasked_downgrade(settings)

    assert any("0.12.0 is older" in line for line in logs)


def test_a_downgrade_named_by_ce_version_is_only_warned_about(settings, capture_logs):
    _downgrade(settings, newest=False)
    settings.non_interactive = True
    logs = capture_logs(helm_ops)

    helm_ops.refuse_an_unasked_downgrade(settings)

    assert any("downgrades the release" in line for line in logs)


def test_the_installed_chart_version_comes_from_the_release_listing(
    monkeypatch, settings, recorder
):
    listing = [
        {"name": "mlrun-ce", "chart": "mlrun-ce-0.12.1-rc.8"},
        {"name": "other-app", "chart": "redis-1.0.0"},
    ]
    answers = {"list": Result(0, json.dumps(listing))}
    monkeypatch.setattr(helm_ops, "helm", recorder(answers=answers))

    helm_ops.refuse_a_second_release(settings)

    assert settings.installed_chart_version == "0.12.1-rc.8"
