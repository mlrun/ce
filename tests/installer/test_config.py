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
"""ce-config.yaml handling: `load_config` and the required-field check behind it.

Ported from the `--config` / load_config group of tests/install_tests.bats, which is
retired with install.sh. The bash cases asserted on exit codes and echoed shell variables;
here the equivalent is an InstallerError carrying a `.code` and the fields it left on
Settings.

The whole point of this module is precedence — flag > env > file > built-in default — and
precedence is the kind of rule that regresses silently: a config file that quietly wins
over `--chart-path` still installs something, just not what was asked for. Most of the
tests below therefore pin the *negative*: what the file must NOT change.

One bash case is deliberately not ported. `load_config exits 1 when yq is not installed`
(install_tests.bats:548) described a prerequisite the port does not have — YAML is parsed
with pyyaml, so there is no yq to be missing and no exit to assert.
"""

from pathlib import Path

import pytest
import typer
import yaml

from ce_installer import cli, config
from ce_installer.console import InstallerError


def write_config(tmp_path, body):
    """Write an installer config file and return its path as the CLI would receive it."""
    path = tmp_path / "ce-config.yaml"
    path.write_text(body)
    return str(path)


# ------------------------------------------------------------------------------------------
# --config argument handling
# ------------------------------------------------------------------------------------------


def test_config_flag_path_reaches_the_run(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(cli, "execute", lambda settings: captured.setdefault("s", settings))

    path = write_config(tmp_path, "installer: {}\n")
    assert cli.main(["--config", path]) == 0

    assert captured["s"].config_file == path


def test_config_without_a_path_is_rejected_rather_than_eating_the_next_flag():
    # click would happily accept "--dry-run" as the value and then install from a config
    # file named after a flag. The pre-parse in cli.py exists to refuse that.
    with pytest.raises(InstallerError) as excinfo:
        cli.normalize_argv(["--config", "--dry-run"])
    assert excinfo.value.code == 1

    with pytest.raises(InstallerError) as excinfo:
        cli.normalize_argv(["--config"])
    assert excinfo.value.code == 1


def test_running_without_a_config_file_is_not_an_error(settings):
    # No --config is the default invocation, so this path is the common one: it must do
    # nothing at all, not merely nothing visible.
    settings.non_interactive = True

    config.load_config(settings)

    assert settings.config_registry_url == ""


def test_missing_config_file_is_reported_not_silently_ignored(settings):
    settings.config_file = "/nonexistent/ce-config.yaml"

    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)

    # Carrying on with built-in defaults would install something the user did not ask for.
    assert excinfo.value.code == 1
    assert "not found" in str(excinfo.value)


# ------------------------------------------------------------------------------------------
# Registry fields
# ------------------------------------------------------------------------------------------


def test_registry_fields_become_prompt_defaults(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  registry:\n"
        "    url: index.docker.io/myuser\n"
        "    secret:\n"
        "      username: myuser\n"
        "      server: https://index.docker.io/v1/\n"
        "      email: me@example.com\n",
    )

    config.load_config(settings)

    # These land in config_* fields rather than the live ones, because they are only
    # defaults: an env var or a typed prompt answer still has to be able to win.
    assert settings.config_registry_url == "index.docker.io/myuser"
    assert settings.config_registry_username == "myuser"
    assert settings.config_registry_server == "https://index.docker.io/v1/"
    assert settings.config_registry_email == "me@example.com"


def test_password_in_the_config_file_is_warned_about_and_ignored(settings, tmp_path, capture_logs):
    logs = capture_logs(config)
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  registry:\n    secret:\n      username: myuser\n      password: shhh\n",
    )

    config.load_config(settings)

    # A password in a file that users are told to commit is the failure mode being guarded
    # against, so silence here would be the bug — and nothing may carry the value forward.
    assert any("ignored" in message for message in logs)
    assert not any("shhh" in message for message in logs)
    assert "shhh" not in vars(settings).values()


# ------------------------------------------------------------------------------------------
# chartSource
# ------------------------------------------------------------------------------------------


def test_a_chart_path_alone_selects_the_local_chart(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path, "installer:\n  chartSource:\n    chartPath: /some/chart/dir\n"
    )

    config.load_config(settings)

    assert settings.chart_path == "/some/chart/dir"


def test_an_empty_chart_path_means_the_published_chart(settings, tmp_path):
    settings.config_file = write_config(tmp_path, "installer:\n  chartSource:\n    chartPath: ''\n")

    config.load_config(settings)

    assert settings.chart_path == ""


@pytest.mark.parametrize(
    "key, value, replacement",
    [("kind", "path", "chartPath"), ("skipDependencyUpdate", "true", "updateDependencies")],
)
def test_a_removed_chart_source_key_says_what_replaced_it(
    settings, tmp_path, capture_logs, key, value, replacement
):
    logs = capture_logs(config)
    settings.config_file = write_config(
        tmp_path, f"installer:\n  chartSource:\n    {key}: {value}\n    chartPath: ./c\n"
    )

    with pytest.raises(InstallerError):
        config.load_config(settings)

    assert any(f"chartSource.{key}: removed" in line and replacement in line for line in logs)


# ------------------------------------------------------------------------------------------
# The non-interactive required-field check
# ------------------------------------------------------------------------------------------


def load_and_check(settings):
    """load_config then the required-field check, in the order execute() runs them.

    The check used to be the last statement of load_config, which returns early when there
    is no --config; it moved to execute() so an env-var-only run is checked too.
    """
    config.load_config(settings)
    config.check_required_non_interactive(settings)


def test_every_missing_required_field_is_reported_in_one_run(settings, tmp_path, capture_logs):
    logs = capture_logs(config)
    settings.non_interactive = True
    settings.config_file = write_config(tmp_path, 'installer:\n  registry:\n    url: ""\n')

    with pytest.raises(InstallerError) as excinfo:
        load_and_check(settings)

    # Reported together on purpose: a CI run that fails three times in a row, once per
    # field, costs three round trips to learn what one message could have said.
    assert excinfo.value.code == 1
    reported = "\n".join(logs)
    assert "installer.registry.url" in reported
    assert "installer.registry.secret.username" in reported
    assert "REGISTRY_PASSWORD" in reported


def test_a_dry_run_demands_no_registry_fields(settings):
    # It asks nothing about the registry and renders a placeholder without one.
    settings.non_interactive = True
    settings.dry_run = True

    load_and_check(settings)


def test_local_registry_does_not_demand_a_registry_url(settings, tmp_path):
    settings.non_interactive = True
    settings.local_registry = True
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  registry:\n    secret:\n      username: myuser\n",
    )

    # There is no external registry to name or log in to — the URL is derived from the
    # in-cluster Service, and the secret is created with fixed local/local credentials.
    # (The bash case also exported REGISTRY_PASSWORD; the exemption covers all three
    # fields, so leaving it unset asserts the same rule more completely.)
    load_and_check(settings)


def test_skip_secret_does_not_demand_registry_credentials(settings, tmp_path):
    settings.non_interactive = True
    settings.skip_registry_secret = True
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  registry:\n    url: myregistry.example.com/user\n",
    )

    # Credentials are only ever needed to build the pull secret; --skip-secret says one
    # already exists, so requiring them would block a perfectly valid CI install.
    load_and_check(settings)


def test_values_file_alone_skips_the_required_field_check(settings):
    settings.non_interactive = True
    settings.values_file = "/some/values.yaml"

    # -f on its own is self-contained: the run creates no secret and gathers no params, so
    # there is nothing for these fields to feed.
    load_and_check(settings)

    assert settings.config_registry_url == ""


def test_values_file_combined_with_config_still_demands_registry_fields(
    settings, tmp_path, capture_logs
):
    logs = capture_logs(config)
    settings.non_interactive = True
    settings.values_file = "/some/values.yaml"
    settings.config_file = write_config(tmp_path, "installer: {}\n")

    with pytest.raises(InstallerError) as excinfo:
        load_and_check(settings)

    # Adding --config re-arms secret creation and param gathering, so the -f-only bypass
    # must not extend to the combination — otherwise the run fails later, mid-install.
    assert excinfo.value.code == 1
    assert "installer.registry.url" in "\n".join(logs)


# ------------------------------------------------------------------------------------------
# Precedence: the file supplies defaults and never overrides a flag or env value
# ------------------------------------------------------------------------------------------


def test_config_file_never_overrides_a_flag_or_env_value(settings, tmp_path):
    # Settings arrives from the CLI with flag/env already merged in, which is exactly the
    # state load_config has to respect.
    settings.kube_context = "from-env"
    settings.chart_path = "/from/flag"
    settings.ce_version = "1.2.3"
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  kubeContext: from-config\n"
        "  chartSource:\n"
        "    chartPath: /from/config\n"
        "    chartVersion: 9.9.9\n",
    )

    config.load_config(settings)

    assert settings.kube_context == "from-env"
    assert settings.chart_path == "/from/flag"
    assert settings.ce_version == "1.2.3"


def test_component_versions_come_from_the_config_file(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  versions:\n    mlrun: 1.13.0\n    nuclio: 1.16.0\n",
    )

    config.load_config(settings)

    # These pin individual service image tags, independently of the umbrella chart version.
    assert settings.mlrun_version == "1.13.0"
    assert settings.nuclio_version == "1.16.0"


# ------------------------------------------------------------------------------------------
# components.* and otel.* — same "add, never remove" rule, opposite polarity
# ------------------------------------------------------------------------------------------


def test_components_false_disables_the_same_as_the_disable_flags(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  components:\n"
        "    monitoring: false\n"
        "    spark: true\n"
        "    mpi: false\n"
        "    modelMonitoring: true\n",
    )

    config.load_config(settings)

    # components.* describes the chart's state ("is this component on?"), while Settings
    # holds the flags ("did anyone ask to turn it off?") — so the polarity inverts here,
    # and getting it backwards would disable whatever the user explicitly kept.
    assert settings.disable_system_monitoring is True
    assert settings.disable_spark is False
    assert settings.disable_mpi is True
    assert settings.disable_model_monitoring is False


def test_config_file_cannot_re_enable_a_component_disabled_by_flag(settings, tmp_path):
    settings.disable_spark = True
    settings.config_file = write_config(tmp_path, "installer:\n  components:\n    spark: true\n")

    config.load_config(settings)

    # `spark: true` is the file's default value, not a request — there is no flag to
    # explicitly re-enable a component, so the file can only ever add a disable.
    assert settings.disable_spark is True


def test_otel_keys_are_independent_opt_ins(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  otel:\n"
        "    operator: true\n"
        "    collector: true\n"
        "    namespaceLabel: false\n"
        "    instrumentation: false\n",
    )

    config.load_config(settings)

    # All four ship off in the chart, so unlike components.* each key opts in. Independent
    # rather than one bundled toggle: operator+collector is a metrics pipeline, while
    # namespaceLabel/instrumentation auto-instrument every Python pod in the namespace.
    assert settings.enable_otel_operator is True
    assert settings.enable_otel_collector is True
    assert settings.enable_otel_namespace_label is False
    assert settings.enable_otel_instrumentation is False


def test_config_file_cannot_turn_off_an_otel_toggle_set_by_flag(settings, tmp_path):
    settings.enable_otel_operator = True
    settings.config_file = write_config(tmp_path, "installer:\n  otel:\n    operator: false\n")

    config.load_config(settings)

    assert settings.enable_otel_operator is True


def test_config_file_cannot_re_enable_what_enable_otel_off_disabled(settings, tmp_path):
    # `--enable-otel off` resolves all four to False, which is also what "never mentioned"
    # looks like. Reading the booleans alone, the config's `true` won and the file switched
    # back on what the flag had just turned off — flag-over-config, inverted.
    settings.otel_set_by_cli = True
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  otel:\n    operator: true\n    collector: true\n",
    )

    config.load_config(settings)

    assert settings.enable_otel_operator is False
    assert settings.enable_otel_collector is False


def test_config_file_cannot_add_instrumentation_to_the_collector_mode(settings, tmp_path):
    # `--enable-otel collector` means operator+collector and *no* auto-instrumentation.
    # A mode names the complete state, so the file may not extend it either.
    settings.otel_set_by_cli = True
    settings.enable_otel_operator = True
    settings.enable_otel_collector = True
    settings.config_file = write_config(
        tmp_path, "installer:\n  otel:\n    instrumentation: true\n"
    )

    config.load_config(settings)

    assert settings.enable_otel_instrumentation is False


def test_otel_config_still_applies_when_no_otel_flag_was_passed(settings, tmp_path):
    settings.otel_set_by_cli = False
    settings.config_file = write_config(tmp_path, "installer:\n  otel:\n    operator: true\n")

    config.load_config(settings)

    assert settings.enable_otel_operator is True


def test_the_ingress_controller_service_can_come_from_the_config_file(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  localRegistry:\n    ingressControllerService: ingress-nginx/custom\n",
    )

    config.load_config(settings)

    assert settings.ingress_controller_service == "ingress-nginx/custom"


# ------------------------------------------------------------------------------------------
# Structure: a key in the wrong place, misspelt or mistyped stops the run before it starts
# ------------------------------------------------------------------------------------------


def _config_errors(settings, tmp_path, capture_logs, body):
    logs = capture_logs(config)
    settings.config_file = write_config(tmp_path, body)
    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)
    assert excinfo.value.code == 1
    return "\n".join([*logs, excinfo.value.message])


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # A block one level too high: read silently, the registry would come from the prompt.
        (
            "installer:\n  secret:\n    username: me\n",
            "installer.secret: not allowed here; it belongs under installer.registry",
        ),
        (
            "registry:\n  url: index.docker.io/me\n",
            "registry: not allowed here; it belongs under installer",
        ),
        (
            "installer:\n  components:\n    sprak: false\n",
            "installer.components.sprak: unknown key; did you mean spark?",
        ),
        (
            "nstaller:\n  kubeContext: kind-ce\n",
            "nstaller: unknown top-level key; only installer: is allowed; did you mean installer?",
        ),
        (
            "installer:\n  registry:\n    skipSecret: 'true'\n",
            "installer.registry.skipSecret: expected true or false, got 'true'",
        ),
        (
            "installer:\n  validators:\n    minK8sVersion: 1.30\n",
            "installer.validators.minK8sVersion: expected text, got 1.3; put the value in quotes",
        ),
        (
            "installer:\n  progress:\n    intervalSeconds: soon\n",
            "installer.progress.intervalSeconds: expected a whole number, got 'soon'",
        ),
        (
            "installer:\n  registry: index.docker.io/me\n",
            "installer.registry: expected a block of keys, got 'index.docker.io/me'",
        ),
    ],
)
def test_a_wrongly_scoped_or_typed_key_stops_the_run_and_says_where_it_belongs(
    settings, tmp_path, capture_logs, body, expected
):
    reported = _config_errors(settings, tmp_path, capture_logs, body)

    assert expected in reported
    assert "ce-config.local.yaml.example" in reported
    assert "ce-config.remote.yaml.example" in reported


def test_every_structure_problem_is_reported_in_one_run(settings, tmp_path, capture_logs):
    reported = _config_errors(
        settings,
        tmp_path,
        capture_logs,
        "installer:\n  kubeContxt: kind-ce\n  secret: {}\n  components:\n    spark: no-thanks\n",
    )

    assert "installer.kubeContxt" in reported
    assert "installer.secret" in reported
    assert "installer.components.spark" in reported


def test_a_rejected_file_changes_no_setting(settings, tmp_path, capture_logs):
    settings.config_file = write_config(
        tmp_path, "installer:\n  kubeContext: kind-ce\n  namespace: other\n  bogus: 1\n"
    )
    before = dict(vars(settings))

    with pytest.raises(InstallerError):
        config.load_config(settings)

    assert vars(settings) == before


def test_an_empty_installer_block_is_valid(settings, tmp_path):
    settings.config_file = write_config(tmp_path, "installer:\n")

    config.load_config(settings)


# ------------------------------------------------------------------------------------------
# The examples and the flags: every key documented, every flag configurable
# ------------------------------------------------------------------------------------------

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _leaf_paths(node, prefix=""):
    paths = set()
    for key, value in node.items():
        if isinstance(value, dict):
            paths |= _leaf_paths(value, f"{prefix}{key}.")
        else:
            paths.add(f"{prefix}{key}")
    return paths


@pytest.mark.parametrize("example", config.EXAMPLES)
def test_each_example_is_valid_and_lists_every_key(settings, example):
    data = yaml.safe_load((SCRIPTS / example).read_text())

    assert config.validate_structure(data) == []
    # password is in the schema only so a file holding one gets the warning.
    assert _leaf_paths(data) == _leaf_paths(config.SCHEMA) - {"installer.registry.secret.password"}


@pytest.mark.parametrize("example", config.EXAMPLES)
def test_each_example_loads(settings, example):
    settings.config_file = str(SCRIPTS / example)

    config.load_config(settings)

    assert settings.config_registry_url


@pytest.mark.parametrize("example", config.EXAMPLES)
def test_no_example_carries_a_password(example):
    data = yaml.safe_load((SCRIPTS / example).read_text())

    assert "password" not in data["installer"]["registry"]["secret"]


def test_every_flag_has_a_config_key_unless_it_is_a_per_run_action():
    command = typer.main.get_command(cli.app)
    flags = {
        opt
        for param in command.params
        for opt in param.opts
        if opt.startswith("--") and opt != "--help"
    }

    assert flags == set(config.FLAG_KEYS) | set(config.FLAGS_WITHOUT_KEY)
    keys = _leaf_paths(config.SCHEMA["installer"]) | set(config.SCHEMA["installer"])
    for flag, key in config.FLAG_KEYS.items():
        assert key in keys, f"{flag} maps to {key}, which the schema does not have"


# ------------------------------------------------------------------------------------------
# Settings that had only a flag or an environment variable
# ------------------------------------------------------------------------------------------


def test_env_only_settings_come_from_the_config_file(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  namespace: ml\n"
        "  releaseName: ce\n"
        "  registry:\n    secretName: pull\n    passwordFile: /run/pw\n"
        "  chartSource:\n    repoUrl: https://charts.example.com\n"
        "  helm:\n    timeout: 1800s\n"
        "  validators:\n    minK8sVersion: '1.30'\n"
        "  progress:\n    intervalSeconds: 5\n",
    )

    config.load_config(settings)

    assert (settings.namespace, settings.release_name) == ("ml", "ce")
    assert (settings.registry_secret_name, settings.registry_password_file) == ("pull", "/run/pw")
    assert settings.helm_repo_url == "https://charts.example.com"
    assert settings.helm_timeout == "1800s"
    assert settings.min_k8s_version == "1.30"
    assert settings.progress_interval_sec == "5"


def test_an_environment_variable_wins_over_the_config_file(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("NAMESPACE", "from-env")
    settings.namespace = "from-env"
    settings.config_file = write_config(tmp_path, "installer:\n  namespace: from-config\n")

    config.load_config(settings)

    assert settings.namespace == "from-env"


def test_toggles_come_from_the_config_file(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  nonInteractive: true\n"
        "  triggerMigrations: true\n"
        "  valuesFile: my-values.yaml\n"
        "  registry:\n    skipSecret: true\n"
        "  localRegistry:\n    enabled: true\n"
        "  chartSource:\n    updateDependencies: true\n    devel: true\n"
        "  ingress:\n    enabled: true\n    className: traefik\n"
        "  validators:\n    skip: true\n"
        "  progress:\n    show: true\n",
    )

    config.load_config(settings)

    assert settings.non_interactive and settings.trigger_migrations
    assert settings.values_file == "my-values.yaml"
    assert settings.skip_registry_secret and settings.local_registry and settings.devel
    assert settings.update_dependencies and settings.skip_validators and settings.show_progress
    assert (settings.enable_ingress, settings.ingress_class) == (True, "traefik")


def test_the_config_file_cannot_turn_off_a_toggle_set_by_flag(settings, tmp_path):
    settings.skip_validators = True
    settings.trigger_migrations = True
    settings.ingress_class = "haproxy"
    settings.config_file = write_config(
        tmp_path,
        "installer:\n"
        "  triggerMigrations: false\n"
        "  validators:\n    skip: false\n"
        "  ingress:\n    className: traefik\n",
    )

    config.load_config(settings)

    assert settings.skip_validators and settings.trigger_migrations
    assert settings.ingress_class == "haproxy"
