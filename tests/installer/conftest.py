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
"""Shared fixtures for the installer suites.

Two things every test here needs: an environment that cannot leak in, and a way to see
what the installer would have run without running it.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ce_installer.settings import Settings  # noqa: E402
from ce_installer.shell import Result  # noqa: E402

# Every tunable the installer reads from the environment. A developer who exported
# KUBE_CONTEXT to drive a real cluster would otherwise silently change what these tests
# exercise — that exact leak made a bash test fail in a way that looked like a code
# regression. Autouse, so no suite can forget it.
INSTALLER_ENV_VARS = (
    "NAMESPACE",
    "RELEASE_NAME",
    "REGISTRY_SECRET_NAME",
    "HELM_REPO_URL",
    "HELM_TIMEOUT",
    "REGISTRY_USERNAME",
    "REGISTRY_PASSWORD",
    "REGISTRY_PASSWORD_FILE",
    "REGISTRY_SERVER",
    "REGISTRY_EMAIL",
    "REGISTRY_URL",
    "EXTERNAL_HOST_ADDRESS",
    "KUBE_CONTEXT",
    "MLRUN_VERSION",
    "NUCLIO_VERSION",
    "MIN_K8S_VERSION",
    "MIN_HELM_VERSION",
    "PROGRESS_INTERVAL_SEC",
    "SKIP_REGISTRY_SECRET",
    "SKIP_VALIDATORS",
    "SHOW_PROGRESS",
    "UNINSTALL",
    "HARD_CLEAN",
    "DISABLE_SYSTEM_MONITORING",
    "DISABLE_SPARK",
    "DISABLE_MPI",
    "DISABLE_MODEL_MONITORING",
    "ENABLE_INGRESS",
    "INGRESS_CLASS",
    "ENABLE_OTEL_OPERATOR",
    "ENABLE_OTEL_COLLECTOR",
    "ENABLE_OTEL_NAMESPACE_LABEL",
    "ENABLE_OTEL_INSTRUMENTATION",
    "LOCAL_REGISTRY",
    "CE_VERSION",
    "CHART_PATH",
    "SKIP_DEPENDENCY_UPDATE",
    "DRY_RUN",
    "NON_INTERACTIVE",
    "CONFIG_FILE",
    "CI",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in INSTALLER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings():
    """Settings with every field pinned, so nothing depends on the ambient environment."""
    return Settings(
        namespace="mlrun",
        release_name="mlrun-ce",
        registry_secret_name="registry-credentials",
        helm_repo_url="https://mlrun.github.io/ce",
        helm_timeout="960s",
        kube_context="",
        external_host_address="",
    )


class Recorder:
    """Stands in for the kubectl/helm/run wrappers: records argv, replays canned answers.

    `answers` maps a substring of the joined argv to the Result to return; the first match
    wins and anything unmatched returns success with no output. Patch this onto the module
    under test (`cluster.kubectl`), not onto `shell` — each module imports the wrappers
    into its own namespace and holds its own reference.
    """

    def __init__(self, answers=None, default=None):
        self.calls = []
        self.answers = answers or {}
        self.default = default if default is not None else Result(0, "")

    def __call__(self, *args, **kwargs):
        # kubectl(settings, *args) for the wrappers; run(argv) for plain commands.
        if args and isinstance(args[0], Settings):
            argv = list(args[1:])
        else:
            argv = list(args[0])
        self.calls.append(argv)
        for needle, answer in self.answers.items():
            if needle in " ".join(argv):
                return answer
        return self.default

    def argv_containing(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]

    def ran(self, needle) -> bool:
        return bool(self.argv_containing(needle))

    @property
    def joined(self):
        return [" ".join(c) for c in self.calls]


@pytest.fixture
def recorder():
    return Recorder


@pytest.fixture
def capture_logs(monkeypatch):
    """Collect log_info/log_warn/log_error text from a module, without rich formatting.

    Returns a callable: `logs = capture_logs(validators)` then assert over `logs`.
    """

    def install(module):
        messages = []
        for name in ("log_info", "log_warn", "log_error"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, messages.append)
        return messages

    return install
