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
"""The pre-install validators, ported from tests/install_tests.bats' Phase 4 group.

The distinction these tests exist to pin down is blocking vs. advisory. Only the Helm
version and the default StorageClass may refuse an install; everything else reports and
returns True, and `run_validators` collects the refusals so a user fixes them in one pass.
A validator that quietly starts blocking turns a working cluster away, and a blocking one
that starts warning lets an install proceed to a failure helm reports far less clearly —
so every test here asserts the return value *and* the message, never just one.

Test names end in the symptom a user would report, matching test_regressions.py.
"""

import pytest

from ce_installer import cli, validators
from ce_installer.console import InstallerError
from ce_installer.settings import apply_version_floors
from ce_installer.shell import Result

from .conftest import Recorder

# The annotation keys reach kubectl with their dots backslash-escaped, because jsonpath
# would otherwise read them as field separators. Canned answers have to key off the
# escaped form the code actually sends: matching the unescaped spelling makes every lookup
# miss, the validator look permanently broken, and the beta-fallback test pass for the
# wrong reason. (The bash stub had the mirror image of this bug.)
DEFAULT_CLASS_ANNOTATION = r"storageclass\.kubernetes\.io/is-default-class"
BETA_CLASS_ANNOTATION = r"storageclass\.beta\.kubernetes\.io/is-default-class"

KUBELET_VERSION_QUERY = "nodeInfo.kubeletVersion"
MEMORY_QUERY = "allocatable.memory"
EPHEMERAL_STORAGE_QUERY = "allocatable.ephemeral-storage"


def log_text(logs):
    return "\n".join(logs)


def install_kubectl(monkeypatch, answers=None, default=None):
    """Patch the kubectl wrapper *on validators* and hand back the recorder.

    Patching `shell.kubectl` instead would record nothing and assert nothing: validators.py
    does `from .shell import kubectl`, so it holds its own reference and never looks the
    name up on `shell` again.
    """
    recorder = Recorder(answers=answers, default=default)
    monkeypatch.setattr(validators, "kubectl", recorder)
    return recorder


# ------------------------------------------------------------------------------------------
# Kubernetes version — informational on every path
# ------------------------------------------------------------------------------------------


def test_k8s_version_never_blocks_without_an_explicit_floor(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {KUBELET_VERSION_QUERY: Result(0, "v1.30.2")})

    assert validators.validate_k8s_version(settings) is True
    # The chart declares no kubeVersion and the README states no cluster version, so there
    # is nothing to enforce — an invented floor would refuse clusters the chart supports.
    assert "Kubernetes version: 1.30" in log_text(logs)
    assert "below" not in log_text(logs)


def test_k8s_version_reports_the_detected_version_with_no_floor_mentioned(
    monkeypatch, settings, capture_logs
):
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {KUBELET_VERSION_QUERY: Result(0, "v1.34.0")})

    assert validators.validate_k8s_version(settings) is True
    assert "Kubernetes version: 1.34" in log_text(logs)
    # "required" would advertise a floor that does not exist.
    assert "required" not in log_text(logs)


def test_k8s_version_warns_instead_of_failing_when_undetectable(
    monkeypatch, settings, capture_logs
):
    logs = capture_logs(validators)
    # An unreachable or RBAC-restricted `get nodes` is not a reason to refuse an install.
    install_kubectl(monkeypatch, default=Result(1, ""))

    assert validators.validate_k8s_version(settings) is True
    assert "Could not determine Kubernetes version" in log_text(logs)


def test_min_k8s_version_only_warns_when_the_cluster_is_below_it(
    monkeypatch, settings, capture_logs
):
    settings.min_k8s_version = "1.34"
    apply_version_floors(settings)
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {KUBELET_VERSION_QUERY: Result(0, "v1.30.2")})

    # MIN_K8S_VERSION exists to tighten an environment, not to hand the installer a veto.
    assert validators.validate_k8s_version(settings) is True
    assert "below the requested minimum (1.34)" in log_text(logs)


# ------------------------------------------------------------------------------------------
# Helm version — the one version floor that blocks
# ------------------------------------------------------------------------------------------


def test_helm_version_below_the_floor_blocks_the_install(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    monkeypatch.setattr(validators, "helm", Recorder(default=Result(0, "v3.5.0+g12345")))

    assert validators.validate_helm_version(settings) is False
    assert "below the minimum supported version" in log_text(logs)


def test_helm_version_at_the_floor_is_accepted(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    # Exactly 3.6, the chart README's stated prerequisite: an off-by-one here would refuse
    # a Helm the chart itself supports.
    monkeypatch.setattr(validators, "helm", Recorder(default=Result(0, "v3.6.0+g12345")))

    assert validators.validate_helm_version(settings) is True
    assert "Helm version: 3.6" in log_text(logs)


def test_min_helm_version_raises_the_enforced_helm_floor(monkeypatch, settings, capture_logs):
    settings.min_helm_version = "4.1"
    apply_version_floors(settings)
    logs = capture_logs(validators)
    monkeypatch.setattr(validators, "helm", Recorder(default=Result(0, "v3.9.0+g12345")))

    assert validators.validate_helm_version(settings) is False
    assert "below the minimum supported version" in log_text(logs)


# ------------------------------------------------------------------------------------------
# Version floors — parsed once, up front
# ------------------------------------------------------------------------------------------


def test_malformed_min_k8s_version_is_rejected_before_the_install_starts(settings):
    settings.min_k8s_version = "1"

    # Parsed up front rather than inside the validator, so a typo fails immediately instead
    # of after the namespace and registry secret already exist.
    with pytest.raises(InstallerError) as excinfo:
        apply_version_floors(settings)
    assert "MIN_K8S_VERSION must be in MAJOR.MINOR form" in str(excinfo.value)
    assert excinfo.value.code == 1


def test_empty_min_k8s_version_leaves_no_kubernetes_floor(settings):
    apply_version_floors(settings)

    # Empty is the default and must stay valid: a floor of "" is "no floor", not an error.
    assert settings.min_k8s_major is None
    assert settings.min_k8s_minor is None
    assert (settings.min_helm_major, settings.min_helm_minor) == (3, 6)


# ------------------------------------------------------------------------------------------
# Default StorageClass — blocking, the chart's PVCs have nowhere to land without one
# ------------------------------------------------------------------------------------------


def test_missing_default_storage_class_blocks_the_install(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {"get storageclass": Result(0, "standard=false=")})

    assert validators.validate_storage_class(settings) is False
    assert "No default StorageClass found" in log_text(logs)


def test_default_storage_class_is_found_and_named(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {"get storageclass": Result(0, "standard=false=\nfast=true=\n")})

    assert validators.validate_storage_class(settings) is True
    assert "Default StorageClass: fast" in log_text(logs)


def test_beta_default_class_annotation_still_counts_as_a_default(
    monkeypatch, settings, capture_logs
):
    logs = capture_logs(validators)
    # Keyed on the beta annotation actually appearing in the jsonpath, so dropping it from
    # the query makes this answer unreachable and the test fail. A fixed row would pass
    # either way. Kubernetes still honours the deprecated key, and clusters provisioned
    # years ago carry only that one.
    recorder = install_kubectl(
        monkeypatch, {BETA_CLASS_ANNOTATION: Result(0, "standard==\nlegacy==true\n")}
    )

    assert validators.validate_storage_class(settings) is True
    assert "Default StorageClass: legacy" in log_text(logs)
    assert recorder.ran(DEFAULT_CLASS_ANNOTATION), "the stable annotation must still be read"


# ------------------------------------------------------------------------------------------
# Ingress controller — advisory, --enable-ingress is bring-your-own-controller
# ------------------------------------------------------------------------------------------


def test_ingress_check_does_not_touch_the_cluster_without_enable_ingress(monkeypatch, settings):
    settings.enable_ingress = False
    recorder = install_kubectl(monkeypatch, default=Result(1, ""))

    assert validators.validate_ingress_controller(settings) is True
    assert recorder.calls == [], "the cluster was queried for an ingress nobody asked for"


def test_missing_ingress_class_warns_but_never_blocks(monkeypatch, settings, capture_logs):
    settings.enable_ingress = True
    settings.ingress_class = "nginx"
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, default=Result(1, ""))

    # The installer stopped installing ingress-nginx itself, so a missing controller is the
    # user's to resolve — blocking here would refuse an otherwise fine cluster.
    assert validators.validate_ingress_controller(settings) is True
    assert "no IngressClass named 'nginx' found" in log_text(logs)
    assert "does not install a controller" in log_text(logs)


def test_present_ingress_class_is_reported_as_found(monkeypatch, settings, capture_logs):
    settings.enable_ingress = True
    settings.ingress_class = "nginx"
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, default=Result(0, ""))

    assert validators.validate_ingress_controller(settings) is True
    assert "IngressClass 'nginx' found" in log_text(logs)


# ------------------------------------------------------------------------------------------
# Registry auth — best-effort docker login, never blocking
# ------------------------------------------------------------------------------------------


def forbid_docker_probe(monkeypatch):
    """Make any docker probe an outright failure, for the paths that must not reach one."""

    def explode():
        raise AssertionError("docker was probed on a path that is supposed to skip the check")

    # validators.docker_available, not shell's: shell's is lru_cached, so a patch there
    # would be bypassed by a cached answer another test already populated.
    monkeypatch.setattr(validators, "docker_available", explode)


def test_registry_auth_skipped_for_a_local_registry(monkeypatch, settings, capture_logs):
    settings.local_registry = True
    settings.registry_username_value = "alice"
    settings.registry_password_value = "secret"
    logs = capture_logs(validators)
    forbid_docker_probe(monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(validators, "run", recorder)

    assert validators.validate_registry_auth(settings) is True
    assert "skipped (--local-registry in use" in log_text(logs)
    assert recorder.calls == [], "logged in to an external registry that is not in use"


def test_registry_auth_skipped_when_no_credentials_were_resolved(
    monkeypatch, settings, capture_logs
):
    settings.local_registry = False
    logs = capture_logs(validators)
    forbid_docker_probe(monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr(validators, "run", recorder)

    # -f-only mode never resolves credentials; there is nothing to check, not a failure.
    assert validators.validate_registry_auth(settings) is True
    assert "skipped (no registry credentials resolved" in log_text(logs)
    assert recorder.calls == []


def test_failed_docker_login_warns_but_never_blocks(monkeypatch, settings, capture_logs):
    settings.local_registry = False
    settings.registry_username_value = "alice"
    settings.registry_password_value = "secret"
    settings.registry_server_value = "https://index.docker.io/v1/"
    logs = capture_logs(validators)
    monkeypatch.setattr(validators, "docker_available", lambda: True)
    monkeypatch.setattr(validators, "run", Recorder(default=Result(1, "unauthorized")))

    # The local docker daemon's credentials say nothing about what the cluster can pull —
    # blocking on this would refuse installs that work.
    assert validators.validate_registry_auth(settings) is True
    assert "could not log in" in log_text(logs)


def test_successful_docker_login_is_reported(monkeypatch, settings, capture_logs):
    settings.local_registry = False
    settings.registry_username_value = "alice"
    settings.registry_password_value = "secret"
    settings.registry_server_value = "https://index.docker.io/v1/"
    logs = capture_logs(validators)
    monkeypatch.setattr(validators, "docker_available", lambda: True)
    recorder = Recorder(default=Result(0, ""))
    monkeypatch.setattr(validators, "run", recorder)

    assert validators.validate_registry_auth(settings) is True
    assert "login to https://index.docker.io/v1/ succeeded" in log_text(logs)
    argv = recorder.calls[0]
    # The password goes over stdin; on argv it would show up in `ps` and in shell history.
    assert "--password-stdin" in argv
    assert "secret" not in argv


# ------------------------------------------------------------------------------------------
# NodePort conflicts — advisory
# ------------------------------------------------------------------------------------------


def test_nodeport_in_use_elsewhere_warns_with_the_conflicting_port(
    monkeypatch, settings, capture_logs
):
    settings.namespace = "mlrun"
    settings.release_name = "mlrun-ce"
    logs = capture_logs(validators)
    install_kubectl(
        monkeypatch,
        {"get svc": Result(0, "other-ns/something 30093\nmlrun/mlrun-ce 30010\n")},
    )

    assert validators.validate_nodeport_conflicts(settings) is True
    assert "NodePort conflict" in log_text(logs)
    assert "30093" in log_text(logs)
    # 30010 belongs to a previous revision of this same release — helm will adopt it, so
    # reporting it as a conflict would send a user chasing a non-problem.
    assert "30010" not in log_text(logs)


def test_nodeport_held_by_a_foreign_service_in_our_namespace_is_a_conflict(
    monkeypatch, settings, capture_logs
):
    settings.namespace = "mlrun"
    settings.release_name = "mlrun-ce"
    logs = capture_logs(validators)
    # Same namespace, but owned by nothing (a hand-made Service) and by another release.
    # install.sh skipped the whole namespace and called this clear, and the install then
    # failed on the port: helm will not adopt a Service it does not own.
    install_kubectl(
        monkeypatch,
        {"get svc": Result(0, "mlrun/ 30040\nmlrun/other-release 30050\n")},
    )

    assert validators.validate_nodeport_conflicts(settings) is True
    assert "30040" in log_text(logs)
    assert "30050" in log_text(logs)


def test_nodeport_check_reports_no_conflicts_when_ports_are_free(
    monkeypatch, settings, capture_logs
):
    settings.namespace = "mlrun"
    logs = capture_logs(validators)
    install_kubectl(monkeypatch, {"get svc": Result(0, "other-ns/something 40000\n")})

    assert validators.validate_nodeport_conflicts(settings) is True
    assert "no conflicts detected" in log_text(logs)


# ------------------------------------------------------------------------------------------
# Node capacity — advisory
# ------------------------------------------------------------------------------------------


def test_node_capacity_below_the_floor_warns_but_never_blocks(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    install_kubectl(
        monkeypatch,
        {
            MEMORY_QUERY: Result(0, "2000000Ki\n"),
            EPHEMERAL_STORAGE_QUERY: Result(0, "2000000Ki\n"),
        },
    )

    # 8Gi is a documented recommendation, not a hard requirement — a small cluster is
    # allowed to try.
    assert validators.validate_node_capacity(settings) is True
    assert "below the documented floor of 8Gi" in log_text(logs)


def test_node_capacity_above_the_floor_reports_the_totals(monkeypatch, settings, capture_logs):
    logs = capture_logs(validators)
    install_kubectl(
        monkeypatch,
        {
            MEMORY_QUERY: Result(0, "9000000Ki\n"),
            EPHEMERAL_STORAGE_QUERY: Result(0, "9000000Ki\n"),
        },
    )

    assert validators.validate_node_capacity(settings) is True
    assert "total allocatable memory ~8Gi" in log_text(logs)
    assert "total allocatable ephemeral storage ~8Gi" in log_text(logs)
    assert "below the documented floor" not in log_text(logs)


# ------------------------------------------------------------------------------------------
# run_validators — one exit, after every check has had its say
# ------------------------------------------------------------------------------------------


def test_run_validators_reports_every_blocking_failure_before_exiting_once(
    monkeypatch, settings, capture_logs
):
    logs = capture_logs(validators)
    # Old helm *and* no default StorageClass: both must be reported, so a user fixes the
    # cluster in one pass instead of discovering the second problem on the next run.
    install_kubectl(monkeypatch, {KUBELET_VERSION_QUERY: Result(0, "v1.30.0")})
    monkeypatch.setattr(validators, "helm", Recorder(default=Result(0, "v3.5.0+g12345")))
    monkeypatch.setattr(validators, "docker_available", lambda: False)

    with pytest.raises(InstallerError) as excinfo:
        validators.run_validators(settings)

    assert excinfo.value.code == 1
    assert "One or more required pre-install checks failed" in str(excinfo.value)
    assert "Helm version 3.5 is below" in log_text(logs)
    assert "No default StorageClass" in log_text(logs)
    # The cluster is 1.30 and informational-only, so it must not have contributed a failure.
    assert "Kubernetes version: 1.30" in log_text(logs)


# ------------------------------------------------------------------------------------------
# --skip-validators
# ------------------------------------------------------------------------------------------


def run_cli(monkeypatch, argv):
    """Drive cli.main() with every step around the validators stubbed out.

    Returns (exit code, the Settings that reached helm_install, the run_validators calls).
    """
    installed = []
    validator_runs = []

    for name in ("load_config", "check_requirements", "ensure_namespace", "gather_install_params"):
        monkeypatch.setattr(cli, name, lambda _settings: None)
    monkeypatch.setattr(cli, "create_registry_secret", lambda _settings: None)
    monkeypatch.setattr(cli, "run_validators", validator_runs.append)
    monkeypatch.setattr(cli, "helm_install", installed.append)

    code = cli.main(argv)
    return code, installed[0] if installed else None, validator_runs


def test_skip_validators_flag_reaches_settings(monkeypatch):
    code, settings, _ = run_cli(monkeypatch, ["--skip-validators"])

    assert code == 0
    assert settings is not None, "the install never reached helm"
    assert settings.skip_validators is True


def test_skip_validators_never_runs_the_validators(monkeypatch, capture_logs):
    logs = capture_logs(cli)
    code, _, validator_runs = run_cli(monkeypatch, ["--skip-validators"])

    assert code == 0
    # The escape hatch is only worth having if it actually bypasses the checks — a
    # validator that still ran would block the install the flag exists to force through.
    assert validator_runs == []
    assert "Skipping pre-install validators" in log_text(logs)


def test_validators_run_by_default(monkeypatch):
    _, _, validator_runs = run_cli(monkeypatch, [])

    # The other half of the flag: opt-out, never opt-in.
    assert len(validator_runs) == 1
