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
"""An upgrade saves the release's values and warns about every value it changes."""

import json
import stat

import pytest
import yaml

from ce_installer import helm_ops, release_values
from ce_installer.shell import Result

PREVIOUS = {
    "global": {"registry": {"url": "index.docker.io/old", "secretName": "registry-credentials"}},
    "storage": {"s3": {"secretKey": "hunter2"}},
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
    assert not release_values.previous_values_file(settings).exists()
    assert not release_values.value_changes_file(settings).exists()
    assert logs == []


def test_an_upgrade_saves_the_current_values_readable_by_the_user_only(upgrade, settings):
    upgrade()

    release_values.load_previous_values(settings)

    saved = release_values.previous_values_file(settings)
    assert yaml.safe_load(saved.read_text()) == PREVIOUS
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600, "it can hold secrets the user set"


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
    assert settings.value_changes == 3

    written = yaml.safe_load(release_values.value_changes_file(settings).read_text())
    assert [change["key"] for change in written["changes"]] == [
        "global.externalHostAddress",
        "global.registry.url",
        "storage.s3.secretKey",
    ]
    assert "hunter2" not in release_values.value_changes_file(settings).read_text()


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
    # -f-only mode, so the installer adds nothing of its own.
    values = tmp_path / "values.yaml"
    values.write_text("{}\n")
    settings.values_file = str(values)
    stale = release_values.value_changes_file(settings)
    stale.write_text("changes: []\n")
    upgrade({})
    logs = capture_logs(release_values)

    release_values.load_previous_values(settings)
    release_values.report_value_changes(settings)

    assert not stale.exists()
    assert any("no change" in line for line in logs)
    assert settings.value_changes == 0
