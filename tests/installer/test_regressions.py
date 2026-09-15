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

import sys
from pathlib import Path

import pytest

# scripts/ is the package root; tests/installer/ -> tests/ -> repo root -> scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from ce_installer import cluster, helm_ops, registry, ui, validators
from ce_installer.settings import Settings
from ce_installer.shell import Result


class Recorder:
    """Stands in for the kubectl/helm/run wrappers, recording argv and replaying answers."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, *args, **kwargs):
        # The wrappers are called as kubectl(settings, *args); plain run() as run(argv).
        argv = list(args[1:]) if args and isinstance(args[0], Settings) else list(args[0])
        self.calls.append(argv)
        for needle, answer in self.answers.items():
            if needle in " ".join(argv):
                return answer
        return Result(0, "")

    def argv_containing(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


@pytest.fixture
def settings():
    """A Settings with the environment ignored, so a stray export cannot alter a result."""
    return Settings(
        namespace="mlrun",
        release_name="mlrun-ce",
        helm_timeout="960s",
        kube_context="",
        external_host_address="",
    )


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
