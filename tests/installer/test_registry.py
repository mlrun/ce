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

import base64
import json
import os

import pytest
import yaml

from ce_installer import registry
from ce_installer.console import InstallerError
from ce_installer.shell import Result


def prepare(monkeypatch, settings):
    """Supply everything create_registry_secret needs apart from the password."""
    monkeypatch.setenv("REGISTRY_USERNAME", "myuser")
    monkeypatch.setenv("REGISTRY_EMAIL", "me@example.com")
    # Pinned to empty rather than deleted: env_str() mirrors bash's ${VAR:-}, so "" already
    # reads as unset, and setting it explicitly documents that this case is about the file
    # being consulted, not about the variable happening to be absent.
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

    # Not in argv at all. install.sh passed it to `kubectl create secret docker-registry`,
    # which put it in the process table for the length of the call and in the text `run`
    # prints when a command fails. The secret is piped in as a manifest instead.
    carrying = [call for call in rec.calls if "supersecret" in " ".join(call)]
    assert not carrying, f"password appeared in argv: {rec.joined}"

    applied = [call for call in rec.calls if call[:3] == ["apply", "-f", "-"]]
    assert len(applied) == 1, f"expected one apply, got: {rec.joined}"


def test_password_from_a_file_is_not_exported_to_child_processes(
    monkeypatch, settings, recorder, tmp_path
):
    password_file = tmp_path / "pw.txt"
    password_file.write_text("fromfile\n")
    prepare(monkeypatch, settings)
    settings.registry_password_file = str(password_file)
    monkeypatch.setattr(registry, "kubectl", recorder())

    registry.create_registry_secret(settings)

    # This used to be assigned into os.environ to reach prompt_or_env, which handed it to
    # every helm, kubectl and docker subprocess the run starts afterwards. A file-supplied
    # password exists precisely so it is not in an environment anyone can read.
    assert os.environ.get("REGISTRY_PASSWORD") == ""
    assert settings.registry_password_value == "fromfile"


def test_the_secret_is_piped_as_a_manifest_kubectl_would_have_produced(
    monkeypatch, settings, recorder
):
    prepare(monkeypatch, settings)
    monkeypatch.setenv("REGISTRY_PASSWORD", "supersecret")
    monkeypatch.setenv("REGISTRY_SERVER", "https://index.docker.io/v1/")
    rec = recorder(answers={"get secret": Result(1, "NotFound")})
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.create_registry_secret(settings)

    applied = [
        text for call, text in zip(rec.calls, rec.inputs) if call[:3] == ["apply", "-f", "-"]
    ]
    manifest = yaml.safe_load(applied[0])
    assert manifest["type"] == "kubernetes.io/dockerconfigjson"
    assert manifest["metadata"]["name"] == settings.registry_secret_name

    # Byte-for-byte what `kubectl create secret docker-registry` writes, so a secret made
    # either way is interchangeable — including the auth field, which is the duplicate of
    # user:pass that registries actually read.
    payload = json.loads(base64.b64decode(manifest["data"][".dockerconfigjson"]))
    entry = payload["auths"]["https://index.docker.io/v1/"]
    assert entry["username"] == "myuser"
    assert entry["password"] == "supersecret"
    assert base64.b64decode(entry["auth"]).decode() == "myuser:supersecret"


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


# ------------------------------------------------------------------------------------------
# The CoreDNS patch, which edits a ConfigMap the whole cluster depends on
# ------------------------------------------------------------------------------------------

COREFILE = """.:53 {
    errors
    forward . /etc/resolv.conf
}
"""

# What `kubectl create configmap --dry-run=client -o yaml` hands back to be applied; only
# its emptiness is load-bearing, since a blank render now aborts the patch.
RENDERED_CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
"""


def test_an_unreadable_corefile_skips_the_patch_rather_than_emptying_it(
    monkeypatch, settings, recorder, capture_logs
):
    rec = recorder(
        answers={"get svc": Result(0, "10.96.0.10"), "get configmap coredns": Result(1, "")}
    )
    monkeypatch.setattr(registry, "kubectl", rec)
    logs = capture_logs(registry)

    registry.patch_coredns_for_registry(settings, "registry.example.com")

    # A failed read used to become Corefile="", which insert_hosts_entry returns unchanged,
    # so the installer applied an empty Corefile and restarted CoreDNS — taking DNS down
    # cluster-wide while logging "CoreDNS patched".
    assert not rec.ran("apply"), f"nothing may be applied: {rec.joined}"
    assert not rec.ran("rollout")
    assert any("skipping CoreDNS patch" in message for message in logs)


def test_an_empty_corefile_skips_the_patch(monkeypatch, settings, recorder, capture_logs):
    rec = recorder(
        answers={"get svc": Result(0, "10.96.0.10"), "get configmap coredns": Result(0, "   ")}
    )
    monkeypatch.setattr(registry, "kubectl", rec)
    logs = capture_logs(registry)

    registry.patch_coredns_for_registry(settings, "registry.example.com")

    assert not rec.ran("apply")
    assert any("skipping CoreDNS patch" in message for message in logs)


def test_a_corefile_with_nowhere_to_insert_skips_the_patch(
    monkeypatch, settings, recorder, capture_logs
):
    # Neither a hosts{} block nor a forward line, so insert_hosts_entry has no anchor and
    # returns the input untouched. Applying that would be a no-op ConfigMap write plus a
    # pointless CoreDNS restart, reported as success.
    rec = recorder(
        answers={
            "get svc": Result(0, "10.96.0.10"),
            "get configmap coredns": Result(0, ".:53 {\n    errors\n}\n"),
        }
    )
    monkeypatch.setattr(registry, "kubectl", rec)
    logs = capture_logs(registry)

    registry.patch_coredns_for_registry(settings, "registry.example.com")

    assert not rec.ran("apply")
    assert any("skipping CoreDNS patch" in message for message in logs)


def test_a_healthy_corefile_is_patched_and_coredns_restarted(monkeypatch, settings, recorder):
    rec = recorder(
        answers={
            "get svc": Result(0, "10.96.0.10"),
            "get configmap coredns": Result(0, COREFILE),
            "create configmap coredns": Result(0, RENDERED_CONFIGMAP),
        }
    )
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.patch_coredns_for_registry(settings, "registry.example.com")

    assert rec.ran("apply")
    assert rec.ran("rollout restart")


def test_a_coredns_configmap_that_cannot_be_written_is_not_reported_as_patched(
    monkeypatch, settings, recorder, capture_logs
):
    # Reading kube-system and writing it are different permissions. The apply used to go
    # unchecked, so a read-only user saw "CoreDNS patched" over an untouched ConfigMap and
    # then had to work out for themselves why pods could not resolve the registry.
    logs = capture_logs(registry)
    rec = recorder(
        answers={
            "get svc": Result(0, "10.96.0.10"),
            "get configmap coredns": Result(0, COREFILE),
            "create configmap coredns": Result(0, RENDERED_CONFIGMAP),
            "apply": Result(1, "", "Error from server (Forbidden)"),
        }
    )
    monkeypatch.setattr(registry, "kubectl", rec)

    registry.patch_coredns_for_registry(settings, "registry.example.com")

    assert not rec.ran("rollout restart")
    assert not any("CoreDNS patched" in message for message in logs)
    assert any("Could not update the CoreDNS ConfigMap" in message for message in logs)


def test_the_ingress_controller_is_looked_for_where_ingress_nginx_installs_itself(settings):
    # install.sh only looked in the release namespace, which is not where a stock
    # ingress-nginx lands, so the patch was skipped on most real clusters.
    assert registry.ingress_controller_candidates(settings)[0] == (
        "ingress-nginx/ingress-nginx-controller"
    )
    assert f"{settings.namespace}/ingress-nginx-controller" in (
        registry.ingress_controller_candidates(settings)
    )


def test_an_explicit_controller_service_is_the_only_one_tried(settings):
    # Traefik, a vendored controller or a non-standard namespace cannot be guessed at, so
    # the override replaces the candidates rather than being appended to them.
    settings.ingress_controller_service = "traefik-system/traefik"
    assert registry.ingress_controller_candidates(settings) == ["traefik-system/traefik"]


def test_the_second_candidate_is_tried_when_the_first_is_absent(monkeypatch, settings, recorder):
    rec = recorder(answers={"--namespace mlrun": Result(0, "10.96.0.44")})
    monkeypatch.setattr(registry, "kubectl", rec)

    assert registry.resolve_ingress_controller_ip(settings) == "10.96.0.44"
    assert len(rec.calls) == 2


def test_a_controller_reference_without_a_namespace_is_rejected(monkeypatch, settings, recorder):
    settings.ingress_controller_service = "just-a-name"
    monkeypatch.setattr(registry, "kubectl", recorder())

    with pytest.raises(InstallerError):
        registry.resolve_ingress_controller_ip(settings)
