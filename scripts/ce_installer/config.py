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

Every key is declared in SCHEMA and checked before anything else runs: a key in the wrong
place, a misspelt one or a value of the wrong type stops the run with every problem listed
at once. Read silently, any of those would install with a default the file meant to change.
"""

import difflib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .console import InstallerError, die, log_error, log_info, log_warn
from .settings import Settings, env_str

EXAMPLES = ("ce-config.local.yaml.example", "ce-config.remote.yaml.example")
EXAMPLES_URL = "https://github.com/mlrun/ce/blob/development/scripts/"

# Every key the file may hold, by its type. A nested dict is a block.
SCHEMA: Dict = {
    "installer": {
        "kubeContext": str,
        "namespace": str,
        "releaseName": str,
        "externalHostAddress": str,
        "valuesFile": str,
        "nonInteractive": bool,
        "logLevel": str,
        "triggerMigrations": bool,
        "registry": {
            "url": str,
            "secretName": str,
            "skipSecret": bool,
            "passwordFile": str,
            "secret": {"username": str, "password": str, "server": str, "email": str},
        },
        "localRegistry": {"enabled": bool, "ingressControllerService": str},
        "chartSource": {
            "chartVersion": str,
            "chartPath": str,
            "repoUrl": str,
            "updateDependencies": bool,
            "devel": bool,
        },
        "versions": {"mlrun": str, "nuclio": str},
        "components": {"monitoring": bool, "spark": bool, "mpi": bool, "modelMonitoring": bool},
        "ingress": {"enabled": bool, "className": str},
        "otel": {
            "operator": bool,
            "collector": bool,
            "namespaceLabel": bool,
            "instrumentation": bool,
        },
        "helm": {"timeout": str},
        "validators": {"skip": bool, "minK8sVersion": str},
        "progress": {"show": bool, "intervalSeconds": int},
        "files": {"generatedValues": str, "installLog": str},
    }
}

# The config key behind each flag. --version, --config and the per-run actions (--uninstall,
# --hard-clean, --delete-namespace, --dry-run) have none: a file left saying
# `hardClean: true` would be a trap.
FLAG_KEYS = {
    "--skip-secret": "registry.skipSecret",
    "--skip-validators": "validators.skip",
    "--values": "valuesFile",
    "--show-progress": "progress.show",
    "--disable-system-monitoring": "components.monitoring",
    "--disable-spark": "components.spark",
    "--disable-mpi": "components.mpi",
    "--disable-model-monitoring": "components.modelMonitoring",
    "--enable-ingress": "ingress.enabled",
    "--enable-otel": "otel",
    "--enable-otel-operator": "otel.operator",
    "--enable-otel-collector": "otel.collector",
    "--enable-otel-namespace-label": "otel.namespaceLabel",
    "--enable-otel-instrumentation": "otel.instrumentation",
    "--local-registry": "localRegistry.enabled",
    "--kube-context": "kubeContext",
    "--ce-version": "chartSource.chartVersion",
    "--chart-path": "chartSource.chartPath",
    "--update-dependencies": "chartSource.updateDependencies",
    "--devel": "chartSource.devel",
    "--non-interactive": "nonInteractive",
    "--log-level": "logLevel",
    "--trigger-migrations": "triggerMigrations",
}
FLAGS_WITHOUT_KEY = (
    "--version",
    "--config",
    "--uninstall",
    "--hard-clean",
    "--delete-namespace",
    "--dry-run",
)

# Keys earlier files may still hold, with what replaced them.
REMOVED_KEYS = {
    "installer.chartSource.kind": "removed; a non-empty chartPath selects a local chart",
    "installer.chartSource.skipDependencyUpdate": (
        "removed; dependencies are fetched only with updateDependencies: true"
    ),
}


def as_text(value) -> str:
    """Render a YAML scalar as the string the comparisons below read against."""
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


def _schema_paths(schema: Dict, prefix: str = "") -> Dict[str, str]:
    """Every key's name, mapped to the dotted path of the block it belongs to."""
    paths: Dict[str, str] = {}
    for key, kind in schema.items():
        paths.setdefault(key, prefix)
        if isinstance(kind, dict):
            for name, parent in _schema_paths(kind, f"{prefix}{key}.").items():
                paths.setdefault(name, parent)
    return paths


_KEY_HOMES = _schema_paths(SCHEMA)


def _unknown_key(path: str, key: str, siblings: List[str]) -> str:
    where = f"{path}.{key}" if path else key
    if where in REMOVED_KEYS:
        return f"{where}: {REMOVED_KEYS[where]}"
    home = _KEY_HOMES.get(key)
    if home is not None:
        return f"{where}: not allowed here; it belongs under {home.rstrip('.') or 'the top level'}"
    close = difflib.get_close_matches(key, siblings, n=1)
    hint = f"; did you mean {close[0]}?" if close else ""
    if not path:
        return f"{where}: unknown top-level key; only installer: is allowed{hint}"
    return f"{where}: unknown key{hint}"


def _type_error(where: str, kind: type, value) -> Optional[str]:
    if value is None:
        return None
    if kind is bool and not isinstance(value, bool):
        return f"{where}: expected true or false, got {value!r}"
    if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
        return f"{where}: expected a whole number, got {value!r}"
    if kind is str and not isinstance(value, str):
        # A number here is almost always a version YAML read as a float: 1.30 becomes 1.3.
        return f"{where}: expected text, got {value!r}; put the value in quotes"
    return None


def validate_structure(data, schema: Dict = SCHEMA, path: str = "") -> List[str]:
    """Every misplaced, unknown or mistyped key in `data`, as one message each."""
    if data is None:
        return []
    if not isinstance(data, dict):
        return [f"{path or 'the file'}: expected a block of keys, got {data!r}"]
    errors: List[str] = []
    for key, value in data.items():
        where = f"{path}.{key}" if path else str(key)
        kind = schema.get(key)
        if kind is None:
            errors.append(_unknown_key(path, str(key), list(schema)))
        elif isinstance(kind, dict):
            errors.extend(validate_structure(value, kind, where))
        else:
            error = _type_error(where, kind, value)
            if error:
                errors.append(error)
    return errors


def examples_hint() -> str:
    names = " or ".join(f"scripts/{name}" for name in EXAMPLES)
    return f"See {names} ({EXAMPLES_URL})."


def _read(settings: Settings) -> Dict:
    path = Path(settings.config_file)
    if not path.is_file():
        raise die(f"Config file not found: {settings.config_file}. {examples_hint()}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise die(f"Could not parse config file {settings.config_file}: {exc}") from exc
    errors = validate_structure(data)
    if errors:
        log_error(f"{settings.config_file} does not match the installer's config format:")
        for error in errors:
            log_error(f"  - {error}")
        raise die(examples_hint())
    return data or {}


def _from_file_unless_env(data: Dict, env_var: str, *path: str) -> Tuple[bool, str]:
    """The file's value for a setting an environment variable would otherwise supply."""
    value = cfg(data, "installer", *path)
    return (bool(value) and not env_str(env_var)), value


def load_config(settings: Settings) -> None:
    """Fill in anything flag/env did not already set from a ce-config.yaml.

    Never touches helm values directly — every key here becomes a default for an existing
    flag/env/prompt, so precedence stays flag > env > config file > built-in default.
    """
    if not settings.config_file:
        return
    data = _read(settings)

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

    _apply_flag_and_env_settings(data, settings)
    _apply_chart_and_components(data, settings)


def _apply_flag_and_env_settings(data: Dict, settings: Settings) -> None:
    # Settings an environment variable supplies, and no flag.
    for attr, env_var, path in (
        ("namespace", "NAMESPACE", ("namespace",)),
        ("release_name", "RELEASE_NAME", ("releaseName",)),
        ("registry_secret_name", "REGISTRY_SECRET_NAME", ("registry", "secretName")),
        ("registry_password_file", "REGISTRY_PASSWORD_FILE", ("registry", "passwordFile")),
        ("helm_repo_url", "HELM_REPO_URL", ("chartSource", "repoUrl")),
        ("helm_timeout", "HELM_TIMEOUT", ("helm", "timeout")),
        ("min_k8s_version", "MIN_K8S_VERSION", ("validators", "minK8sVersion")),
        ("progress_interval_sec", "PROGRESS_INTERVAL_SEC", ("progress", "intervalSeconds")),
        ("generated_values_file", "GENERATED_VALUES_FILE", ("files", "generatedValues")),
        ("install_log_file", "INSTALL_LOG_FILE", ("files", "installLog")),
    ):
        applies, value = _from_file_unless_env(data, env_var, *path)
        if applies:
            setattr(settings, attr, value)

    # A flag or env var that turned a toggle on is never turned off by the file; the file
    # can only turn one on.
    for attr, path in (
        ("skip_registry_secret", ("registry", "skipSecret")),
        ("local_registry", ("localRegistry", "enabled")),
        ("update_dependencies", ("chartSource", "updateDependencies")),
        ("devel", ("chartSource", "devel")),
        ("non_interactive", ("nonInteractive",)),
        ("skip_validators", ("validators", "skip")),
        ("show_progress", ("progress", "show")),
        ("trigger_migrations", ("triggerMigrations",)),
    ):
        if not getattr(settings, attr) and cfg(data, "installer", *path) == "true":
            setattr(settings, attr, True)

    if not settings.values_file:
        settings.values_file = cfg(data, "installer", "valuesFile")
    if not settings.log_level:
        settings.log_level = cfg(data, "installer", "logLevel")

    if not settings.enable_ingress and cfg(data, "installer", "ingress", "enabled") == "true":
        settings.enable_ingress = True
    cfg_ingress_class = cfg(data, "installer", "ingress", "className")
    if cfg_ingress_class and not settings.ingress_class_set_by_cli:
        settings.ingress_class = cfg_ingress_class


def _apply_chart_and_components(data: Dict, settings: Settings) -> None:
    settings.config_chart_version = cfg(data, "installer", "chartSource", "chartVersion")
    settings.config_chart_path = cfg(data, "installer", "chartSource", "chartPath")
    # Each of the two names the whole chart, so a flag or env var for either one overrides
    # the file's other one as well.
    if not settings.ce_version and not settings.chart_path:
        settings.ce_version = settings.config_chart_version
        settings.chart_path = settings.config_chart_path

    cfg_mlrun = cfg(data, "installer", "versions", "mlrun")
    cfg_nuclio = cfg(data, "installer", "versions", "nuclio")
    if not settings.mlrun_version and cfg_mlrun:
        settings.mlrun_version = cfg_mlrun
    if not settings.nuclio_version and cfg_nuclio:
        settings.nuclio_version = cfg_nuclio

    # components.* mirrors the --disable-* flags exactly: "false" disables, same as passing
    # the flag. A flag/env-set disable is never un-set by the file — there is no "explicitly
    # re-enable" flag to begin with, so config can only ever add a disable, never remove one.
    # A "true" is recorded and written as on, over a -f file or an earlier run's disable.
    for attr, key in (
        ("disable_system_monitoring", "monitoring"),
        ("disable_spark", "spark"),
        ("disable_mpi", "mpi"),
        ("disable_model_monitoring", "modelMonitoring"),
    ):
        if getattr(settings, attr):
            continue
        value = cfg(data, "installer", "components", key)
        if value == "false":
            setattr(settings, attr, True)
        elif value == "true":
            settings.enabled_by_config.append(attr)

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


def _local_chart(chart_path: str) -> str:
    """The path, with its Chart.yaml version when that can be read."""
    try:
        chart = yaml.safe_load((Path(chart_path) / "Chart.yaml").read_text())
    except (OSError, yaml.YAMLError):
        chart = None
    version = str(chart.get("version", "")) if isinstance(chart, dict) else ""
    return f"{chart_path} (version {version})" if version else chart_path


def _given(flag: str, env_var: str, value: str, from_env: bool) -> Tuple[str, str]:
    """How a command-line value was given, and how to take it back."""
    if from_env:
        return f"{env_var}={value}", f"unset {env_var}"
    return f"{flag} {value}", f"drop {flag}"


def _refuse_two_charts(
    settings: Settings, headline: str, version: str, path: str, keep_version: str, keep_path: str
) -> InstallerError:
    log_error(f"{headline}; keep one:")
    log_error(
        f"  the published chart version {version} from {settings.helm_repo_url}: {keep_version}"
    )
    log_error(f"  the local chart at {_local_chart(path)}: {keep_path}")
    return InstallerError(headline, code=1)


def check_chart_source(settings: Settings) -> None:
    """Refuse a published chart version and a local chart named at the same layer.

    Across layers load_config has already let the flag or env var win; this only says which
    of the file's keys that dropped. Uninstall reads neither, so it is never refused.
    """
    if settings.uninstall:
        return
    file_version, file_path = settings.config_chart_version, settings.config_chart_path
    if file_version and file_path:
        where = settings.config_file
        raise _refuse_two_charts(
            settings,
            f"{where} sets both installer.chartSource.chartVersion ({file_version}) and "
            f"installer.chartSource.chartPath ({file_path})",
            file_version,
            file_path,
            keep_version=f"remove chartPath from {where}",
            keep_path=f"remove chartVersion from {where}",
        )
    if settings.ce_version and settings.chart_path:
        version_given, drop_version = _given(
            "--ce-version", "CE_VERSION", settings.ce_version, settings.ce_version_from_env
        )
        path_given, drop_path = _given(
            "--chart-path", "CHART_PATH", settings.chart_path, settings.chart_path_from_env
        )
        raise _refuse_two_charts(
            settings,
            f"{version_given} and {path_given} name two different charts",
            settings.ce_version,
            settings.chart_path,
            keep_version=drop_path,
            keep_path=drop_version,
        )
    if settings.chart_path and file_version:
        log_info(
            f"Using local chart {_local_chart(settings.chart_path)}; the config file's "
            f"chartVersion {file_version} is ignored."
        )
    elif settings.ce_version and file_path:
        log_info(
            f"Installing chart version {settings.ce_version} (--ce-version or CE_VERSION); "
            f"the config file's chartPath {file_path} is ignored."
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
    # A dry run asks nothing about the registry and needs none of it.
    if not settings.non_interactive or settings.dry_run:
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

    credentials: List[str] = []
    if not settings.skip_registry_secret and not settings.local_registry:
        if not env_str("REGISTRY_USERNAME") and not settings.config_registry_username:
            credentials.append("installer.registry.secret.username (or REGISTRY_USERNAME)")
        password_file = settings.registry_password_file
        if not env_str("REGISTRY_PASSWORD") and not (
            password_file and Path(password_file).is_file()
        ):
            credentials.append(
                "REGISTRY_PASSWORD, REGISTRY_PASSWORD_FILE or installer.registry.passwordFile "
                "(the password itself is never read from ce-config.yaml)"
            )

    if missing:
        report_missing_configuration(missing + credentials)
    # Missing credentials alone wait for prepare_registry_secret: a re-run reuses the
    # installer's own pull secret without them, and only the cluster can say whether it
    # exists.
    settings.missing_registry_credentials = credentials


def report_missing_configuration(missing: List[str]) -> None:
    log_error("Missing required configuration (non-interactive mode, no prompt to fall back on):")
    for item in missing:
        log_error(f"  - {item}")
    raise InstallerError(code=1)
