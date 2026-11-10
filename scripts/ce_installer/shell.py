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
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, List, Optional, Sequence

from .console import (
    InstallerError,
    debug_log_path,
    die,
    err,
    log_debug,
    log_error,
    log_info,
    quiet,
)
from .secret_data import SecretDataRedactor, redact_secret_data
from .settings import Settings


@dataclass
class Result:
    code: int
    out: str
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


# Flags whose value is a credential. Nothing in the installer passes one today — the
# registry secret is piped as a manifest on stdin precisely so it cannot appear here — but
# a failed command prints its own argv, so anything that did would land in a CI log.
SECRET_FLAGS = ("--docker-password", "--password", "--token")


def redact(cmd: Sequence[str]) -> List[str]:
    """Mask credential values in a command about to be printed.

    Handles both `--flag value` and `--flag=value`, since click-style and kubectl-style
    call sites differ and a leak is not worth being clever about.
    """
    masked: List[str] = []
    mask_next = False
    for token in cmd:
        if mask_next:
            masked.append("<redacted>")
            mask_next = False
            continue
        if token in SECRET_FLAGS:
            mask_next = True
            masked.append(token)
            continue
        flag = token.split("=", 1)[0]
        if "=" in token and flag in SECRET_FLAGS:
            masked.append(f"{flag}=<redacted>")
            continue
        masked.append(token)
    return masked


# Per stream, per command. `kubectl get svc --all-namespaces` on a big cluster runs to
# megabytes, and the head of a failing command's output is what explains it.
DEBUG_OUTPUT_LIMIT = 20_000


def _clip(text: str) -> str:
    if len(text) <= DEBUG_OUTPUT_LIMIT:
        return text
    return text[:DEBUG_OUTPUT_LIMIT] + f"\n... ({len(text) - DEBUG_OUTPUT_LIMIT} more characters)"


def _log_command(
    cmd: Sequence[str], code: int, seconds: float, out: str, error: str, log_stdout: bool = True
) -> None:
    """Record one finished command. stdin is never written: it carries the pull secret."""
    lines = [f"$ {' '.join(redact(cmd))}", f"  exit {code} in {seconds:.2f}s"]
    if out.strip() and not log_stdout:
        lines.append(f"  --- stdout not logged ({len(out)} characters) ---")
        out = ""
    for label, text in (("stdout", out), ("stderr", error)):
        if text.strip():
            lines.append(f"  --- {label} ---")
            lines.append(_clip(redact_secret_data(text).rstrip("\n")))
    log_debug("\n".join(lines))


def run(
    cmd: Sequence[str],
    *,
    input_data: Optional[str] = None,
    check: bool = False,
    log_stdout: bool = True,
) -> Result:
    """Run a command, capturing stdout and stderr separately.

    Never raises on a non-zero exit unless `check` is set. Call sites mirror the bash
    original's explicit `|| true` / `if ! cmd` style rather than exception flow, which is
    what keeps the warning-vs-blocking distinction in the validators readable.

    The two streams are kept apart because `out` is parsed, not just displayed: kubectl and
    helm write deprecation and kubeconfig warnings to stderr on otherwise successful calls,
    and folding those into stdout corrupts whatever reads it. The sharpest case is
    `deploy_local_registry`, which pipes rendered YAML from one kubectl into the stdin of
    the next — a single warning line would be applied to the cluster as part of the
    manifest. The version validators are the subtler one: `parse_major_minor` takes the
    first `vN.N` anywhere in the string, and a warning naming a Kubernetes version would be
    read as the cluster's own.

    `log_stdout=False` keeps stdout out of the debug log, for output that holds values the
    Secret redactor does not recognise (`helm get values`).
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(cmd),
            input=input_data,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        _log_command(cmd, 127, time.monotonic() - started, "", "command not found")
        if check:
            raise die(f"Command not found: {cmd[0]}") from None
        return Result(127, "")

    result = Result(proc.returncode, proc.stdout or "", proc.stderr or "")
    _log_command(cmd, result.code, time.monotonic() - started, result.out, result.err, log_stdout)
    if check and not result.ok:
        # Diagnostics land on stderr, but fall back to stdout for tools that report
        # failures there rather than lose the only explanation the user would get.
        detail = result.err.strip() or result.out.strip()
        if detail:
            err.print(detail)
        raise die("Command failed: {}".format(" ".join(redact(cmd))), result.code)
    return result


def _tee(source: IO[str], sink: Optional[IO[str]]) -> None:
    redactor = SecretDataRedactor()
    for line in source:
        if sink is not None:
            sink.write(line)
            sink.flush()
        for safe in redactor.feed(line):
            log_debug(safe)
    for safe in redactor.flush():
        log_debug(safe)


def stream(cmd: Sequence[str], show_stdout: bool = True) -> int:
    """Run a command with its output going to the terminal, and to the debug log if open.

    Each stream keeps its own destination, so a helm error still reaches stderr for anyone
    piping stdout. Without `show_stdout`, and at the warning log level, stdout goes to the
    debug log only, where rendered Secrets are redacted.
    """
    show_stdout = show_stdout and not quiet()
    log_debug(f"$ {' '.join(redact(cmd))}  (output follows as it streams)")
    started = time.monotonic()
    try:
        if not debug_log_path():
            code = subprocess.call(list(cmd), stdout=None if show_stdout else subprocess.DEVNULL)
        else:
            proc = subprocess.Popen(
                list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
            stdout_sink = sys.stdout if show_stdout else None
            threads = [
                threading.Thread(target=_tee, args=(proc.stdout, stdout_sink), daemon=True),
                threading.Thread(target=_tee, args=(proc.stderr, sys.stderr), daemon=True),
            ]
            for thread in threads:
                thread.start()
            code = proc.wait()
            for thread in threads:
                thread.join()
    except FileNotFoundError:
        raise die(f"Command not found: {cmd[0]}") from None
    log_debug(f"  exit {code} in {time.monotonic() - started:.2f}s")
    return code


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

    The registry secret is a manifest applied with kubectl, not anything Docker builds.
    The only real use is the best-effort `docker login` in validate_registry_auth, which
    already degrades to a warning. Requiring it would also make the installer unusable
    from inside a pod on a containerd/CRI-O cluster, where there is no daemon.

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
