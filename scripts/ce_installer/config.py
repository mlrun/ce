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
"""The ce-config.yaml 'installer:' block.

Reading this with pyyaml rather than shelling out to `yq` is what removes yq from the
installer's prerequisites.
"""

from pathlib import Path
from typing import Dict, List

import yaml

from .console import InstallerError, die, log_error, log_warn
from .settings import Settings, env_str

# The only values installer.chartSource.kind may take; anything else is a config error
# rather than a silent fall-back to the default.
CHART_SOURCE_KINDS = ("repo", "path")


def as_text(value) -> str:
    """Render a YAML scalar the way `yq eval` would.

    Keeps the comparisons below reading against the literals 'true'/'false' the bash
    version used, whether the file spells them as YAML booleans or as quoted strings.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def cfg(data: Dict, *path: str) -> str:
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return ""
        node = node[key]
    return as_text(node)


def load_config(settings: Settings) -> None:
    """Fill in anything flag/env did not already set from a ce-config.yaml.

    Never touches helm values directly — every key here becomes a default for an existing
    flag/env/prompt, so precedence stays flag > env > config file > built-in default.
    """
    if not settings.config_file:
        return

    path = Path(settings.config_file)
    if not path.is_file():
        raise die(f"Config file not found: {settings.config_file}")

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise die(f"Could not parse config file {settings.config_file}: {exc}") from exc
    if not isinstance(data, dict):
        data = {}

    if cfg(data, "installer", "registry", "secret", "password"):
        log_warn("ce-config.yaml has 'installer.registry.secret.password' set — ignored.")
        log_warn(
            "Passwords are never read from the config file; "
            "set REGISTRY_PASSWORD or use the prompt."
        )

    settings.config_registry_url = cfg(data, "installer", "registry", "url")
    settings.config_registry_username = cfg(data, "installer", "registry", "secret", "username")
    settings.config_registry_server = cfg(data, "installer", "registry", "secret", "server")
    settings.config_registry_email = cfg(data, "installer", "registry", "secret", "email")

    external = cfg(data, "installer", "externalHostAddress")
    settings.config_external_host_address = "" if external == "auto" else external

    cfg_kube_context = cfg(data, "installer", "kubeContext")
    if not settings.kube_context and cfg_kube_context:
        settings.kube_context = cfg_kube_context

    if not settings.ingress_controller_service:
        settings.ingress_controller_service = cfg(
            data, "installer", "localRegistry", "ingressControllerService"
        )

    cfg_chart_kind = cfg(data, "installer", "chartSource", "kind")
    cfg_chart_version = cfg(data, "installer", "chartSource", "chartVersion")
    cfg_chart_path = cfg(data, "installer", "chartSource", "chartPath")
    # Checked rather than defaulted: 'kind' selects where the chart comes from, so a typo
    # like 'pth' would otherwise fall through to repo mode and quietly install the
    # published chart instead of the local one the file is asking for.
    if cfg_chart_kind and cfg_chart_kind not in CHART_SOURCE_KINDS:
        expected = ", ".join(CHART_SOURCE_KINDS)
        raise die(
            f"ce-config.yaml sets installer.chartSource.kind: {cfg_chart_kind} — "
            f"expected one of {expected}."
        )
    if not settings.ce_version and cfg_chart_version:
        settings.ce_version = cfg_chart_version
    if not settings.chart_path and cfg_chart_kind == "path" and cfg_chart_path:
        settings.chart_path = cfg_chart_path

    cfg_mlrun = cfg(data, "installer", "versions", "mlrun")
    cfg_nuclio = cfg(data, "installer", "versions", "nuclio")
    if not settings.mlrun_version and cfg_mlrun:
        settings.mlrun_version = cfg_mlrun
    if not settings.nuclio_version and cfg_nuclio:
        settings.nuclio_version = cfg_nuclio

    # components.* mirrors the --disable-* flags exactly: "false" disables, same as passing
    # the flag. A flag/env-set disable is never un-set by the file — there is no "explicitly
    # re-enable" flag to begin with, so config can only ever add a disable, never remove one.
    for attr, key in (
        ("disable_system_monitoring", "monitoring"),
        ("disable_spark", "spark"),
        ("disable_mpi", "mpi"),
        ("disable_model_monitoring", "modelMonitoring"),
    ):
        if not getattr(settings, attr) and cfg(data, "installer", "components", key) == "false":
            setattr(settings, attr, True)

    # otel is its own block, not under components.*: the chart ships all four of these
    # opentelemetry-operator/opentelemetry.* values disabled by default (unlike components.*,
    # which default enabled), so each key here opts IN, mirroring the --enable-otel-* flags
    # rather than the --disable-* pattern. Independently settable so a user can enable e.g.
    # just the operator+collector without the namespace-wide auto-instrumentation.
    #
    # The whole block is skipped when any otel flag was passed, because unlike the
    # --disable-* toggles above, an otel flag can legitimately resolve to False: a MODE
    # names the complete state, so `--enable-otel off` and `collector` both turn things
    # *off*. Reading a per-attribute "is it still False?" cannot tell that apart from
    # "never mentioned", and would let the file switch back on what the flag just
    # disabled — inverting the documented flag-over-config precedence.
    if not settings.otel_set_by_cli:
        for attr, key in (
            ("enable_otel_operator", "operator"),
            ("enable_otel_collector", "collector"),
            ("enable_otel_namespace_label", "namespaceLabel"),
            ("enable_otel_instrumentation", "instrumentation"),
        ):
            if not getattr(settings, attr) and cfg(data, "installer", "otel", key) == "true":
                setattr(settings, attr, True)

    # chartPath is a pure config-authoring error with no flag/env/prompt fallback, so it is
    # checked unconditionally rather than only in --non-interactive mode.
    if cfg_chart_kind == "path" and not settings.chart_path:
        raise die(
            "ce-config.yaml sets installer.chartSource.kind: path but "
            "installer.chartSource.chartPath is empty."
        )


def check_required_non_interactive(settings: Settings) -> None:
    """Report every missing required field at once, rather than one exit-1 at a time.

    Everything here already has a flag/env/prompt fallback, so this only fires in
    --non-interactive mode where there is no prompt left to catch it. --skip-secret
    bypasses the username/password checks. Pure -f-only mode (no --config) bypasses the
    whole block, since the run skips secret creation and param gathering entirely there;
    -f combined with --config does not, since --config still drives secret creation.

    Called from execute(), not from load_config(): it used to sit at the end of load_config,
    which returns immediately when there is no --config, so `--non-interactive` driven purely
    by environment variables got no up-front check at all and failed later, one variable at a
    time, from inside prompt_or_env. execute() also calls it after the uninstall branch, since
    an uninstall needs no registry credentials.
    """
    if not settings.non_interactive:
        return
    if settings.values_file and not settings.config_file:
        return

    missing: List[str] = []
    if (
        not settings.local_registry
        and not env_str("REGISTRY_URL")
        and not settings.config_registry_url
    ):
        missing.append("installer.registry.url (or REGISTRY_URL)")

    if not settings.skip_registry_secret and not settings.local_registry:
        if not env_str("REGISTRY_USERNAME") and not settings.config_registry_username:
            missing.append("installer.registry.secret.username (or REGISTRY_USERNAME)")
        password_file = settings.registry_password_file
        if not env_str("REGISTRY_PASSWORD") and not (
            password_file and Path(password_file).is_file()
        ):
            missing.append(
                "REGISTRY_PASSWORD or REGISTRY_PASSWORD_FILE "
                "(env/file only — never set via ce-config.yaml)"
            )

    if missing:
        log_error(
            "Missing required configuration (non-interactive mode, no prompt to fall back on):"
        )
        for item in missing:
            log_error(f"  - {item}")
        raise InstallerError(code=1)
