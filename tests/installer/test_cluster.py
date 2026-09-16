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
"""Chart-source selection, namespace creation, and the KUBE_CONTEXT-aware wrappers.

Ports install_tests.bats 435, 444, 456, 471, 483 and 1239. Everything here decides *which*
cluster and *which* chart an install lands on, so a wrong answer is not a cosmetic one: a
swapped context flag installs into the wrong cluster, and a chart-source mistake installs
a different chart than the operator asked for.

The dependency-resolution tests have no bats counterpart — `--chart-path` preferring
`helm dependency build`, skipping the fetch when `charts/` already satisfies the lock, and
obeying `--skip-dependency-update` are deliberate divergences from install.sh (which always
ran `helm dependency update`), recorded in scripts/AGENTS.md.
"""

import pytest

from ce_installer import cluster, helm_ops
from ce_installer.console import InstallerError
from ce_installer.shell import Result, helm_cmd, kubectl_cmd

# A subset of the real charts/mlrun-ce/requirements.lock, kept because strimzi is the
# dependency whose tarball is not named after it.
LOCK_DEPS = (("nuclio", "0.21.27"), ("strimzi-kafka-operator", "0.48.0"))
VENDORED_TARBALLS = ("nuclio-0.21.27.tgz", "strimzi-kafka-operator-helm-3-chart-0.48.0.tgz")


def make_chart(tmp_path, name="fakechart", deps=None, vendored=()):
    """Build a chart directory: Chart.yaml, optionally a requirements.lock and charts/."""
    chart_dir = tmp_path / name
    chart_dir.mkdir()
    (chart_dir / "Chart.yaml").write_text("apiVersion: v1\nname: mlrun-ce\nversion: 0.0.1\n")

    if deps is not None:
        lines = ["dependencies:"]
        for dep_name, version in deps:
            lines += [f"- name: {dep_name}", f"  version: {version}"]
        (chart_dir / "requirements.lock").write_text("\n".join(lines) + "\n")

    if vendored:
        (chart_dir / "charts").mkdir()
        for tarball in vendored:
            (chart_dir / "charts" / tarball).write_text("")

    return chart_dir


# ------------------------------------------------------------------------------------------
# Chart source selection (bats 435, 444, 456, 471, 483)
# ------------------------------------------------------------------------------------------


def test_missing_chart_path_directory_is_rejected(monkeypatch, settings, tmp_path, recorder):
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(tmp_path / "nonexistent")

    with pytest.raises(InstallerError) as excinfo:
        cluster.resolve_chart_source(settings)

    # Falling through to the published repo instead of failing would install a chart the
    # operator never pointed at — a typo'd path has to stop the run, not silently redirect.
    assert not rec.calls
    # A path that is not there at all and a directory that is not a chart need different
    # answers. Reporting the missing Chart.yaml for a typo'd path sends the reader looking
    # inside a directory that does not exist.
    assert "Chart path not found" in excinfo.value.message
    assert settings.chart_path in excinfo.value.message


def test_chart_path_without_chart_yaml_is_rejected(monkeypatch, settings, tmp_path, recorder):
    empty = tmp_path / "emptychart"
    empty.mkdir()
    monkeypatch.setattr(cluster, "helm", recorder())
    settings.chart_path = str(empty)

    with pytest.raises(InstallerError) as excinfo:
        cluster.resolve_chart_source(settings)

    # The common mistake is pointing one level too high (the repo root, or charts/), so the
    # message has to name what was looked for rather than just refusing.
    assert "Chart.yaml" in excinfo.value.message


def test_valid_chart_path_becomes_the_chart_ref(monkeypatch, settings, tmp_path, recorder):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)
    monkeypatch.setattr(cluster, "helm", recorder())
    settings.chart_path = str(chart_dir)

    cluster.resolve_chart_source(settings)

    assert settings.chart_ref == str(chart_dir)


def test_local_chart_path_does_not_add_the_published_repo(
    monkeypatch, settings, tmp_path, recorder
):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(chart_dir)

    cluster.resolve_chart_source(settings)

    # `helm repo add`/`update` reach the network. A local-path install is the air-gapped and
    # "test my working copy" case; contacting mlrun.github.io anyway defeats both.
    assert not rec.ran("repo add")
    assert not rec.ran("repo update")


def test_published_repo_mode_uses_the_published_chart_ref(monkeypatch, settings, recorder):
    monkeypatch.setattr(cluster, "helm", recorder())
    settings.chart_path = ""

    cluster.resolve_chart_source(settings)

    # The default install: no --chart-path means the released chart, not whatever checkout
    # the installer happens to be running from.
    assert settings.chart_ref == "mlrun-ce/mlrun-ce"


def test_published_repo_mode_registers_the_repo_before_using_it(monkeypatch, settings, recorder):
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = ""

    cluster.resolve_chart_source(settings)

    # Without the add, `mlrun-ce/mlrun-ce` resolves against whatever the user already has
    # registered under that name; without the update, a stale cache hides new versions.
    assert rec.argv_containing("repo add")[0] == [
        "repo",
        "add",
        "mlrun-ce",
        settings.helm_repo_url,
    ]
    assert rec.ran("repo update")


def test_ce_version_in_local_path_mode_warns_that_it_is_ignored(
    monkeypatch, settings, tmp_path, recorder, capture_logs
):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)
    monkeypatch.setattr(cluster, "helm", recorder())
    logs = capture_logs(cluster)
    settings.chart_path = str(chart_dir)
    settings.ce_version = "0.11.0"

    cluster.resolve_chart_source(settings)

    # Both flags together are contradictory, and the loser is --ce-version. Silently
    # dropping it would let someone believe they had pinned a version they had not.
    assert any("--ce-version is ignored" in message for message in logs)


def test_ce_version_does_not_reach_helm_in_local_path_mode(settings, tmp_path):
    settings.chart_path = str(make_chart(tmp_path))
    settings.chart_ref = settings.chart_path
    settings.ce_version = "0.11.0"

    cmd = helm_ops.build_helm_install_command(settings)

    # A local directory has no versions to choose between; helm rejects --version against
    # one, so passing it through would turn a harmless contradiction into a failed install.
    assert "--version" not in cmd


def test_ce_version_still_reaches_helm_in_published_repo_mode(settings):
    settings.chart_ref = "mlrun-ce/mlrun-ce"
    settings.ce_version = "0.11.0"

    cmd = helm_ops.build_helm_install_command(settings)

    # The other half of the same condition: suppressing --ce-version everywhere would make
    # the flag silently dead, which is how pinning a release stops working unnoticed.
    assert cmd[cmd.index("--version") + 1] == "0.11.0"


# ------------------------------------------------------------------------------------------
# Chart dependency resolution — no bats equivalent, deliberate divergence from install.sh
# ------------------------------------------------------------------------------------------


def test_chart_dependencies_are_built_rather_than_updated(
    monkeypatch, settings, tmp_path, recorder
):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(chart_dir)

    cluster.resolve_chart_source(settings)

    # `update` re-resolves requirements.yaml and rewrites the lock — a maintainer action.
    # An installer run is a consumer, and must get the versions the lock names.
    assert rec.argv_containing("dependency build")
    assert not rec.ran("dependency update")


def test_chart_without_a_lock_falls_back_to_dependency_update(
    monkeypatch, settings, tmp_path, recorder
):
    chart_dir = make_chart(tmp_path)
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(chart_dir)

    cluster.resolve_chart_source(settings)

    # `helm dependency build` errors out with no lock to build from, so the unlocked chart
    # still needs the resolving form.
    assert rec.ran("dependency update")


def test_vendored_dependencies_matching_the_lock_skip_the_fetch(
    monkeypatch, settings, tmp_path, recorder
):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS, vendored=VENDORED_TARBALLS)
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(chart_dir)

    cluster.resolve_chart_source(settings)

    # The whole point is that an egress-restricted host (a pod, an air-gapped machine) can
    # install from a fully vendored checkout. Any dependency call here reaches the network
    # and fails the run with nothing left to download.
    assert not rec.ran("dependency")
    assert settings.chart_ref == str(chart_dir)


def test_skip_dependency_update_never_fetches_even_when_nothing_is_vendored(
    monkeypatch, settings, tmp_path, recorder
):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)
    rec = recorder()
    monkeypatch.setattr(cluster, "helm", rec)
    settings.chart_path = str(chart_dir)
    settings.skip_dependency_update = True

    cluster.resolve_chart_source(settings)

    # An explicit opt-out has to win over the automatic check, or the flag only works in
    # the case where it was not needed.
    assert not rec.ran("dependency")


def test_renamed_dependency_tarball_still_counts_as_vendored(tmp_path):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS, vendored=VENDORED_TARBALLS)

    # strimzi-kafka-operator 0.48.0 ships as strimzi-kafka-operator-helm-3-chart-0.48.0.tgz.
    # Matching on an exact `<name>-<version>.tgz` filename would call a complete charts/
    # directory incomplete and re-fetch on every run.
    assert cluster.chart_deps_satisfied(chart_dir)


def test_partially_vendored_dependencies_still_trigger_a_fetch(tmp_path):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS, vendored=VENDORED_TARBALLS[:1])

    # Installing with a subchart missing fails deep inside helm's template step; the fetch
    # has to be the thing that notices.
    assert not cluster.chart_deps_satisfied(chart_dir)


def test_vendored_dependency_at_the_wrong_version_triggers_a_fetch(tmp_path):
    stale = ("nuclio-0.20.0.tgz", *VENDORED_TARBALLS[1:])
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS, vendored=stale)

    # A leftover tarball from an older checkout is the likeliest way charts/ goes stale, and
    # it is exactly the case where "some nuclio is present" is not good enough.
    assert not cluster.chart_deps_satisfied(chart_dir)


def test_chart_without_a_charts_directory_is_not_considered_vendored(tmp_path):
    chart_dir = make_chart(tmp_path, deps=LOCK_DEPS)

    assert not cluster.chart_deps_satisfied(chart_dir)


# ------------------------------------------------------------------------------------------
# The KUBE_CONTEXT-aware wrappers (bats 1239)
# ------------------------------------------------------------------------------------------


def test_kubectl_wrapper_targets_the_selected_context(settings):
    settings.kube_context = "myctx"

    assert kubectl_cmd(settings, "get", "pods") == ["kubectl", "--context", "myctx", "get", "pods"]


def test_helm_wrapper_targets_the_selected_context(settings):
    settings.kube_context = "myctx"

    # kubectl spells it --context and helm spells it --kube-context. Swapping them makes
    # every remote install fail on an unknown flag; worse, a flag one tool silently accepts
    # would install into whatever cluster the kubeconfig currently points at.
    assert helm_cmd(settings, "list") == ["helm", "--kube-context", "myctx", "list"]


def test_wrappers_pass_no_context_flag_when_none_is_selected(settings):
    settings.kube_context = ""

    # An empty --context is not the same as no --context: kubectl reads it as a request for
    # a context literally named "", which fails rather than using the current one.
    assert kubectl_cmd(settings, "get", "pods") == ["kubectl", "get", "pods"]
    assert helm_cmd(settings, "list") == ["helm", "list"]


# ------------------------------------------------------------------------------------------
# Namespace creation
# ------------------------------------------------------------------------------------------


def test_namespace_is_created_when_it_does_not_exist(monkeypatch, settings, recorder):
    rec = recorder(answers={"get namespace": Result(1, 'namespaces "mlrun" not found')})
    monkeypatch.setattr(cluster, "kubectl", rec)

    cluster.ensure_namespace(settings)

    assert rec.argv_containing("create namespace")[0] == ["create", "namespace", "mlrun"]


def test_existing_namespace_is_not_recreated(monkeypatch, settings, recorder):
    rec = recorder()
    monkeypatch.setattr(cluster, "kubectl", rec)

    cluster.ensure_namespace(settings)

    # `kubectl create namespace` on an existing namespace exits non-zero (AlreadyExists),
    # which would abort a re-install into a namespace the user deliberately prepared.
    assert not rec.ran("create namespace")


def test_dry_run_does_not_create_the_namespace(monkeypatch, settings, recorder):
    rec = recorder(answers={"get namespace": Result(1, 'namespaces "mlrun" not found')})
    monkeypatch.setattr(cluster, "kubectl", rec)
    settings.dry_run = True

    cluster.ensure_namespace(settings)

    # A dry run must leave the cluster exactly as it found it — a stray empty namespace is
    # a real object someone has to clean up.
    assert not rec.ran("create namespace")
