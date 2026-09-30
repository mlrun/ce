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
"""Turning a command line into a run: commands, flags, precedence, and the argv pre-parse.

Ported from the bash suite's command/flag/otel sections. The pre-parse is the part with no
click equivalent — optional option values, order-sensitive otel folding, and warning rather
than aborting on an unknown flag — so it carries most of the risk here.
"""

from pathlib import Path

import hatch_build
import pytest

from ce_installer import cli, console
from ce_installer.console import InstallerError
from ce_installer.settings import UNKNOWN_VERSION, Settings, env_str, prompt_or_env

# ------------------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    """Run cli.main() without executing an install; returns (exit_code, settings_or_None)."""
    captured = {}

    def fake_execute(settings):
        captured["settings"] = settings

    monkeypatch.setattr(cli, "execute", fake_execute)

    def run(*argv):
        code = cli.main(list(argv))
        return code, captured.get("settings")

    return run


def test_install_verb_is_consumed_and_its_flags_still_parse(ran):
    code, settings = ran("install", "--dry-run")
    assert code == 0
    assert settings.dry_run is True


def test_uninstall_verb_means_the_same_as_the_flag(ran):
    code, settings = ran("uninstall")
    assert code == 0
    assert settings.uninstall is True


def test_bare_flags_with_no_command_still_install(ran):
    # Every invocation predating commands relied on this.
    code, settings = ran("--dry-run")
    assert code == 0
    assert settings.dry_run is True


def test_no_arguments_at_all_is_an_install(ran):
    code, settings = ran()
    assert code == 0
    assert settings is not None


def test_an_unknown_command_refuses_instead_of_installing(ran):
    # A typo like `unistall` must not fall through to an install, which on a live cluster
    # would be a destructive surprise.
    code, settings = ran("unistall")
    assert code == 1
    assert settings is None


def test_version_command_prints_and_exits_zero(ran, capsys):
    code, settings = ran("version")
    assert code == 0
    assert settings is None, "version must not start an install"
    assert "mlrun-ce installer" in capsys.readouterr().out


def test_help_command_exits_zero(ran, capsys):
    code, _ = ran("help")
    assert code == 0
    assert "Usage" in capsys.readouterr().out


def test_short_v_is_accepted_for_version(ran, capsys):
    code, settings = ran("-v")
    assert code == 0
    assert settings is None
    assert "mlrun-ce installer" in capsys.readouterr().out


def test_an_unknown_option_aborts_rather_than_being_ignored(ran):
    # install.sh warned and carried on, which meant a typo changed what the run did:
    # this invocation would have performed a real install, the --dry-run never reaching it.
    code, settings = ran("--dry-rnu")
    assert code != 0, "an unrecognised flag must abort before anything touches the cluster"
    assert settings is None, "the command body must not run at all"


def test_an_unknown_option_aborts_even_alongside_version(ran):
    code, _ = ran("--bogus-flag", "--version")
    assert code != 0, "a typo must not be masked by an unrelated flag that exits early"


def test_an_unknown_option_is_one_clean_line_and_exit_2(ran, capsys):
    code, _ = ran("--dry-rnu")

    # click's own usage-error handling, which means the exception has to be caught as the
    # class typer actually raises. typer >= 0.24 vendors click, so `click.ClickException`
    # alone let it escape as a traceback on Python 3.10+ while 3.9 printed this — a split
    # that only appeared once unknown options started reaching click at all.
    assert code == 2
    printed = capsys.readouterr()
    combined = printed.out + printed.err
    assert "--dry-rnu" in combined
    assert "Traceback" not in combined


# ------------------------------------------------------------------------------------------
# Version reporting
# ------------------------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]


def installer_chart_version():
    """The version in charts/mlrun-ce/Chart.yaml, read without going through the code."""
    chart_yaml = REPO_ROOT / "charts" / "mlrun-ce" / "Chart.yaml"
    for line in chart_yaml.read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return ""


def test_version_is_read_from_the_chart_shipped_beside_the_installer():
    # The installer has no version of its own; it ships with the chart and is released by
    # the same tag, so a copy kept here would only drift.
    expected = installer_chart_version()
    assert expected, "could not read the chart version to compare against"
    assert cli.installer_version() == expected


def test_version_tracks_the_chart_when_it_changes(monkeypatch, tmp_path):
    package = tmp_path / "repo" / "scripts" / "ce_installer"
    package.mkdir(parents=True)
    chart = tmp_path / "repo" / "charts" / "mlrun-ce"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v1\nname: mlrun-ce\nversion: 9.9.9-rc.1\n")

    monkeypatch.setattr(cli, "__file__", str(package / "cli.py"))
    assert cli.installer_version() == "9.9.9-rc.1"


def test_version_is_unknown_when_run_detached_from_a_checkout(monkeypatch, tmp_path):
    # Nothing records which commit a loose copy came from, so "unknown" is honest. This is
    # now only reachable for a copy that was neither cloned nor built by hatch_build.py.
    lonely = tmp_path / "nowhere" / "ce_installer"
    lonely.mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(lonely / "cli.py"))
    monkeypatch.setattr(cli, "BUILT_IN_CHART_VERSION", "")
    assert cli.installer_version() == UNKNOWN_VERSION


def bake_version(monkeypatch, value):
    """Stand in for the value hatch_build.py writes into the wheel."""
    monkeypatch.setattr(cli, "BUILT_IN_CHART_VERSION", value)


def test_an_installed_copy_reports_the_version_baked_in_at_build_time(monkeypatch, tmp_path):
    # The uvx install has no chart beside it and used to report "unknown", which is the
    # whole reason the build hook exists.
    lonely = tmp_path / "site-packages" / "ce_installer"
    lonely.mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(lonely / "cli.py"))
    bake_version(monkeypatch, "1.2.3-rc.4")
    assert cli.installer_version() == "1.2.3-rc.4"


def test_a_checkouts_chart_wins_over_a_stale_baked_version(monkeypatch, tmp_path):
    # A developer editing Chart.yaml must see the new value without rebuilding, so the
    # baked copy can only ever be a fallback.
    package = tmp_path / "repo" / "scripts" / "ce_installer"
    package.mkdir(parents=True)
    chart = tmp_path / "repo" / "charts" / "mlrun-ce"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("version: 5.0.0\n")

    monkeypatch.setattr(cli, "__file__", str(package / "cli.py"))
    bake_version(monkeypatch, "1.2.3-rc.4")
    assert cli.installer_version() == "5.0.0"


# ------------------------------------------------------------------------------------------
# The build hook that puts that baked version there
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chart_version", "expected"),
    [
        ("0.12.0", "0.12.0"),
        # Helm speaks SemVer and PEP 440 rejects its pre-release spelling outright, so this
        # translation is what lets the distribution carry a real version at all.
        ("0.12.0-rc.12", "0.12.0rc12"),
        ("0.12.0-rc12", "0.12.0rc12"),
        ("1.0.0-beta.2", "1.0.0b2"),
        ("1.0.0-alpha.1", "1.0.0a1"),
        # Anything unrecognised falls back rather than guessing: a wrong version is worse
        # than an absent one, because it silently misidentifies what a user is running.
        ("not-a-version", "0.0.0"),
        ("0.12", "0.0.0"),
        ("", "0.0.0"),
    ],
)
def test_chart_versions_are_translated_for_python_packaging(chart_version, expected):
    assert hatch_build.to_pep440(chart_version) == expected


def test_the_hook_reads_the_real_chart_in_this_repo():
    # Pins the ../charts relative path the hook depends on; moving either tree breaks the
    # version of every installed copy, and nothing else would notice.
    assert hatch_build.read_chart_version(REPO_ROOT / "scripts") == installer_chart_version()


def test_a_build_without_a_chart_degrades_instead_of_failing(tmp_path):
    # An sdist of scripts/ alone has no ../charts. Reporting 0.0.0 is the old behaviour;
    # failing the build would be worse.
    (tmp_path / "scripts").mkdir()
    assert hatch_build.read_chart_version(tmp_path / "scripts") == ""
    assert hatch_build.to_pep440("") == "0.0.0"


# ------------------------------------------------------------------------------------------
# Flags and precedence
# ------------------------------------------------------------------------------------------


def test_ce_version_stores_the_supplied_value(ran):
    _, settings = ran("--ce-version", "0.11.0")
    assert settings.ce_version == "0.11.0"


@pytest.mark.parametrize("option", ["--ce-version", "--chart-path", "--config"])
def test_an_option_needing_a_value_rejects_a_following_flag(option):
    # click would otherwise happily swallow "--dry-run" as the value.
    with pytest.raises(InstallerError) as excinfo:
        cli.normalize_argv([option, "--dry-run"])
    assert excinfo.value.code == 1


@pytest.mark.parametrize("option", ["--ce-version", "--chart-path", "--config"])
def test_an_option_needing_a_value_rejects_a_missing_value(option):
    with pytest.raises(InstallerError):
        cli.normalize_argv([option])


def test_values_file_rejects_an_empty_value():
    with pytest.raises(InstallerError):
        cli.normalize_argv(["-f", ""])


def test_values_file_rejects_a_flag_looking_value():
    # bash accepted this, so `-f --dry-run` silently installed a values file named
    # "--dry-run" and dropped the flag. No real path begins with a dash.
    with pytest.raises(InstallerError):
        cli.normalize_argv(["-f", "--odd-name.yaml"])


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--dry-run", "dry_run"),
        ("--non-interactive", "non_interactive"),
        ("--skip-secret", "skip_registry_secret"),
        ("--skip-validators", "skip_validators"),
        ("--local-registry", "local_registry"),
        ("--disable-spark", "disable_spark"),
        ("--disable-mpi", "disable_mpi"),
        ("--disable-system-monitoring", "disable_system_monitoring"),
        ("--disable-model-monitoring", "disable_model_monitoring"),
        ("--skip-dependency-update", "skip_dependency_update"),
        ("--show-progress", "show_progress"),
    ],
)
def test_boolean_flags_set_their_field(ran, flag, attribute):
    _, settings = ran(flag)
    assert getattr(settings, attribute) is True


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--dry-run", "dry_run"),
        ("--non-interactive", "non_interactive"),
        ("--local-registry", "local_registry"),
        ("--disable-spark", "disable_spark"),
    ],
)
def test_boolean_flags_default_off(ran, flag, attribute):
    _, settings = ran()
    assert getattr(settings, attribute) is False


@pytest.mark.parametrize(
    ("env_var", "attribute"),
    [
        ("DRY_RUN", "dry_run"),
        ("SKIP_VALIDATORS", "skip_validators"),
        ("SKIP_REGISTRY_SECRET", "skip_registry_secret"),
        ("LOCAL_REGISTRY", "local_registry"),
        ("DISABLE_SPARK", "disable_spark"),
    ],
)
def test_an_env_var_enables_a_toggle_without_the_flag(ran, monkeypatch, env_var, attribute):
    monkeypatch.setenv(env_var, "true")
    _, settings = ran()
    assert getattr(settings, attribute) is True


def test_only_the_exact_string_true_counts_as_an_env_toggle(ran, monkeypatch):
    # Mirrors bash's [[ "$VAR" == "true" ]]; "1" or "yes" must not silently enable things.
    monkeypatch.setenv("DRY_RUN", "1")
    _, settings = ran()
    assert settings.dry_run is False


def test_ci_implies_non_interactive(ran, monkeypatch):
    # A CI job has nobody to answer a prompt; hanging there is worse than failing.
    monkeypatch.setenv("CI", "true")
    _, settings = ran()
    assert settings.non_interactive is True


def test_without_ci_the_run_stays_interactive(ran):
    _, settings = ran()
    assert settings.non_interactive is False


# ------------------------------------------------------------------------------------------
# prompt_or_env
# ------------------------------------------------------------------------------------------


def test_prompt_returns_the_default_when_non_interactive():
    settings = Settings(non_interactive=True)
    assert prompt_or_env(settings, "SOME_VAR", "Some prompt", "fallback") == "fallback"


def test_prompt_fails_when_non_interactive_with_no_default():
    settings = Settings(non_interactive=True)
    with pytest.raises(InstallerError):
        prompt_or_env(settings, "SOME_VAR", "Some prompt")


def test_env_var_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "from-env")
    settings = Settings(non_interactive=True)
    assert prompt_or_env(settings, "SOME_VAR", "Some prompt", "fallback") == "from-env"


def test_an_empty_env_var_counts_as_unset(monkeypatch):
    # Mirrors bash's ${VAR:-default}: exporting VAR="" must not defeat the default. Asserted
    # on env_str rather than through prompt_or_env, which returns the default for any falsy
    # value and so would pass even if env_str stopped doing this.
    monkeypatch.setenv("SOME_VAR", "")
    assert env_str("SOME_VAR", "fallback") == "fallback"
    monkeypatch.setenv("SOME_VAR", "actual")
    assert env_str("SOME_VAR", "fallback") == "actual"


def test_an_empty_env_var_does_not_blank_out_a_settings_default(monkeypatch):
    # The failure this prevents: `export NAMESPACE=` leaving the installer targeting the
    # empty namespace instead of "mlrun".
    monkeypatch.setenv("NAMESPACE", "")
    monkeypatch.setenv("HELM_TIMEOUT", "")
    settings = Settings()
    assert settings.namespace == "mlrun"
    assert settings.helm_timeout == "960s"


def test_a_prompt_default_is_used_when_the_env_var_is_empty(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "")
    settings = Settings(non_interactive=True)
    assert prompt_or_env(settings, "SOME_VAR", "Some prompt", "fallback") == "fallback"


# ------------------------------------------------------------------------------------------
# --enable-otel: a MODE names a complete state, so order decides the outcome
# ------------------------------------------------------------------------------------------

OFF = (False, False, False, False)


def fold(*argv, start=OFF):
    return tuple(cli.resolve_otel_flags(list(argv), start))


def test_bare_enable_otel_turns_everything_on():
    assert fold("--enable-otel") == (True, True, True, True)


def test_enable_otel_full_matches_the_bare_form():
    assert fold("--enable-otel", "full") == fold("--enable-otel")


def test_enable_otel_collector_is_the_metrics_pipeline_only():
    assert fold("--enable-otel", "collector") == (True, True, False, False)


def test_enable_otel_off_turns_everything_off():
    assert fold("--enable-otel", "off", start=(True, True, True, True)) == OFF


def test_a_mode_overrides_earlier_granular_flags():
    # The crux: a MODE names a complete state, so `collector` has to mean "no
    # auto-instrumentation" even when something earlier turned it on.
    assert fold("--enable-otel-instrumentation", "--enable-otel", "collector") == (
        True,
        True,
        False,
        False,
    )


def test_a_granular_flag_after_a_mode_adds_to_it():
    assert fold("--enable-otel", "collector", "--enable-otel-instrumentation") == (
        True,
        True,
        False,
        True,
    )


def test_a_mode_overrides_the_environment():
    assert fold("--enable-otel", "collector", start=(True, True, True, True)) == (
        True,
        True,
        False,
        False,
    )


@pytest.mark.parametrize(
    ("flag", "index"),
    [
        ("--enable-otel-operator", 0),
        ("--enable-otel-collector", 1),
        ("--enable-otel-namespace-label", 2),
        ("--enable-otel-instrumentation", 3),
    ],
)
def test_each_granular_flag_sets_only_its_own_toggle(flag, index):
    expected = [False, False, False, False]
    expected[index] = True
    assert fold(flag) == tuple(expected)


def test_an_invalid_otel_mode_is_rejected():
    with pytest.raises(InstallerError):
        fold("--enable-otel", "bogus")


def test_enable_otel_does_not_swallow_the_next_flag_as_its_mode():
    # The value is optional, so it may only be consumed when it is not another flag.
    assert fold("--enable-otel", "--dry-run") == (True, True, True, True)
    assert cli.normalize_argv(["--enable-otel", "--dry-run"]) == ["--enable-otel=full", "--dry-run"]


def test_enable_ingress_without_a_class_keeps_the_default(ran):
    _, settings = ran("--enable-ingress")
    assert settings.enable_ingress is True
    assert settings.ingress_class == "nginx"


def test_enable_ingress_takes_an_optional_class(ran):
    _, settings = ran("--enable-ingress", "traefik")
    assert settings.enable_ingress is True
    assert settings.ingress_class == "traefik"


def test_enable_ingress_does_not_swallow_the_next_flag_as_a_class(ran):
    _, settings = ran("--enable-ingress", "--dry-run")
    assert settings.enable_ingress is True
    assert settings.ingress_class == "nginx"
    assert settings.dry_run is True


def test_enable_ingress_does_not_swallow_a_following_short_option(ran, tmp_path):
    # bash tested `!= --*`, so a single-dash option was fair game as the class: this read
    # "-f" as the ingress class and left "values.yaml" stranded as a bare word.
    values = tmp_path / "values.yaml"
    values.write_text("global: {}\n")

    _, settings = ran("--enable-ingress", "-f", str(values))

    assert settings.enable_ingress is True
    assert settings.ingress_class == "nginx"
    assert settings.values_file == str(values)


def test_enable_otel_does_not_swallow_a_following_short_option(ran, tmp_path):
    values = tmp_path / "values.yaml"
    values.write_text("global: {}\n")

    _, settings = ran("--enable-otel", "-f", str(values))

    # Bare --enable-otel means full, and "-f" is an option rather than the MODE.
    assert settings.enable_otel_instrumentation is True
    assert settings.values_file == str(values)


@pytest.mark.parametrize("flag", ["--chart-path", "--ce-version", "--config"])
def test_a_value_requiring_option_rejects_a_following_short_option(flag):
    with pytest.raises(InstallerError):
        cli.normalize_argv([flag, "-f"])


# ------------------------------------------------------------------------------------------
# Version floors
# ------------------------------------------------------------------------------------------


def test_a_malformed_version_floor_is_rejected_at_startup(ran, monkeypatch):
    # Better to fail immediately than to silently enforce nothing.
    monkeypatch.setenv("MIN_K8S_VERSION", "1.x")
    code, settings = ran()
    assert code == 1
    assert settings is None


def test_an_empty_version_floor_means_no_floor(ran, monkeypatch):
    monkeypatch.setenv("MIN_K8S_VERSION", "")
    code, settings = ran()
    assert code == 0
    assert settings.min_k8s_major is None


def test_the_helm_floor_defaults_to_the_charts_stated_requirement(ran):
    _, settings = ran()
    assert (settings.min_helm_major, settings.min_helm_minor) == (3, 6)


# ------------------------------------------------------------------------------------------
# Run order
# ------------------------------------------------------------------------------------------


def test_hard_clean_without_uninstall_is_refused(monkeypatch):
    # On its own it reads like a cleanup flag; requiring --uninstall keeps a destructive
    # PVC deletion from being one typo away.
    settings = Settings(hard_clean=True, uninstall=False)
    monkeypatch.setattr(cli, "load_config", lambda s: None)
    with pytest.raises(InstallerError):
        cli.execute(settings)


def test_uninstall_skips_every_install_step(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "do_uninstall", lambda s: called.append("uninstall"))
    monkeypatch.setattr(cli, "check_requirements", lambda s: called.append("requirements"))
    monkeypatch.setattr(cli, "helm_install", lambda s: called.append("install"))

    cli.execute(Settings(uninstall=True))

    assert called == ["uninstall"]


def test_a_values_file_alone_skips_secret_creation(monkeypatch, tmp_path):
    values = tmp_path / "my-values.yaml"
    values.write_text("global: {}\n")
    called = []

    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "ensure_namespace", lambda s: None)
    monkeypatch.setattr(cli, "run_validators", lambda s: None)
    monkeypatch.setattr(cli, "helm_install", lambda s: None)
    monkeypatch.setattr(cli, "create_registry_secret", lambda s: called.append("secret"))
    monkeypatch.setattr(cli, "gather_install_params", lambda s: called.append("params"))

    cli.execute(Settings(values_file=str(values)))

    # -f alone is self-contained: the file must already reference an existing secret.
    assert called == []


def test_a_values_file_with_config_still_creates_the_secret(monkeypatch, tmp_path):
    values = tmp_path / "my-values.yaml"
    values.write_text("global: {}\n")
    config = tmp_path / "ce-config.yaml"
    config.write_text("installer: {}\n")
    called = []

    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "ensure_namespace", lambda s: None)
    monkeypatch.setattr(cli, "run_validators", lambda s: None)
    monkeypatch.setattr(cli, "helm_install", lambda s: None)
    monkeypatch.setattr(cli, "create_registry_secret", lambda s: called.append("secret"))
    monkeypatch.setattr(cli, "gather_install_params", lambda s: called.append("params"))

    cli.execute(Settings(values_file=str(values), config_file=str(config)))

    # Composition, not exclusivity: --config still drives secret creation so its curated
    # fields have something to layer --set on.
    assert called == ["secret", "params"]


def test_a_missing_values_file_is_refused_before_anything_runs(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "check_requirements", lambda s: called.append("requirements"))
    monkeypatch.setattr(cli, "ensure_namespace", lambda s: called.append("namespace"))
    monkeypatch.setattr(cli, "deploy_local_registry", lambda s: called.append("registry"))
    monkeypatch.setattr(cli, "resolve_external_host", lambda s: None)

    with pytest.raises(InstallerError):
        cli.execute(Settings(values_file="/nonexistent/values.yaml", local_registry=True))

    # The check used to sit after ensure_namespace and deploy_local_registry, so a mistyped
    # -f created a namespace and a running registry Deployment on the way to reporting that
    # the file does not exist — cluster state left behind by a run that never installed.
    assert called == [], f"cluster was touched before the path was validated: {called}"


def test_skip_secret_verifies_the_existing_one_instead(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "ensure_namespace", lambda s: None)
    monkeypatch.setattr(cli, "run_validators", lambda s: None)
    monkeypatch.setattr(cli, "helm_install", lambda s: None)
    monkeypatch.setattr(cli, "gather_install_params", lambda s: None)
    monkeypatch.setattr(cli, "create_registry_secret", lambda s: called.append("create"))
    monkeypatch.setattr(cli, "verify_existing_registry_secret", lambda s: called.append("verify"))

    cli.execute(Settings(skip_registry_secret=True))

    # "Use the existing secret" has to confirm one exists, or the install fails later at
    # image pull with a much less obvious error.
    assert called == ["verify"]


def test_skip_validators_bypasses_the_checks(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "load_config", lambda s: None)
    monkeypatch.setattr(cli, "check_requirements", lambda s: None)
    monkeypatch.setattr(cli, "ensure_namespace", lambda s: None)
    monkeypatch.setattr(cli, "create_registry_secret", lambda s: None)
    monkeypatch.setattr(cli, "gather_install_params", lambda s: None)
    monkeypatch.setattr(cli, "helm_install", lambda s: None)
    monkeypatch.setattr(cli, "run_validators", lambda s: called.append("validators"))

    cli.execute(Settings(skip_validators=True))

    assert called == []


# ------------------------------------------------------------------------------------------
# Output
# ------------------------------------------------------------------------------------------


def test_log_output_is_plain_text_when_stdout_is_not_a_terminal(capsys):
    # Piped or redirected runs — CI logs, `| tee install.log` — must read as text. rich
    # suppresses colour off-terminal on its own; this pins the user-visible property.
    console.log_info("hello")
    console.log_warn("careful")
    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out
    assert "[INFO] hello" in captured.out
    assert "[WARN] careful" in captured.out


def test_error_output_goes_to_stderr(capsys):
    # So `installer ... > install.log` still shows failures on the terminal.
    console.log_error("it broke")
    captured = capsys.readouterr()
    assert "[ERROR] it broke" in captured.err
    assert "it broke" not in captured.out


def test_log_messages_containing_brackets_are_not_parsed_as_markup(capsys):
    # jsonpath expressions and "[ERROR]" appear in real messages; rich would otherwise try
    # to read them as style tags and drop them.
    console.log_info("jsonpath={range .items[*]}{.metadata.name}{end}")
    assert ".items[*]" in capsys.readouterr().out
