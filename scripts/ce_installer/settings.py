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
from typing import Optional, Tuple

from .console import die

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

# The Helm floor mirrors charts/mlrun-ce/README.md's "Helm >=3.6" — the chart's own stated
# requirement, so the installer never refuses a Helm the chart itself supports.
#
# Kubernetes has no floor by default: the chart declares no kubeVersion in Chart.yaml and
# the README states no cluster version, so there is nothing to enforce. The check reports
# what it finds and only warns when MIN_K8S_VERSION is set explicitly. Both are overridable
# upward for anyone who wants to enforce a stricter environment.
DEFAULT_MIN_HELM_VERSION = "3.6"

# The chart's fixed NodePorts (not configurable via values.yaml).
REQUIRED_NODEPORTS = [30010, 30020, 30040, 30050, 30060, 30070, 30093, 30094, 30100, 30110]

COMMANDS = ("install", "uninstall", "version", "help")

UNKNOWN_VERSION = "unknown (standalone script — pin by release tag to identify it)"


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Mirror bash's ${NAME:-default}: an empty value counts as unset."""
    return os.environ.get(name) or default


def env_true(name: str) -> bool:
    """Mirror bash's [[ "${NAME}" == "true" ]] — only the exact string counts."""
    return os.environ.get(name, "") == "true"


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
    min_helm_version: str = field(
        default_factory=lambda: env_str("MIN_HELM_VERSION", DEFAULT_MIN_HELM_VERSION)
    )
    progress_interval_sec: str = field(
        default_factory=lambda: env_str("PROGRESS_INTERVAL_SEC", "10")
    )

    # Flag > env.
    skip_registry_secret: bool = False
    skip_validators: bool = False
    values_file: str = ""
    show_progress: bool = False
    uninstall: bool = False
    hard_clean: bool = False
    disable_system_monitoring: bool = False
    disable_spark: bool = False
    disable_mpi: bool = False
    disable_model_monitoring: bool = False
    enable_ingress: bool = False
    ingress_class: str = DEFAULT_INGRESS_CLASS
    enable_otel_operator: bool = False
    enable_otel_collector: bool = False
    enable_otel_namespace_label: bool = False
    enable_otel_instrumentation: bool = False
    # Set when any --enable-otel* flag appeared, so load_config knows the four booleans
    # above are a deliberate complete state and not just unset defaults.
    otel_set_by_cli: bool = False
    local_registry: bool = False
    ce_version: str = ""
    chart_path: str = ""
    skip_dependency_update: bool = False
    dry_run: bool = False
    non_interactive: bool = False
    config_file: str = ""

    # Resolved during the run.
    local_registry_url: str = ""
    registry_url: str = ""
    chart_ref: str = ""
    registry_username_value: str = ""
    registry_password_value: str = ""
    registry_server_value: str = ""

    # Defaults sourced from ce-config.yaml, used as prompt fallbacks.
    config_registry_url: str = ""
    config_registry_username: str = ""
    config_registry_server: str = ""
    config_registry_email: str = ""
    config_external_host_address: str = ""

    # Parsed forms of the version floors.
    min_k8s_major: Optional[int] = None
    min_k8s_minor: Optional[int] = None
    min_helm_major: int = 3
    min_helm_minor: int = 6


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

    return entered or default


def parse_floor(raw: str, name: str) -> Tuple[int, int]:
    """Parse a MAJOR.MINOR version floor, rejecting anything else."""
    match = re.fullmatch(r"(\d+)\.(\d+)", raw)
    if not match:
        example = "1.30" if "K8S" in name else "3.6"
        raise die(f"{name} must be in MAJOR.MINOR form (e.g. {example}), got: '{raw}'")
    return int(match.group(1)), int(match.group(2))


def apply_version_floors(settings: Settings) -> None:
    if settings.min_k8s_version:
        settings.min_k8s_major, settings.min_k8s_minor = parse_floor(
            settings.min_k8s_version, "MIN_K8S_VERSION"
        )
    settings.min_helm_major, settings.min_helm_minor = parse_floor(
        settings.min_helm_version, "MIN_HELM_VERSION"
    )
