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
"""Everything that reaches an external binary: running it, the kubectl/helm wrappers that
carry KUBE_CONTEXT, and the pre-flight check that the binaries are there at all."""

import functools
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .console import InstallerError, die, err, log_error, log_info
from .settings import Settings


@dataclass
class Result:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(
    cmd: Sequence[str],
    *,
    input_data: Optional[str] = None,
    check: bool = False,
) -> Result:
    """Run a command, capturing stdout and stderr together.

    Never raises on a non-zero exit unless `check` is set. Call sites mirror the bash
    original's explicit `|| true` / `if ! cmd` style rather than exception flow, which is
    what keeps the warning-vs-blocking distinction in the validators readable.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        if check:
            raise die(f"Command not found: {cmd[0]}") from None
        return Result(127, "")

    result = Result(proc.returncode, proc.stdout or "")
    if check and not result.ok:
        if result.out:
            err.print(result.out.rstrip("\n"))
        raise die("Command failed: {}".format(" ".join(cmd)), result.code)
    return result


def stream(cmd: Sequence[str]) -> int:
    """Run a command with its output going straight to the terminal."""
    try:
        return subprocess.call(list(cmd))
    except FileNotFoundError:
        raise die(f"Command not found: {cmd[0]}") from None


# The wrappers below exist so every call in the installer honours KUBE_CONTEXT
# (installer.kubeContext) without threading --context/--kube-context through every call
# site individually.
def kubectl_cmd(settings: Settings, *args: str) -> List[str]:
    cmd = ["kubectl"]
    if settings.kube_context:
        cmd += ["--context", settings.kube_context]
    return cmd + list(args)


def helm_cmd(settings: Settings, *args: str) -> List[str]:
    cmd = ["helm"]
    if settings.kube_context:
        cmd += ["--kube-context", settings.kube_context]
    return cmd + list(args)


def kubectl(settings: Settings, *args: str, **kwargs) -> Result:
    return run(kubectl_cmd(settings, *args), **kwargs)


def helm(settings: Settings, *args: str, **kwargs) -> Result:
    return run(helm_cmd(settings, *args), **kwargs)


# --------------------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    """docker is optional: the install never needs it.

    The registry secret is created by `kubectl create secret docker-registry`, not by
    Docker. The only real use is the best-effort `docker login` in validate_registry_auth,
    which already degrades to a warning. Requiring it would also make the installer
    unusable from inside a pod on a containerd/CRI-O cluster, where there is no daemon.

    Cached because both check_requirements and validate_registry_auth ask, and `docker
    info` against an unreachable daemon is not free — the bash installer probed once into
    a variable that both readers shared.
    """
    return shutil.which("docker") is not None and run(["docker", "info"]).ok


def check_requirements(settings: Settings) -> None:
    log_info("Checking prerequisites (helm, kubectl)...")

    if shutil.which("helm") is None:
        log_error("Helm is not installed or not in PATH.")
        log_info("Install Helm: https://helm.sh/docs/intro/install/")
        raise InstallerError(code=1)

    version = helm(settings, "version", "--short")
    if not version.ok or not version.out.strip():
        version = helm(settings, "version")
    first_line = version.out.strip().splitlines()[0] if version.out.strip() else ""
    log_info(f"  Helm: {first_line}")

    if shutil.which("kubectl") is None:
        log_error("kubectl is not installed or not in PATH.")
        log_info("Install kubectl: https://kubernetes.io/docs/tasks/tools/")
        raise InstallerError(code=1)

    if not kubectl(settings, "cluster-info").ok:
        log_error("kubectl is not configured or cannot reach a Kubernetes cluster.")
        log_info("Configure kubeconfig (e.g. set KUBECONFIG or run your cluster's setup).")
        log_info(
            "See: https://kubernetes.io/docs/concepts/configuration/"
            "organize-cluster-access-kubeconfig/"
        )
        raise InstallerError(code=1)
    log_info("  kubectl: connected to cluster")

    if docker_available():
        log_info("  Docker: installed and configured")
    else:
        log_info("  Docker: not available (optional — only used by the registry-auth validator)")

    log_info("All prerequisites met.")
