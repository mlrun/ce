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
"""An upgrade keeps the release's NodePorts and warns about every other value it changes."""

import json
import stat

import pytest
import yaml

from ce_installer import helm_ops, release_values
from ce_installer.shell import Result

PREVIOUS = {
    "global": {"registry": {"url": "index.docker.io/old", "secretName": "registry-credentials"}},
    "jupyterNotebook": {"service": {"nodePort": 30041}},
    "storage": {"s3": {"secretKey": "hunter2"}},
    "mlrunMigrations": {"enabled": False},
}


@pytest.fixture
def upgrade(monkeypatch, recorder, settings):
    """A release that exists and was installed with PREVIOUS."""

    def install(previous=PREVIOUS):
        helm = recorder(answers={"get values": Result(0, json.dumps(previous))})
        monkeypatch.setattr(release_values, "helm", helm)
        return helm

    return install


def test_a_first_install_has_nothing_to_compare(monkeypatch, recorder, settings, capture_logs):
    monkeypatch.setattr(
        release_values, "helm", recorder(default=Result(1, "", "Error: release: not found"))
    )
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    assert settings.previous_values is None
    assert settings.node_ports == {}
    assert not release_values.previous_values_file(settings).exists()
    assert not release_values.value_changes_file(settings).exists()
    assert logs == []


def test_an_upgrade_saves_the_current_values_readable_by_the_user_only(upgrade, settings):
    upgrade()

    release_values.load_previous_values(settings)

    saved = release_values.previous_values_file(settings)
    assert yaml.safe_load(saved.read_text()) == PREVIOUS
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600, "it can hold secrets the user set"


def test_an_upgrade_keeps_the_nodeports_the_release_has(upgrade, settings):
    upgrade()

    release_values.load_previous_values(settings)

    assert settings.node_ports == {"jupyterNotebook.service.nodePort": 30041}


def test_a_nodeport_in_the_values_file_wins_over_the_release(upgrade, settings, tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("jupyterNotebook:\n  service:\n    nodePort: 31040\n")
    settings.values_file = str(values)
    upgrade()

    release_values.load_previous_values(settings)

    assert settings.node_ports == {}


def test_every_changed_value_is_warned_about_and_written_down(upgrade, settings, capture_logs):
    settings.registry_url = "index.docker.io/new"
    settings.registry_secret_name = "registry-credentials"
    settings.external_host_address = "localhost"
    upgrade()
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    text = "\n".join(logs)
    assert "global.registry.url: index.docker.io/old -> index.docker.io/new" in text
    assert "global.externalHostAddress: (unset) -> localhost" in text
    assert "storage.s3.secretKey: <redacted> -> (unset)" in text
    assert "hunter2" not in text
    assert "jupyterNotebook" not in text, "the carried-over port is not a change"
    assert settings.value_changes == 3

    written = yaml.safe_load(release_values.value_changes_file(settings).read_text())
    assert [change["key"] for change in written["changes"]] == [
        "global.externalHostAddress",
        "global.registry.url",
        "storage.s3.secretKey",
    ]
    assert "hunter2" not in release_values.value_changes_file(settings).read_text()


@pytest.mark.parametrize("key", ["clientSecret", "accountKey", "connectionString", "sasToken"])
def test_azure_credentials_are_redacted_too(upgrade, settings, capture_logs, key):
    # Only the S3 key names matched, so the chart's Azure credentials were printed.
    upgrade({"storage": {"azure": {key: "hunter2"}}})
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    assert f"storage.azure.{key}: <redacted> -> (unset)" in "\n".join(logs)
    assert "hunter2" not in "\n".join(logs)
    assert "hunter2" not in release_values.value_changes_file(settings).read_text()


def test_a_registry_change_warns_that_existing_functions_keep_the_old_one(
    upgrade, settings, capture_logs
):
    # Each function stores its registry; a redeploy after the upgrade still used the old one.
    settings.registry_url = "registry.host.docker.internal"
    upgrade()
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    warning = next(line for line in logs if "Functions created before" in line)
    assert "index.docker.io/old" in warning
    assert "registry.host.docker.internal" in warning


def test_a_first_registry_value_is_not_a_registry_change(upgrade, settings, capture_logs):
    settings.registry_url = "index.docker.io/new"
    upgrade({"mlrunMigrations": {"enabled": False}})
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    assert not any("Functions created before" in line for line in logs)


def test_a_dry_run_points_at_every_file_it_left(
    monkeypatch, upgrade, settings, capture_logs, tmp_path
):
    log = tmp_path / "mlrun-ce-install.log"
    log.write_text("rendered manifests\n")
    monkeypatch.setattr(helm_ops, "debug_log_path", lambda: str(log))
    settings.registry_url = "index.docker.io/new"
    upgrade()
    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)
    helm_ops.write_installer_values(settings)
    logs = capture_logs(helm_ops)

    helm_ops.report_dry_run_files(settings)

    text = "\n".join(logs)
    for path in (
        log,
        tmp_path / "mlrun-ce-values.yaml",
        release_values.previous_values_file(settings),
        release_values.value_changes_file(settings),
    ):
        assert str(path) in text


def test_an_upgrade_that_changes_nothing_removes_a_stale_changes_file(
    upgrade, settings, capture_logs, tmp_path
):
    # -f-only mode, so the installer adds nothing of its own but the carried-over port.
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    settings.values_file = str(values)
    stale = release_values.value_changes_file(settings)
    stale.write_text("changes: []\n")
    upgrade(
        {"jupyterNotebook": {"service": {"nodePort": 30041}}, "mlrunMigrations": {"enabled": False}}
    )
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    assert not stale.exists()
    assert any("no change" in line for line in logs)
    assert settings.value_changes == 0


def test_migrations_run_on_the_upgrade_that_asks_and_are_off_in_the_saved_file(
    upgrade, settings, capture_logs
):
    settings.trigger_migrations = True
    upgrade()
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)
    saved = helm_ops.write_installer_values(settings)
    cmd = helm_ops.build_helm_install_command(settings, saved)

    assert "mlrunMigrations.enabled: false -> true" in "\n".join(logs)
    # A later plain `helm upgrade -f mlrun-ce-values.yaml` must not run them again.
    assert yaml.safe_load(open(saved))["mlrunMigrations"] == {"enabled": False}
    assert cmd[cmd.index(saved) + 1 : cmd.index(saved) + 3] == [
        "--set",
        "mlrunMigrations.enabled=true",
    ]


def test_migrations_stay_off_unless_asked(settings):
    flags = helm_ops.build_set_flags(settings)
    cmd = helm_ops.build_helm_install_command(settings, "values.yaml")

    assert flags[-2:] == ["--set", "mlrunMigrations.enabled=false"]
    assert "mlrunMigrations.enabled=true" not in cmd
