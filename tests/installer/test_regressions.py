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
"""One named test per entry in scripts/AGENTS.md's "Fixed bugs" section, plus the output
formatting the differential harness structurally cannot see.

The harness in matrix.sh compares the *calls* two implementations make. Everything here is
about what a single implementation computes or prints, which that comparison would report
as identical no matter how wrong it was — the access-URL table shipped broken through 28
green matrix cases.

Each test name ends in the symptom a user would have reported, so a future failure says
what regressed rather than which assertion tripped.
"""

import glob
import os
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ce_installer import cli, cluster, config, helm_ops, registry, shell, ui, validators
from ce_installer.config import check_required_non_interactive
from ce_installer.console import InstallerError
from ce_installer.settings import prompt_or_env
from ce_installer.shell import Result

from .conftest import Recorder

# ------------------------------------------------------------------------------------------
# The access-URL table put the wrong text in the URL column
# ------------------------------------------------------------------------------------------

# Trimmed from a real `helm get notes` on the rke2 lab: SeaweedFS carries a trailing detail
# line, and TimescaleDB is last, so the otel section follows it with nothing in between.
LIVE_NOTES = """\
Jupyter UI is available at:
192.168.236.51:30040

SeaweedFS Admin UI is available at:
192.168.236.51:30093
-  S3 credentials: seaweed / seaweed123

TimescaleDB is available at:
192.168.236.51:30110
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
192.168.236.51:30060
Sign in with the admin account.
"""

LATER_SECTION_NOTES = """\
TimescaleDB is available at:
192.168.236.51:30110
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
    assert rows["SeaweedFS Admin UI"][0] == "192.168.236.51:30093"


def test_notes_table_last_service_does_not_absorb_the_rest_of_the_notes(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, LIVE_NOTES)
    # Was a sentence of OpenTelemetry prose: nothing terminated the final entry, so it kept
    # consuming lines to the end of the NOTES.
    assert rows["TimescaleDB"][0] == "192.168.236.51:30110"
    assert "OTel" not in rows["TimescaleDB"][0]


def test_notes_table_url_survives_a_following_detail_line(monkeypatch, settings):
    rows = render_table(monkeypatch, settings, PLAIN_DETAIL_NOTES)
    # Isolates "first non-empty line wins" from the credentials handling: an ordinary prose
    # line after the address used to overwrite it.
    assert rows["MLRun UI"][0] == "192.168.236.51:30060"


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
    recorder = Recorder()
    monkeypatch.setattr(helm_ops, "helm", recorder)
    monkeypatch.setattr(helm_ops, "kubectl", Recorder())
    monkeypatch.setattr(helm_ops, "check_requirements", lambda s: None)

    helm_ops.do_uninstall(settings)

    # The follow-up to the install fix: uninstall kept a literal 960s, so raising
    # HELM_TIMEOUT reached install but not uninstall.
    uninstall = recorder.argv_containing("uninstall")
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

    registry.deploy_local_registry(settings)

    # The guard sits after the URL assignment on purpose: a dry run has to render the same
    # --set flags the real install would.
    assert settings.local_registry_url


def test_local_registry_real_run_still_applies(monkeypatch, settings):
    settings.dry_run = False
    settings.external_host_address = "localhost"
    recorder = Recorder()
    monkeypatch.setattr(registry, "kubectl", recorder)

    registry.deploy_local_registry(settings)

    assert recorder.argv_containing("apply"), "a real run must deploy the registry"


# ------------------------------------------------------------------------------------------
# resolve_external_host()'s autodetect ignored KUBE_CONTEXT / generic fallback
# ------------------------------------------------------------------------------------------


def test_external_host_with_kube_context_ignores_local_machine_heuristics(monkeypatch, settings):
    settings.kube_context = "remote-cluster"
    settings.non_interactive = True

    def answer(*args, **kwargs):
        if "InternalIP" in " ".join(args[1:]):
            return Result(0, "192.168.236.51")
        return Result(0, "docker-desktop")

    monkeypatch.setattr(cluster, "kubectl", answer)
    # Present and working: the point is that it must not be consulted.
    monkeypatch.setattr(cluster.shutil, "which", lambda _: "/usr/local/bin/minikube")
    monkeypatch.setattr(cluster, "run", lambda *a, **k: Result(0, "192.168.49.2"))

    cluster.resolve_external_host(settings)

    # `kubectl config current-context` reports the ambient context regardless of --context,
    # so these heuristics describe the local machine, not the selected cluster. The value
    # flows into --set global.externalHostAddress, so a wrong guess is not cosmetic.
    assert settings.external_host_address == "192.168.236.51"


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


def test_every_declared_dependency_has_an_upper_bound():
    # `uvx --from "git+...#subdirectory=scripts"` resolves this list, not install.py.lock,
    # so an unbounded floor lets a release-tagged install pick up a future major of typer
    # or click and behave differently from the one that was tested.
    pyproject = (Path(__file__).resolve().parents[2] / "scripts" / "pyproject.toml").read_text()
    block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.MULTILINE | re.DOTALL)
    declared = re.findall(r'"([^"]+)"', block.group(1))

    assert declared, "no dependencies found — did the pyproject layout change?"
    for requirement in declared:
        assert "<" in requirement, f"{requirement} has no upper bound"


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
    assert "nuclio.dashboard.kaniko.insecurePushRegistry=true" in flags
    assert "nuclio.dashboard.kaniko.insecurePullRegistry=true" in flags


def test_the_insecure_registry_flag_names_a_key_a_chart_actually_reads(settings):
    # The bug this pins: bash set `mlrun.api.kaniko.insecureRegistry`, the mlrun chart has
    # never had a kaniko key, and helm accepts an unknown --set path in silence — so the
    # flag read as present and did nothing. nuclio's dashboard is the container builder.
    settings.local_registry = True

    assert not [flag for flag in helm_ops.build_set_flags(settings) if "mlrun.api.kaniko" in flag]


def test_kaniko_is_not_told_anything_when_the_registry_is_not_local(settings):
    settings.local_registry = False

    flags = helm_ops.build_set_flags(settings)
    assert not [flag for flag in flags if "kaniko" in flag]


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
    helm_rec = recorder(answers={"status": Result(0, "STATUS: deployed")})
    monkeypatch.setattr(helm_ops, "helm", helm_rec)
    monkeypatch.setattr(helm_ops, "kubectl", recorder())
    monkeypatch.setattr(helm_ops, "check_requirements", lambda s: None)
    settings.uninstall = True
    settings.dry_run = True

    helm_ops.do_uninstall(settings)

    assert not helm_rec.ran("uninstall")
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
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"status": Result(0, "deployed")}))
    monkeypatch.setattr(helm_ops, "kubectl", recorder())
    monkeypatch.setattr(helm_ops, "check_requirements", lambda s: None)
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
    listing = "kube-system/ \ndefault/ 30040 \n"
    monkeypatch.setattr(validators, "kubectl", recorder(answers={"get svc": Result(0, listing)}))
    logs = capture_logs(validators)

    validators.validate_nodeport_conflicts(settings)

    assert any("30040" in line for line in logs), (
        "a conflict on a chart NodePort should be reported, not swallowed"
    )


def test_a_nodeport_owned_by_this_release_is_not_a_conflict(
    monkeypatch, settings, recorder, capture_logs
):
    listing = f"{settings.namespace}/{settings.release_name} 30040 30050 \n"
    monkeypatch.setattr(validators, "kubectl", recorder(answers={"get svc": Result(0, listing)}))
    logs = capture_logs(validators)

    validators.validate_nodeport_conflicts(settings)

    assert any("no conflicts" in line for line in logs)


# ------------------------------------------------------------------------------------------
# Things that happened in the wrong order, or that a dry run should not have done
# ------------------------------------------------------------------------------------------


def test_a_bad_chart_path_is_caught_before_the_namespace_is_created(monkeypatch, settings):
    # resolve_chart_source validates the path, but only runs after ensure_namespace and
    # deploy_local_registry — so a typo left a namespace and a running registry behind.
    settings.chart_path = "/no/such/chart"

    with pytest.raises(InstallerError):
        cluster.validate_chart_path(settings)


def test_a_directory_without_a_chart_yaml_is_rejected(settings, tmp_path):
    settings.chart_path = str(tmp_path)

    with pytest.raises(InstallerError):
        cluster.validate_chart_path(settings)


def test_a_helm_older_than_313_gets_a_client_side_dry_run(
    monkeypatch, settings, recorder, capture_logs
):
    # --dry-run only became a string flag in helm 3.13; before that `--dry-run=server` is an
    # invalid boolean and the install never starts. The chart's own floor is 3.6.
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"version": Result(0, "v3.12.3")}))
    logs = capture_logs(helm_ops)
    settings.dry_run = True

    assert helm_ops.dry_run_flag(settings) == "--dry-run"
    assert any("3.13" in message for message in logs), "the downgrade must not be silent"


def test_a_helm_new_enough_still_gets_the_server_side_dry_run(monkeypatch, settings, recorder):
    monkeypatch.setattr(helm_ops, "helm", recorder(answers={"version": Result(0, "v3.16.2")}))
    settings.dry_run = True

    assert helm_ops.dry_run_flag(settings) == "--dry-run=server"


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
    monkeypatch.setattr(cluster, "kubectl", recorder())
    monkeypatch.setenv("EXTERNAL_HOST_ADDRESS", "")
    settings.non_interactive = True
    settings.kube_context = ""

    cluster.resolve_external_host(settings)

    assert settings.external_host_address == "192.168.49.2"
    assert len([call for call in rec.calls if "minikube" in " ".join(call)]) == 1


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
    # repositories.yaml, and nothing put it back afterwards.
    monkeypatch.delenv("HELM_REPOSITORY_CONFIG", raising=False)

    cluster.isolate_helm_repo_config()

    configured = Path(os.environ["HELM_REPOSITORY_CONFIG"])
    assert configured != Path.home() / ".config" / "helm" / "repositories.yaml"
    assert not configured.exists(), "helm creates it; the installer only names the location"


def test_an_explicit_helm_repository_config_is_respected(monkeypatch):
    monkeypatch.setenv("HELM_REPOSITORY_CONFIG", "/somewhere/mine.yaml")

    cluster.isolate_helm_repo_config()

    assert os.environ["HELM_REPOSITORY_CONFIG"] == "/somewhere/mine.yaml"


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
    # deliberately not declared anywhere, so it is exempt.
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

    flags = helm_ops.build_set_flags(settings)
    paths = [
        value.split("=", 1)[0]
        for flag, value in zip(flags, flags[1:])
        if flag == "--set" and not value.startswith("global.")
    ]
    assert paths, "no --set paths to check — did build_set_flags change shape?"

    missing = []
    for path in paths:
        node = values
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                break
            node = node[part]
    assert not missing, "--set paths no chart declares: " + ", ".join(missing)


# ------------------------------------------------------------------------------------------
# --show-progress off a terminal threw the helm output away
# ------------------------------------------------------------------------------------------


def test_show_progress_off_a_terminal_falls_back_to_helm_output(monkeypatch, settings):
    # The progress UI returns immediately when stdout is not a tty, but helm was still
    # writing into a temp file that a successful run deleted — so --show-progress in CI
    # produced no install output at all.
    settings.show_progress = True
    monkeypatch.setattr(helm_ops, "resolve_chart_source", lambda s: None)
    # rich exposes is_terminal as a read-only property, so the class is the only seam.
    monkeypatch.setattr(type(helm_ops.out), "is_terminal", property(lambda self: False))
    monkeypatch.setattr(
        helm_ops, "_install_with_progress_ui", lambda s, cmd: pytest.fail("used the progress UI")
    )
    streamed = []
    monkeypatch.setattr(helm_ops, "stream", lambda cmd: streamed.append(cmd) or 0)
    monkeypatch.setattr(helm_ops, "helm", lambda *a, **k: Result(1, ""))

    helm_ops.helm_install(settings)

    assert streamed, "helm was never streamed to the terminal"


# ------------------------------------------------------------------------------------------
# The pull secret was deleted before being re-applied
# ------------------------------------------------------------------------------------------


def test_an_existing_pull_secret_is_updated_without_being_deleted_first(
    monkeypatch, settings, recorder
):
    # Left over from the `kubectl create secret` era, which refused to overwrite. `apply`
    # updates in place, so the delete only opened a window with no credentials — and lost
    # them entirely if anything failed before the re-create.
    rec = recorder()
    monkeypatch.setattr(registry, "kubectl", rec)
    settings.local_registry = True
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"

    registry.create_registry_secret(settings)

    assert not rec.ran("delete secret")
    assert rec.ran("apply")


class _ImmutableOnFirstApply(Recorder):
    """Fails the first `apply` the way the API server rejects a Secret type change."""

    def __call__(self, *args, **kwargs):
        result = super().__call__(*args, **kwargs)
        if self.calls[-1][:1] == ["apply"] and len(self.argv_containing("apply")) == 1:
            return Result(1, "", 'Secret "registry-credentials" is invalid: type: immutable')
        return result


def test_a_secret_that_cannot_be_updated_in_place_is_replaced(monkeypatch, settings):
    # A name already taken by a Secret of another type cannot be applied over, because
    # `type` is immutable — that case, and only that case, still warrants the delete.
    rec = _ImmutableOnFirstApply()
    monkeypatch.setattr(registry, "kubectl", rec)
    settings.local_registry = True
    settings.local_registry_url = "local-registry.mlrun.svc.cluster.local:5000"

    registry.create_registry_secret(settings)

    assert rec.ran("delete secret")
    assert len(rec.argv_containing("apply")) == 2
