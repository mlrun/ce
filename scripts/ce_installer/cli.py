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
"""Turning a command line into a run: the argv pre-parse click cannot express, the typer
command, the run order, and the entry point."""

import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import click
import typer

from .cluster import (
    ensure_namespace,
    gather_install_params,
    resolve_external_host,
    validate_chart_path,
)
from .config import check_required_non_interactive, load_config
from .console import InstallerError, die, log_info, out
from .helm_ops import do_uninstall, helm_install
from .registry import (
    create_registry_secret,
    deploy_local_registry,
    verify_existing_registry_secret,
)
from .settings import (
    COMMANDS,
    DEFAULT_INGRESS_CLASS,
    UNKNOWN_VERSION,
    Settings,
    apply_version_floors,
    env_str,
    env_true,
)
from .shell import check_requirements
from .validators import run_validators

# --------------------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------------------

try:
    from ._chart_version import CHART_VERSION as BUILT_IN_CHART_VERSION
except ImportError:
    # Written into the wheel by scripts/hatch_build.py, so it is absent in a checkout —
    # where the chart itself is present and authoritative anyway.
    BUILT_IN_CHART_VERSION = ""


def installer_version() -> str:
    """Report the chart version, whether or not a chart is sitting next to this file.

    The installer has no version of its own — it ships with the chart and is released by
    the same tag — so there is exactly one number, in charts/mlrun-ce/Chart.yaml. It is
    reached two ways, in this order:

    1. The chart in the surrounding checkout, which is authoritative and always current. A
       developer editing Chart.yaml sees the new value immediately, with nothing to rebuild.
    2. A value baked in at build time by hatch_build.py, for an installed copy that has no
       checkout around it — `uvx --from "git+...#subdirectory=scripts"` and friends.

    Only a copy that is neither in a checkout nor built by that pipeline is genuinely
    unknown, and then nothing records which commit it came from; pin by release tag.

    Path.resolve() follows symlink chains in one step; `make installer-link` puts the
    command on PATH as a link into a checkout, and the link's own directory has no chart.
    """
    try:
        package_dir = Path(__file__).resolve().parent
    except OSError:
        package_dir = None

    if package_dir is not None:
        # scripts/ce_installer/cli.py -> scripts/ -> repo root -> charts/mlrun-ce
        chart_yaml = package_dir.parent.parent / "charts" / "mlrun-ce" / "Chart.yaml"
        if chart_yaml.is_file():
            match = re.search(r"^version:\s*(\S+)", chart_yaml.read_text(), re.MULTILINE)
            if match:
                return match.group(1)

    return BUILT_IN_CHART_VERSION or UNKNOWN_VERSION


def print_version() -> None:
    out.print(f"mlrun-ce installer {installer_version()}")


# --------------------------------------------------------------------------------------
# argv pre-parse
#
# Three things the bash installer's hand-rolled parse_args does that click does not do
# natively, handled here so the command itself can stay a plain typer command:
#
# 1. --enable-ingress [CLASS] and --enable-otel [MODE] take an *optional* value, consumed
#    only when the next token is not itself an option.
# 2. The otel flags are order-sensitive: a mode names a complete state, so
#    `--enable-otel collector --enable-otel-instrumentation` ends with instrumentation on
#    while the reverse order does not. click does not preserve inter-option order.
#
# A value is "another option" if it starts with a single dash, not two. bash tested
# `!= --*`, which let `--enable-ingress -f values.yaml` read "-f" as the ingress class and
# strand the path; no ingress class, otel mode, version or path legitimately begins with a
# dash, so the stricter test costs nothing and closes that.
# --------------------------------------------------------------------------------------

OTEL_GRANULAR = (
    "--enable-otel-operator",
    "--enable-otel-collector",
    "--enable-otel-namespace-label",
    "--enable-otel-instrumentation",
)

# Options that require a value and reject a following "--flag", matching the bash
# `if [[ -z "${2:-}" || "${2:-}" == --* ]]` guards.
VALUE_REQUIRED = {
    "--chart-path": "Option {opt} requires a directory path (e.g. --chart-path ./charts/mlrun-ce).",
    "--ce-version": "Option {opt} requires a version value (e.g. --ce-version 0.11.0).",
    "--config": "Option {opt} requires a file path (e.g. --config ce-config.yaml).",
}


def inject_default_command(args: Sequence[str]) -> List[str]:
    """Pull an optional leading verb off the front, defaulting to `install`.

    Kept in front of the flag parser so that stays a pure flag parser. No verb (or a
    leading flag) means install, which is what every invocation predating commands relied
    on; an unrecognised bare word is an error rather than an install, so a typo like
    `unistall` cannot wipe a cluster's worth of PVCs.
    """
    args = list(args)
    if not args:
        return ["install"]

    first = args[0]
    if first in COMMANDS:
        return args
    if first.startswith("-"):
        return ["install", *args]
    raise die(
        f"Unknown command '{first}'. Expected one of: install, uninstall, version, help.\n"
        "[INFO] Flags may be passed without a command, e.g. '--dry-run' is the same as "
        "'install --dry-run'."
    )


def otel_flags_present(args: Sequence[str]) -> bool:
    """Whether the CLI said anything about otel at all.

    Distinct from the resolved state, which cannot answer this: `--enable-otel off` and
    passing no flag both leave the four booleans False, and only the first of those means
    the user has decided. load_config needs to tell them apart.
    """
    return any(
        token == "--enable-otel" or token.startswith("--enable-otel=") or token in OTEL_GRANULAR
        for token in args
    )


def resolve_otel_flags(args: Sequence[str], start: Sequence[bool]) -> List[bool]:
    """Fold the otel flags left-to-right into a final (operator, collector, label, instr) state.

    Order is significant and deliberate: a MODE names a *complete* state, so `collector`
    has to mean "no auto-instrumentation" even when the env vars or an earlier granular
    flag turned those two on.
    """
    state = list(start)

    def apply_mode(mode: str) -> None:
        if mode == "off":
            state[:] = [False, False, False, False]
        elif mode == "collector":
            # Set, not just left alone: see the docstring.
            state[:] = [True, True, False, False]
        elif mode == "full":
            state[:] = [True, True, True, True]
        else:
            raise die(f"Invalid --enable-otel mode: '{mode}' (expected off, collector, or full).")

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--enable-otel":
            mode = "full"
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                mode = args[index + 1]
                index += 1
            apply_mode(mode)
        elif token.startswith("--enable-otel="):
            apply_mode(token.split("=", 1)[1] or "full")
        elif token in OTEL_GRANULAR:
            state[OTEL_GRANULAR.index(token)] = True
        index += 1

    return state


def normalize_argv(args: Sequence[str]) -> List[str]:
    """Rewrite optional-value flags into explicit `--flag=value` form for click.

    Also enforces the bash "this option requires a value" guards, which click would
    otherwise satisfy by swallowing the following flag as the value.
    """
    result: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        nxt = args[index + 1] if index + 1 < len(args) else None

        if token in ("--enable-ingress", "--enable-otel"):
            if nxt is not None and not nxt.startswith("-"):
                result.append(f"{token}={nxt}")
                index += 2
                continue
            # Bare. An empty value means "flag given, no explicit value"; --enable-otel's
            # own default of "full" is applied by resolve_otel_flags, which reads raw argv.
            result.append("{}={}".format(token, "full" if token == "--enable-otel" else ""))
            index += 1
            continue

        if token in VALUE_REQUIRED:
            if nxt is None or nxt.startswith("-"):
                raise die(VALUE_REQUIRED[token].format(opt=token))
            result.extend([token, nxt])
            index += 2
            continue

        if token in ("-f", "--values"):
            if nxt is None or nxt == "" or nxt.startswith("-"):
                raise die(f"Option {token} requires a value (path to YAML file).")
            result.extend([token, nxt])
            index += 2
            continue

        result.append(token)
        index += 1

    return result


# --------------------------------------------------------------------------------------
# Run order
# --------------------------------------------------------------------------------------


def execute(settings: Settings) -> None:
    load_config(settings)

    if settings.hard_clean and not settings.uninstall:
        raise die("--hard-clean requires --uninstall.")

    if settings.uninstall:
        do_uninstall(settings)
        return

    # After the uninstall branch, so tearing a release down does not demand registry
    # credentials, and outside load_config, so an env-var-only --non-interactive run is
    # checked too.
    check_required_non_interactive(settings)

    # Before anything touches the cluster. Further down this sat after ensure_namespace and
    # deploy_local_registry, so a mistyped -f left a namespace and a running registry behind
    # on the way to reporting that the file was never there.
    if settings.values_file and not Path(settings.values_file).is_file():
        raise die(f"Values file not found: {settings.values_file}")
    # Same reasoning: resolve_chart_source only reaches its path checks after the namespace
    # exists and a local registry is running.
    validate_chart_path(settings)

    check_requirements(settings)
    ensure_namespace(settings)

    if settings.local_registry or settings.enable_ingress:
        resolve_external_host(settings)

    if settings.local_registry:
        deploy_local_registry(settings)

    # Pure -f-only mode (no --config) stays fully self-contained: no secret creation, no
    # registry/host resolution — the values file must already reference an existing secret.
    # -f combined with --config runs the same secret creation / registry resolution as
    # --config-only, so the config-resolved fields have something to layer their --set on.
    if settings.values_file and not settings.config_file:
        log_info(
            f"Using values file: {settings.values_file} "
            "(skipping secret creation and install prompts)"
        )
    else:
        if settings.skip_registry_secret:
            log_info("Skipping registry secret creation (--skip-secret or SKIP_REGISTRY_SECRET)")
            verify_existing_registry_secret(settings)
        else:
            create_registry_secret(settings)
        gather_install_params(settings)

    if settings.skip_validators:
        log_info("Skipping pre-install validators (--skip-validators or SKIP_VALIDATORS)")
    else:
        run_validators(settings)

    helm_install(settings)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

# Raw argv, captured before click consumes it. resolve_otel_flags needs the original
# left-to-right order, which click does not preserve.
_RAW_ARGS: List[str] = []


app = typer.Typer(
    add_completion=False,
    context_settings={
        "help_option_names": ["-h", "--help"],
        # Unknown options are rejected
        "max_content_width": 100,
    },
)


@app.command(
    help=(
        "Interactive installer for MLRun CE on your local Kubernetes cluster. "
        "Creates a Docker registry secret (unless skipped) and installs the Helm chart "
        "with your local URL and registry URL, or from a values file.\n\n"
        "Commands: install (the default), uninstall, version, help. Flags may be passed "
        "with no command at all."
    )
)
def install(
    version: bool = typer.Option(
        False,
        "-v",
        "--version",
        help="Print the installer version (read from the chart it ships with)",
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Uninstall the MLRun CE Helm release (uses RELEASE_NAME and NAMESPACE)",
    ),
    hard_clean: bool = typer.Option(
        False,
        "--hard-clean",
        help="Use with --uninstall: also delete all PVCs and PVs in the namespace (data loss!)",
    ),
    skip_secret: bool = typer.Option(
        False,
        "--skip-secret",
        help="Do not create or replace the registry secret (use existing one)",
    ),
    skip_validators: bool = typer.Option(
        False,
        "--skip-validators",
        help="Skip the pre-install validators (K8s/Helm version, StorageClass, registry auth, "
        "NodePort conflicts, node capacity)",
    ),
    values: Optional[str] = typer.Option(
        None,
        "-f",
        "--values",
        metavar="FILE",
        help="Use this YAML values file as the base for installation. Alone (no --config): "
        "fully self-contained (global.registry.*, versions, everything) — skips secret "
        "creation and prompts. Combined with --config: --config still creates the registry "
        "secret and resolves its curated fields as --set overrides, which win over the same "
        "keys in this file (helm applies --set after --values).",
    ),
    show_progress: bool = typer.Option(
        False,
        "--show-progress",
        help="Show a live-updating UI with each deployment/statefulset progress during install",
    ),
    disable_system_monitoring: bool = typer.Option(
        False, "--disable-system-monitoring", help="Disable Grafana/Prometheus stack"
    ),
    disable_spark: bool = typer.Option(False, "--disable-spark", help="Disable Spark operator"),
    disable_mpi: bool = typer.Option(False, "--disable-mpi", help="Disable MPI operator resources"),
    disable_model_monitoring: bool = typer.Option(
        False, "--disable-model-monitoring", help="Disable Kafka + TimescaleDB components"
    ),
    enable_ingress: Optional[str] = typer.Option(
        None,
        "--enable-ingress",
        metavar="[CLASS]",
        help="Enable the chart's Ingress resources for UI/API/Jupyter/Nuclio (class defaults to "
        '"nginx"). Requires an ingress controller already present in the cluster — this '
        "installer does not install one for you.",
    ),
    enable_otel: Optional[str] = typer.Option(
        None,
        "--enable-otel",
        metavar="[MODE]",
        help="Convenience for the 4 granular toggles below. MODE is one of: off, collector "
        "(operator+collector only — metrics pipeline, no auto-instrumentation), or full (all 4). "
        "Bare --enable-otel means full. The granular flags still work individually and combine "
        "with this, left to right.",
    ),
    enable_otel_operator: bool = typer.Option(
        False,
        "--enable-otel-operator",
        help="--set opentelemetry-operator.enabled=true (installs CRDs/webhook/manager)",
    ),
    enable_otel_collector: bool = typer.Option(
        False,
        "--enable-otel-collector",
        help="--set opentelemetry.collector.enabled=true (deploys the Collector -> Prometheus)",
    ),
    enable_otel_namespace_label: bool = typer.Option(
        False,
        "--enable-otel-namespace-label",
        help="--set opentelemetry.namespaceLabel.enabled=true (labels NAMESPACE; auto-instruments "
        "every Python pod in it once the operator+instrumentation are also on)",
    ),
    enable_otel_instrumentation: bool = typer.Option(
        False,
        "--enable-otel-instrumentation",
        help="--set opentelemetry.instrumentation.enabled=true (creates the Instrumentation CR)",
    ),
    local_registry: bool = typer.Option(
        False, "--local-registry", help="Deploy a local registry:2 registry inside the cluster"
    ),
    ce_version: Optional[str] = typer.Option(
        None,
        "--ce-version",
        metavar="VERSION",
        help="Pin a specific mlrun-ce chart version (default: latest)",
    ),
    chart_path: Optional[str] = typer.Option(
        None,
        "--chart-path",
        metavar="DIR",
        help="Install from a local chart directory instead of the published repo — use "
        "./charts/mlrun-ce for this repo's own chart, on whatever branch is checked out. "
        "Resolves the chart's dependencies first (honouring requirements.lock).",
    ),
    skip_dependency_update: bool = typer.Option(
        False,
        "--skip-dependency-update",
        help="With --chart-path: do not fetch chart dependencies at all. Use when charts/ is "
        "already vendored and the host has no access to the upstream Helm repos.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Show what would happen without changing anything. Renders the chart via helm "
            "--dry-run=server, and on 'uninstall' reports what would be removed"
        ),
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; fail with exit 1 if a required value is missing "
        "(auto-set when CI=true)",
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        metavar="FILE",
        help="Read defaults from a ce-config.yaml file's 'installer:' block. Flag/env values "
        "always win over the file. Can be combined with -f/--values.",
    ),
) -> None:
    if version:
        print_version()
        raise typer.Exit(0)

    try:
        settings = Settings()

        # Flag > env for every boolean toggle; bash seeded each from the env var and then
        # let the flag set it to true, which is the same OR.
        settings.skip_registry_secret = skip_secret or env_true("SKIP_REGISTRY_SECRET")
        settings.skip_validators = skip_validators or env_true("SKIP_VALIDATORS")
        settings.show_progress = show_progress or env_true("SHOW_PROGRESS")
        settings.uninstall = uninstall or env_true("UNINSTALL")
        settings.hard_clean = hard_clean or env_true("HARD_CLEAN")
        settings.disable_system_monitoring = disable_system_monitoring or env_true(
            "DISABLE_SYSTEM_MONITORING"
        )
        settings.disable_spark = disable_spark or env_true("DISABLE_SPARK")
        settings.disable_mpi = disable_mpi or env_true("DISABLE_MPI")
        settings.disable_model_monitoring = disable_model_monitoring or env_true(
            "DISABLE_MODEL_MONITORING"
        )
        settings.local_registry = local_registry or env_true("LOCAL_REGISTRY")
        settings.dry_run = dry_run or env_true("DRY_RUN")
        settings.skip_dependency_update = skip_dependency_update or env_true(
            "SKIP_DEPENDENCY_UPDATE"
        )
        # CI auto-enables non-interactive, matching the bash installer.
        settings.non_interactive = non_interactive or env_true("NON_INTERACTIVE") or env_true("CI")

        settings.values_file = values or ""
        settings.ce_version = ce_version or env_str("CE_VERSION")
        settings.chart_path = chart_path or env_str("CHART_PATH")
        settings.config_file = config or env_str("CONFIG_FILE")

        settings.ingress_class = env_str("INGRESS_CLASS", DEFAULT_INGRESS_CLASS)
        settings.enable_ingress = env_true("ENABLE_INGRESS")
        if enable_ingress is not None:
            settings.enable_ingress = True
            if enable_ingress:
                settings.ingress_class = enable_ingress

        # The otel four-state is folded from raw argv rather than from the parsed flags,
        # because a MODE names a complete state and order decides the outcome. The five
        # otel parameters above are declared only so click renders them in --help; their
        # parsed values are deliberately unused here.
        (
            settings.enable_otel_operator,
            settings.enable_otel_collector,
            settings.enable_otel_namespace_label,
            settings.enable_otel_instrumentation,
        ) = resolve_otel_flags(
            _RAW_ARGS,
            (
                env_true("ENABLE_OTEL_OPERATOR"),
                env_true("ENABLE_OTEL_COLLECTOR"),
                env_true("ENABLE_OTEL_NAMESPACE_LABEL"),
                env_true("ENABLE_OTEL_INSTRUMENTATION"),
            ),
        )
        settings.otel_set_by_cli = otel_flags_present(_RAW_ARGS)

        apply_version_floors(settings)
        execute(settings)
    except InstallerError as exc:
        raise typer.Exit(exc.code) from None


def click_exception_types() -> tuple:
    """The classes a click usage error can actually arrive as.

    typer >= 0.24 ships a vendored copy of click as `typer._click`, so a command built by
    `typer.main.get_command` raises that copy's `ClickException` — a different class from
    the one `import click` provides, and not a subclass of it. Which applies depends on the
    resolved typer version and therefore on the user's Python: 3.9 gets typer 0.23 and the
    real click, 3.10+ gets 0.27 and the vendored one.

    Catching only the real one left a bare traceback and exit 1 on 3.10+ where 3.9 printed
    one clean line and exited 2 — invisible until unknown options became fatal, because
    until then nothing routine reached click's own error path.
    """
    types = [click.ClickException]
    vendored = getattr(typer, "_click", None)
    if vendored is not None and vendored.ClickException is not click.ClickException:
        types.append(vendored.ClickException)
    return tuple(types)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # A module global rather than a parameter: the typer command is invoked by click, so
    # there is no call site in between to thread the raw argv through.
    global _RAW_ARGS  # noqa: PLW0603
    raw = list(sys.argv[1:] if argv is None else argv)

    try:
        args = inject_default_command(raw)
    except InstallerError as exc:
        return exc.code

    verb, rest = args[0], args[1:]
    if verb == "help":
        rest = ["--help"]
    elif verb == "version":
        print_version()
        return 0
    elif verb == "uninstall":
        # The `uninstall` command and the older --uninstall flag are the same thing, so the
        # verb collapses into the flag and there is only ever one command to parse.
        rest = ["--uninstall", *rest]

    _RAW_ARGS = list(rest)

    try:
        normalized = normalize_argv(rest)
    except InstallerError as exc:
        return exc.code

    command = typer.main.get_command(app)
    try:
        # standalone_mode=False stops click calling sys.exit() itself, which is what lets
        # main() stay a plain int-returning function that tests can call. The catch is that
        # click then *returns* the code from a typer.Exit instead of propagating it, so the
        # return value has to be honoured or every in-command failure silently exits 0.
        result = command.main(
            args=normalized, prog_name="mlrun-ce-installer", standalone_mode=False
        )
    except SystemExit as exc:
        return int(exc.code or 0)
    except typer.Exit as exc:
        return exc.exit_code
    except click_exception_types() as exc:
        exc.show()
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    return result if isinstance(result, int) else 0
