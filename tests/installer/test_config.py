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

import pytest

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


def test_chart_kind_path_sets_the_chart_path(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  chartSource:\n    kind: path\n    chartPath: /some/chart/dir\n",
    )

    config.load_config(settings)

    assert settings.chart_path == "/some/chart/dir"


def test_chart_kind_path_without_a_chart_path_fails_even_interactively(settings, tmp_path):
    settings.non_interactive = False
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  chartSource:\n    kind: path\n",
    )

    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)

    # Unlike every other field here, chartPath has no flag/env/prompt fallback, so there is
    # nothing later in the run that could rescue it — interactive mode included.
    assert excinfo.value.code == 1
    assert "chartSource.chartPath" in str(excinfo.value)


# ------------------------------------------------------------------------------------------
# The non-interactive required-field check
# ------------------------------------------------------------------------------------------


def test_every_missing_required_field_is_reported_in_one_run(settings, tmp_path, capture_logs):
    logs = capture_logs(config)
    settings.non_interactive = True
    settings.config_file = write_config(tmp_path, 'installer:\n  registry:\n    url: ""\n')

    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)

    # Reported together on purpose: a CI run that fails three times in a row, once per
    # field, costs three round trips to learn what one message could have said.
    assert excinfo.value.code == 1
    reported = "\n".join(logs)
    assert "installer.registry.url" in reported
    assert "installer.registry.secret.username" in reported
    assert "REGISTRY_PASSWORD" in reported


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
    config.load_config(settings)


def test_skip_secret_does_not_demand_registry_credentials(settings, tmp_path):
    settings.non_interactive = True
    settings.skip_registry_secret = True
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  registry:\n    url: myregistry.example.com/user\n",
    )

    # Credentials are only ever needed to build the pull secret; --skip-secret says one
    # already exists, so requiring them would block a perfectly valid CI install.
    config.load_config(settings)


def test_values_file_alone_skips_the_required_field_check(settings):
    settings.non_interactive = True
    settings.values_file = "/some/values.yaml"

    # -f on its own is self-contained: the run creates no secret and gathers no params, so
    # there is nothing for these fields to feed.
    config.load_config(settings)

    assert settings.config_registry_url == ""


def test_values_file_combined_with_config_still_demands_registry_fields(
    settings, tmp_path, capture_logs
):
    logs = capture_logs(config)
    settings.non_interactive = True
    settings.values_file = "/some/values.yaml"
    settings.config_file = write_config(tmp_path, "installer: {}\n")

    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)

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
        "    kind: path\n"
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


# ------------------------------------------------------------------------------------------
# chartSource.kind selects where the chart comes from, so a typo may not be ignored
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["pth", "Path", "local", "git"])
def test_an_unrecognised_chart_source_kind_is_rejected(settings, tmp_path, kind):
    # Falling through to repo mode would install the *published* chart while the file is
    # plainly asking for the local one — the difference between testing your branch and
    # testing whatever is on the chart repo, with nothing said either way.
    settings.config_file = write_config(
        tmp_path,
        f"installer:\n  chartSource:\n    kind: {kind}\n    chartPath: ./charts/mlrun-ce\n",
    )

    with pytest.raises(InstallerError) as excinfo:
        config.load_config(settings)
    assert kind in excinfo.value.message


@pytest.mark.parametrize("kind", ["repo", "path"])
def test_the_documented_chart_source_kinds_are_accepted(settings, tmp_path, kind):
    settings.config_file = write_config(
        tmp_path,
        f"installer:\n  chartSource:\n    kind: {kind}\n    chartPath: ./charts/mlrun-ce\n",
    )

    config.load_config(settings)

    assert settings.chart_path == ("./charts/mlrun-ce" if kind == "path" else "")


def test_an_omitted_chart_source_kind_still_means_repo(settings, tmp_path):
    settings.config_file = write_config(tmp_path, "installer:\n  chartSource:\n    kind: ''\n")

    config.load_config(settings)

    assert settings.chart_path == ""


def test_the_ingress_controller_service_can_come_from_the_config_file(settings, tmp_path):
    settings.config_file = write_config(
        tmp_path,
        "installer:\n  localRegistry:\n    ingressControllerService: ingress-nginx/custom\n",
    )

    config.load_config(settings)

    assert settings.ingress_controller_service == "ingress-nginx/custom"
