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
"""The registry pull secret: where its password comes from, and --skip-secret's precondition.

Ports install_tests.bats 1151, 1167, 1184, 1214 and 1226. The password resolution order is
the part worth pinning hardest — REGISTRY_PASSWORD_FILE exists so CI systems can hand the
installer a mounted file instead of an exported variable, and getting the precedence or the
trailing-newline handling wrong produces a secret that fails registry auth much later, in a
pod's image pull, with nothing pointing back here.

The in-cluster registry (deploy_local_registry) is covered in test_regressions.py.
"""

import pytest

from ce_installer import registry
from ce_installer.console import InstallerError
from ce_installer.shell import Result


def prepare(monkeypatch, settings):
    """Supply everything create_registry_secret needs apart from the password."""
    monkeypatch.setenv("REGISTRY_USERNAME", "myuser")
    monkeypatch.setenv("REGISTRY_EMAIL", "me@example.com")
    # Pinned to empty rather than deleted, for two reasons: env_str() mirrors bash's
    # ${VAR:-}, so "" already reads as unset, and routing it through monkeypatch guarantees
    # the value create_registry_secret writes into os.environ is undone before the next test
    # (a leak here would make the "file is read" case pass for the wrong reason).
    monkeypatch.setenv("REGISTRY_PASSWORD", "")
    settings.non_interactive = True


# ------------------------------------------------------------------------------------------
# Password resolution: env > file > prompt (bats 1151, 1167, 1184)
# ------------------------------------------------------------------------------------------


def test_password_is_read_from_the_password_file_when_the_env_var_is_unset(
    monkeypatch, settings, tmp_path
):
    password_file = tmp_path / "pw.txt"
    password_file.write_text("supersecret\n")
    prepare(monkeypatch, settings)
    settings.registry_password_file = str(password_file)
    settings.dry_run = True

    registry.create_registry_secret(settings)

    # The trailing newline every editor and `echo` leaves at EOF is not part of the
    # password; carrying it through produces a 401 from the registry at image-pull time.
    assert settings.registry_password_value == "supersecret"


def test_password_file_windows_line_ending_is_stripped(monkeypatch, settings, tmp_path):
    password_file = tmp_path / "pw-crlf.txt"
    password_file.write_text("supersecret\r\n")
    prepare(monkeypatch, settings)
    settings.registry_password_file = str(password_file)
    settings.dry_run = True

    registry.create_registry_secret(settings)

    # A file authored on Windows, or mounted from a Secret created there, is the same case
    # as above but leaves a \r that is invisible in every log and error message.
    assert settings.registry_password_value == "supersecret"


def test_password_env_var_wins_over_the_password_file(monkeypatch, settings, tmp_path):
    password_file = tmp_path / "pw2.txt"
    password_file.write_text("fromfile\n")
    prepare(monkeypatch, settings)
    monkeypatch.setenv("REGISTRY_PASSWORD", "fromenv")
    settings.registry_password_file = str(password_file)
    settings.dry_run = True

    registry.create_registry_secret(settings)

    # Both set at once is the "override what the CI mount provides" case, so the explicit
    # variable has to win. The file must not even be consulted.
    assert settings.registry_password_value == "fromenv"


def test_missing_password_file_is_rejected_naming_the_variable_and_the_path(
    monkeypatch, settings, tmp_path
):
    missing = tmp_path / "nonexistent" / "pw.txt"
    prepare(monkeypatch, settings)
    settings.registry_password_file = str(missing)
    settings.dry_run = True

    with pytest.raises(InstallerError) as excinfo:
        registry.create_registry_secret(settings)

    # Falling back to the prompt (or to an empty password) would turn a typo'd mount path
    # into a secret containing nothing, discovered only when a pod cannot pull.
    assert "REGISTRY_PASSWORD_FILE" in excinfo.value.message
    assert str(missing) in excinfo.value.message


# ------------------------------------------------------------------------------------------
# Where the password is allowed to appear
# ------------------------------------------------------------------------------------------


def test_registry_password_reaches_only_the_secret_creation_call(monkeypatch, settings, recorder):
    prepare(monkeypatch, settings)
    monkeypatch.setenv("REGISTRY_PASSWORD", "supersecret")
    rec = recorder(answers={"get secret": Result(1, "NotFound")})
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.create_registry_secret(settings)

    # The password is an argv element of `kubectl create secret docker-registry` — so it is
    # visible in the process table for the life of that one call (true of install.sh too;
    # the leak-free form is --docker-password-stdin, which kubectl does not offer, or
    # `create secret generic --from-file`). This pins the blast radius to that single call:
    # no other kubectl invocation, and nothing the installer composes later, may carry it.
    carrying = [call for call in rec.calls if "supersecret" in " ".join(call)]
    assert len(carrying) == 1, f"password appeared in {len(carrying)} calls: {rec.joined}"
    assert carrying[0][:3] == ["create", "secret", "docker-registry"]


def test_registry_password_never_appears_in_log_output(
    monkeypatch, settings, recorder, capture_logs
):
    prepare(monkeypatch, settings)
    monkeypatch.setenv("REGISTRY_PASSWORD", "supersecret")
    monkeypatch.setattr(registry, "kubectl", recorder())
    logs = capture_logs(registry)

    registry.create_registry_secret(settings)

    # Installer output routinely gets pasted into tickets and CI logs, which outlive the
    # process table by a long way.
    assert not [message for message in logs if "supersecret" in message]


# ------------------------------------------------------------------------------------------
# --skip-secret's precondition (bats 1214, 1226)
# ------------------------------------------------------------------------------------------


def test_skip_secret_without_an_existing_secret_is_rejected(
    monkeypatch, settings, recorder, capture_logs
):
    rec = recorder(answers={"get secret": Result(1, 'secrets "registry-credentials" not found')})
    monkeypatch.setattr(registry, "kubectl", rec)
    logs = capture_logs(registry)

    with pytest.raises(InstallerError):
        registry.verify_existing_registry_secret(settings)

    # Proceeding would produce a release whose pods all fail to pull, ~20 pods deep into an
    # otherwise successful install. Failing up front, naming the secret and the namespace,
    # is the difference between a one-line fix and a debugging session.
    assert any("does not exist" in message for message in logs)
    assert any(settings.registry_secret_name in message for message in logs)


def test_skip_secret_with_an_existing_secret_passes_silently(monkeypatch, settings, recorder):
    rec = recorder()
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.verify_existing_registry_secret(settings)

    # --skip-secret means "the secret is mine, leave it alone" — the check may look, and
    # must not delete, replace or otherwise touch it.
    assert rec.joined == [
        f"get secret {settings.registry_secret_name} --namespace {settings.namespace}"
    ]
