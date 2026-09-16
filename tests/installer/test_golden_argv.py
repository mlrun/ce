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
"""End-to-end argv expectations: what the installer asks the cluster to do, for real runs.

This replaces the differential harness. That harness ran install.sh and install.py side by
side and compared the calls they made; once the bash script is gone there is no second
implementation to compare against, so the calls are recorded here instead. The expectations
in golden/ were generated while both still existed and were confirmed identical to the bash
output, so they carry the same authority the oracle did.

The unit suites patch the helm/kubectl wrappers and check one function. This runs the whole
program as a subprocess against stub binaries, so it catches what those cannot: a flag that
parses correctly but never reaches helm, a step that runs in the wrong order, an argument
dropped between layers.

Regenerate after an intentional change:

    make installer-test-golden-update

and read the diff as the reviewable part of your PR — every changed line is a change in what
the installer does to somebody's cluster.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).parent / "golden"
STUB = Path(__file__).parent / "stub.py"

# Inherited from the differential matrix. Add a case whenever a flag gains behaviour that
# reaches helm or kubectl. Refusals belong here too: how the installer declines is as much a
# contract as how it succeeds.
CASES = [
    # Baseline and component toggles
    "--dry-run",
    "--dry-run --disable-spark",
    "--dry-run --disable-mpi",
    "--dry-run --disable-model-monitoring",
    "--dry-run --disable-system-monitoring",
    "--dry-run --disable-spark --disable-mpi --disable-model-monitoring "
    "--disable-system-monitoring",
    # otel: the four-state fold, where left-to-right order is the whole point
    "--dry-run --enable-otel",
    "--dry-run --enable-otel off",
    "--dry-run --enable-otel collector",
    "--dry-run --enable-otel full",
    "--dry-run --enable-otel collector --enable-otel-instrumentation",
    "--dry-run --enable-otel-instrumentation --enable-otel collector",
    "--dry-run --enable-otel off --enable-otel-operator",
    "--dry-run --enable-otel-operator --enable-otel-collector",
    # Ingress, with and without an explicit class
    "--dry-run --enable-ingress",
    "--dry-run --enable-ingress traefik",
    # Local registry, which changes the registry URL and the secret's contents
    "--dry-run --local-registry",
    "--dry-run --local-registry --enable-ingress",
    # Secret and validator skips
    "--dry-run --skip-secret",
    "--dry-run --skip-validators",
    "--dry-run --skip-secret --skip-validators",
    # Chart selection
    "--dry-run --ce-version 0.11.0",
    # Config file and values file. The combined case is the one worth having: --config
    # resolves to --set and -f to --values, helm applies --set last, so the config's values
    # must win over the same keys in the values file.
    "--dry-run --config tests/installer/fixtures/ce-config.yaml",
    "--dry-run -f tests/installer/fixtures/values.yaml",
    "--dry-run --config tests/installer/fixtures/ce-config.yaml "
    "-f tests/installer/fixtures/values.yaml",
    "--dry-run --config tests/installer/fixtures/nonexistent.yaml",
    # Uninstall, including the destructive path
    "uninstall",
    "uninstall --hard-clean",
    # Refusals: the installer must keep declining these, and for the same reason
    "--hard-clean",
    "badverb",
    "--chart-path",
    "--enable-otel bogus",
]


def slugify(args):
    if not args:
        return "no-args"
    # Fixture paths would otherwise turn into unreadable filenames carrying a directory and
    # an extension; the basename identifies the case perfectly well.
    slug = args.replace("tests/installer/fixtures/", "").replace(".yaml", "")
    slug = slug.replace("--", "").replace(" ", "-").replace("/", "_")
    return re.sub(r"-{2,}", "-", slug).strip("-")


def run_case(args, tmp_path):
    """Run the installer with stubs on PATH; return (exit code, recorded calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    STUB.chmod(0o755)
    for name in ("helm", "kubectl", "docker", "minikube"):
        (bin_dir / name).symlink_to(STUB)

    log = tmp_path / "calls.log"
    log.write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(log)
    # A complete non-interactive answer set, so the run never stops at a prompt and any
    # difference is about logic rather than about a question being asked.
    env.update(
        {
            "NON_INTERACTIVE": "true",
            "EXTERNAL_HOST_ADDRESS": "localhost",
            "REGISTRY_URL": "index.docker.io/someone",
            "REGISTRY_USERNAME": "someone",
            "REGISTRY_PASSWORD": "secret",
            "REGISTRY_EMAIL": "someone@example.com",
            "STUB_SC_STABLE": "true",
        }
    )
    for leaked in ("CI", "KUBE_CONTEXT", "CHART_PATH", "CE_VERSION", "CONFIG_FILE"):
        env.pop(leaked, None)

    # A config file supplies the registry identity itself, and a flag or env var always
    # beats the file, so leaving these set would mask the values the case exists to
    # exercise — the recording would look identical whether the file was read or not. The
    # password stays: it is deliberately never read from a config file.
    if "--config" in args:
        for supplied_by_config in (
            "REGISTRY_URL",
            "REGISTRY_USERNAME",
            "REGISTRY_EMAIL",
            "EXTERNAL_HOST_ADDRESS",
        ):
            env.pop(supplied_by_config, None)

    # Invoked through the current interpreter rather than the shebang: pytest already has
    # the dependencies, so install.py's uv bootstrap is a no-op and the subprocess stays
    # cheap and offline.
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "install.py"), *args.split()],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    return completed.returncode, log.read_text()


def render(exit_code, calls):
    return f"exit: {exit_code}\n---\n{calls}"


@pytest.mark.parametrize("args", CASES, ids=slugify)
def test_installer_makes_the_recorded_calls(args, tmp_path):
    exit_code, calls = run_case(args, tmp_path)
    actual = render(exit_code, calls)
    golden = GOLDEN_DIR / f"{slugify(args)}.txt"

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden.write_text(actual)
        pytest.skip(f"recorded {golden.name}")

    assert golden.exists(), (
        f"no expectation recorded for `{args}` — run `make installer-test-golden-update`"
    )
    assert actual == golden.read_text(), (
        f"`install.py {args}` no longer does what golden/{golden.name} records.\n"
        f"If the change is intended, run `make installer-test-golden-update` and review "
        f"the diff."
    )
