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
"""Everything to do with resolving a configuration value: the built-in defaults, the
environment, the Settings object they land in, and the prompt used when nothing else
supplied one."""

import getpass
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .console import die, log_debug

# --------------------------------------------------------------------------------------
# Built-in defaults
# --------------------------------------------------------------------------------------

DEFAULT_NAMESPACE = "mlrun"
DEFAULT_RELEASE_NAME = "mlrun-ce"
DEFAULT_REGISTRY_SECRET_NAME = "registry-credentials"
DEFAULT_HELM_REPO_URL = "https://mlrun.github.io/ce"

# --wait without --timeout inherits helm's 5m default, which a single image pull can
# outrun: the 4.2Gi jupyter image alone takes ~5m40s on a cold node, failing the release
# even though the rollout goes on to succeed.
DEFAULT_HELM_TIMEOUT = "960s"

DEFAULT_INGRESS_CLASS = "nginx"
DEFAULT_DOCKER_SERVER = "https://index.docker.io/v1/"

# The Helm floor is the one the MLRun CE docs require, and charts/mlrun-ce/README.md states
# the same. Below it the install is refused; --skip-validators is the only way past.
#
# Kubernetes never blocks: the chart declares no kubeVersion in Chart.yaml, so nothing it
# renders needs a particular cluster version. The MLRun CE docs ask for Kubernetes >= 1.34,
# and below that the installer warns. MIN_K8S_VERSION adds a stricter warning on top.
MIN_HELM_VERSION = "4.1"
RECOMMENDED_K8S_VERSION = "1.34"
MLRUN_CE_INSTALL_DOCS_URL = (
    "https://github.com/mlrun/mlrun/blob/development/docs/install-mlrun-ce/kubernetes-install.md"
)

# The chart's NodePorts, by the values key that sets each. The preflight moves a busy
# default to a free port and refuses a busy one set in -f; an upgrade keeps the ports the
# release already has.
NODEPORTS = {
    "kube-prometheus-stack.grafana.service.nodePort": 30010,
    "kube-prometheus-stack.prometheus.service.nodePort": 30020,
    "jupyterNotebook.service.nodePort": 30040,
    "global.nuclio.dashboard.nodePort": 30050,
    "mlrun.ui.service.nodePort": 30060,
    "mlrun.api.service.nodePort": 30070,
    "seaweedfs.adminService.nodePort": 30093,
    "seaweedfs.s3Service.nodePort": 30094,
    "pipelines.service.nodePort": 30100,
    "timescaledb.service.nodePort": 30110,
}
# Kubernetes' default --service-node-port-range.
NODEPORT_RANGE = range(30000, 32768)

COMMANDS = ("install", "uninstall", "version", "help")

UNKNOWN_VERSION = "unknown (standalone script — pin by release tag to identify it)"


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Mirror bash's ${NAME:-default}: an empty value counts as unset."""
    return os.environ.get(name) or default


def env_true(name: str) -> bool:
    """`true` or `1` turns a toggle on; anything else, `false` and `0` included, leaves it off."""
    return os.environ.get(name, "") in ("true", "1")


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


@dataclass
class Settings:
    """Every tunable, already resolved.

    Precedence is flag > env > ce-config.yaml > built-in default, applied in exactly two
    places (the CLI's flag merge, then `config.load_config`) rather than scattered across
    the call sites. Nothing below this layer reads os.environ for a tunable.
    """

    # Env-only — no flag exists for these.
    namespace: str = field(default_factory=lambda: env_str("NAMESPACE", DEFAULT_NAMESPACE))
    release_name: str = field(default_factory=lambda: env_str("RELEASE_NAME", DEFAULT_RELEASE_NAME))
    registry_secret_name: str = field(
        default_factory=lambda: env_str("REGISTRY_SECRET_NAME", DEFAULT_REGISTRY_SECRET_NAME)
    )
    helm_repo_url: str = field(
        default_factory=lambda: env_str("HELM_REPO_URL", DEFAULT_HELM_REPO_URL)
    )
    helm_timeout: str = field(default_factory=lambda: env_str("HELM_TIMEOUT", DEFAULT_HELM_TIMEOUT))
    generated_values_file: str = field(
        default_factory=lambda: env_str("GENERATED_VALUES_FILE", "mlrun-ce-values.yaml")
    )
    install_log_file: str = field(
        default_factory=lambda: env_str("INSTALL_LOG_FILE", "mlrun-ce-install.log")
    )
    registry_password_file: str = field(default_factory=lambda: env_str("REGISTRY_PASSWORD_FILE"))
    # 'namespace/name' of the ingress controller Service, for the --local-registry CoreDNS
    # patch. Unset means try the well-known ingress-nginx locations; see
    # registry.ingress_controller_candidates.
    ingress_controller_service: str = field(
        default_factory=lambda: env_str("INGRESS_CONTROLLER_SERVICE")
    )
    external_host_address: str = field(default_factory=lambda: env_str("EXTERNAL_HOST_ADDRESS"))
    kube_context: str = field(default_factory=lambda: env_str("KUBE_CONTEXT"))
    mlrun_version: str = field(default_factory=lambda: env_str("MLRUN_VERSION"))
    nuclio_version: str = field(default_factory=lambda: env_str("NUCLIO_VERSION"))
    min_k8s_version: str = field(default_factory=lambda: env_str("MIN_K8S_VERSION"))
    progress_interval_sec: str = field(
        default_factory=lambda: env_str("PROGRESS_INTERVAL_SEC", "10")
    )

    # Flag > env.
    skip_registry_secret: bool = False
    skip_validators: bool = False
    values_file: str = ""
    show_progress: bool = False
    # "info" or "warning"; empty until the flag, LOG_LEVEL or the config file sets it.
    log_level: str = ""
    uninstall: bool = False
    hard_clean: bool = False
    delete_namespace: bool = False
    # A hard clean's answer to "delete the functions and runs too?"; yes unless asked.
    delete_runtime: bool = True
    disable_system_monitoring: bool = False
    disable_spark: bool = False
    disable_mpi: bool = False
    disable_model_monitoring: bool = False
    enable_ingress: bool = False
    ingress_class: str = DEFAULT_INGRESS_CLASS
    # Set when --enable-ingress CLASS or INGRESS_CLASS named the class, nginx included, so
    # the config file's className does not replace it.
    ingress_class_set_by_cli: bool = False
    enable_otel_operator: bool = False
    enable_otel_collector: bool = False
    enable_otel_namespace_label: bool = False
    enable_otel_instrumentation: bool = False
    # Set when any --enable-otel* flag appeared, so load_config knows the four booleans
    # above are a deliberate complete state and not just unset defaults.
    otel_set_by_cli: bool = False
    local_registry: bool = False
    ce_version: str = ""
    # True when ce_version is the newest stable chart rather than --ce-version.
    ce_version_is_newest: bool = False
    # The chart version of the release being upgraded; "" on a first install.
    installed_chart_version: str = ""
    chart_path: str = ""
    update_dependencies: bool = False
    devel: bool = False
    dry_run: bool = False
    non_interactive: bool = False
    trigger_migrations: bool = False
    config_file: str = ""

    # Resolved during the run.
    local_registry_url: str = ""
    registry_url: str = ""
    chart_ref: str = ""
    registry_username_value: str = ""
    registry_password_value: str = ""
    registry_server_value: str = ""
    registry_email_value: str = ""
    # Non-interactive credentials not supplied, which only the installer's own secret excuses.
    missing_registry_credentials: List[str] = field(default_factory=list)
    registry_secret_reused: bool = False
    # Upgrade only: what `helm get values` returns for the current release.
    previous_values: Optional[dict] = None
    # Keys from an earlier installer values file that the user chose to merge into this one.
    kept_installer_values: dict = field(default_factory=dict)
    # NodePort values keys this run sets: the current release's ports, and a free port in
    # place of a busy default.
    node_ports: Dict[str, int] = field(default_factory=dict)
    # Upgrade only: how many values this run changes on the current release.
    value_changes: int = 0

    # Defaults sourced from ce-config.yaml, used as prompt fallbacks.
    config_registry_url: str = ""
    config_registry_username: str = ""
    config_registry_server: str = ""
    config_registry_email: str = ""
    config_external_host_address: str = ""

    # Parsed form of MIN_K8S_VERSION.
    min_k8s_major: Optional[int] = None
    min_k8s_minor: Optional[int] = None


# --------------------------------------------------------------------------------------
# Value resolution
# --------------------------------------------------------------------------------------


def prompt_or_env(
    settings: Settings,
    env_var: str,
    prompt_msg: str,
    default: str = "",
    secret: bool = False,
    allow_empty: bool = False,
) -> str:
    """Resolve a value from the environment, then a prompt, then a default.

    Prompts go to stderr so that piping the installer's stdout somewhere still shows them.

    `allow_empty` is for the fields that genuinely have no value rather than an unset one —
    REGISTRY_EMAIL, which registries stopped caring about years ago. Without it, "no default
    available" makes every such field mandatory under --non-interactive, which is how a CI
    run with a complete set of credentials still died asking for an email address.
    """
    value = env_str(env_var)
    if value:
        return value

    if settings.non_interactive:
        if default or allow_empty:
            return default
        raise die(
            f"Required value '{env_var}' is not set and no default is available "
            "(non-interactive mode)."
        )

    suffix = f" [{default}]" if default else ""
    label = f"{prompt_msg}{suffix}: "
    try:
        if secret:
            entered = getpass.getpass(label, stream=sys.stderr)
        else:
            sys.stderr.write(label)
            sys.stderr.flush()
            entered = input()
    except EOFError:
        entered = ""

    value = entered or default
    if not secret:
        log_debug(f"Prompted for {env_var}: {value!r}")
    return value


def confirm(settings: Settings, question: str, default_yes: bool = True) -> bool:
    """Ask a yes/no question on stderr. Never asked under --non-interactive, where the
    default stands. A closed stdin is a no: nobody is there to agree."""
    if settings.non_interactive:
        return default_yes
    sys.stderr.write(f"{question} {'[Y/n]' if default_yes else '[y/N]'}: ")
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        log_debug(f"Asked '{question}': stdin closed, treated as no")
        return False
    log_debug(f"Asked '{question}': {answer!r}")
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def choose(settings: Settings, question: str, choices: List[str], default: str) -> str:
    """Ask for one of `choices`, each answerable by its first letter. Enter takes the
    default and --non-interactive never asks. A closed stdin or an unknown answer is
    "cancel": nobody agreed to anything."""
    if settings.non_interactive:
        return default
    options = " / ".join(f"[{c[0]}]{c[1:]}" for c in choices)
    sys.stderr.write(f"{question} {options} (default: {default}): ")
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        log_debug(f"Asked '{question}': stdin closed, treated as cancel")
        return "cancel"
    log_debug(f"Asked '{question}': {answer!r}")
    if not answer:
        return default
    return next((c for c in choices if answer in (c, c[0])), "cancel")


def _beside_generated_values(settings: Settings, label: str) -> Path:
    generated = Path(settings.generated_values_file).resolve()
    return generated.with_name(f"{generated.stem}.{label}{generated.suffix}")


def previous_values_file(settings: Settings) -> Path:
    """Where an upgrade saves the values the current release was installed with."""
    return _beside_generated_values(settings, "previous")


def value_changes_file(settings: Settings) -> Path:
    """Where an upgrade writes the values it changes on the current release."""
    return _beside_generated_values(settings, "changes")


def parse_floor(raw: str, name: str) -> Tuple[int, int]:
    """Parse a MAJOR.MINOR version floor, rejecting anything else."""
    match = re.fullmatch(r"(\d+)\.(\d+)", raw)
    if not match:
        raise die(f"{name} must be in MAJOR.MINOR form (e.g. 1.30), got: '{raw}'")
    return int(match.group(1)), int(match.group(2))


def apply_version_floors(settings: Settings) -> None:
    if settings.min_k8s_version:
        settings.min_k8s_major, settings.min_k8s_minor = parse_floor(
            settings.min_k8s_version, "MIN_K8S_VERSION"
        )
