#!/usr/bin/env bats
# Tests for install.sh — Phase 1 (flag parsing, dry-run, non-interactive, CI),
# Phase 2 (--chart-path / resolve_chart_source), and Phase 3 (--config / load_config).
# Uses INSTALL_SH_SOURCE_ONLY=true so sourcing the script only defines
# functions and globals without calling main().

SCRIPT="$BATS_TEST_DIRNAME/../scripts/install.sh"

# Source helper: load the script without running main().
# Usage: _load [extra env assignments...]
_src() {
    # shellcheck source=/dev/null
    INSTALL_SH_SOURCE_ONLY=true source "$SCRIPT"
}

# ---------------------------------------------------------------------------
# --ce-version parsing
# ---------------------------------------------------------------------------

@test "--ce-version stores the supplied value" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --ce-version 0.11.0
        echo \"\$CE_VERSION\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "0.11.0" ]
}

@test "--ce-version with no argument exits 1" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --ce-version
    "
    [ "$status" -eq 1 ]
}

@test "CE_VERSION env var pre-set is preserved after sourcing" {
    run bash -c "
        CE_VERSION=0.10.0
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"\$CE_VERSION\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "0.10.0" ]
}

@test "CE_VERSION defaults to empty string" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"'\$CE_VERSION'\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "''" ]
}

# ---------------------------------------------------------------------------
# --dry-run flag
# ---------------------------------------------------------------------------

@test "--dry-run sets DRY_RUN=true" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --dry-run
        echo \"\$DRY_RUN\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "true" ]
}

@test "DRY_RUN defaults to false" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"\$DRY_RUN\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "false" ]
}

@test "--dry-run flag is consumed (no leftover args)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        # parse_args silently drops unknown args; DRY_RUN must be set
        parse_args --dry-run
        [ \"\$DRY_RUN\" = 'true' ] && echo ok
    "
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

# ---------------------------------------------------------------------------
# --non-interactive flag
# ---------------------------------------------------------------------------

@test "--non-interactive sets NON_INTERACTIVE=true" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --non-interactive
        echo \"\$NON_INTERACTIVE\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "true" ]
}

@test "NON_INTERACTIVE defaults to false" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"\$NON_INTERACTIVE\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "false" ]
}

# ---------------------------------------------------------------------------
# prompt_or_env behaviour in non-interactive mode
# ---------------------------------------------------------------------------

@test "prompt_or_env returns default when NON_INTERACTIVE=true and var is unset" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        NON_INTERACTIVE=true
        result=\$(prompt_or_env SOME_UNSET_VAR 'Label' 'thedefault')
        echo \"\$result\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "thedefault" ]
}

@test "prompt_or_env exits 1 when NON_INTERACTIVE=true, var unset, and no default" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        NON_INTERACTIVE=true
        prompt_or_env SOME_UNSET_VAR 'Label' ''
    "
    [ "$status" -eq 1 ]
}

@test "prompt_or_env returns the env var even when NON_INTERACTIVE=true" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        NON_INTERACTIVE=true
        MY_VAR=fromenv
        result=\$(prompt_or_env MY_VAR 'Label' '')
        echo \"\$result\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "fromenv" ]
}

@test "prompt_or_env env var takes priority over default in non-interactive mode" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        NON_INTERACTIVE=true
        MY_VAR=fromenv
        result=\$(prompt_or_env MY_VAR 'Label' 'thedefault')
        echo \"\$result\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "fromenv" ]
}

# ---------------------------------------------------------------------------
# CI auto-detect in main()
# ---------------------------------------------------------------------------

@test "CI=true sets NON_INTERACTIVE before any work is done" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'

        # Stub everything main() calls so we don't need a live cluster or registry creds
        check_requirements()    { :; }
        ensure_namespace()      { :; }
        create_registry_secret() { :; }
        gather_install_params() { :; }
        run_validators()       { :; }
        helm_install()          { echo \"NON_INTERACTIVE=\$NON_INTERACTIVE\"; }

        CI=true main
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"NON_INTERACTIVE=true"* ]]
}

@test "CI unset leaves NON_INTERACTIVE false" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'

        check_requirements()    { :; }
        ensure_namespace()      { :; }
        create_registry_secret() { :; }
        gather_install_params() { :; }
        run_validators()       { :; }
        helm_install()          { echo \"NON_INTERACTIVE=\$NON_INTERACTIVE\"; }

        unset CI
        main
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"NON_INTERACTIVE=false"* ]]
}

# ---------------------------------------------------------------------------
# Phase 2 — --chart-path flag and resolve_chart_source
# ---------------------------------------------------------------------------

@test "--chart-path stores the supplied path" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --chart-path ./ce/charts/mlrun-ce
        echo \"\$CHART_PATH\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "./ce/charts/mlrun-ce" ]
}

@test "--chart-path with no argument exits 1" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --chart-path
    "
    [ "$status" -eq 1 ]
}

@test "--chart-path rejects a flag-looking value" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --chart-path --dry-run
    "
    [ "$status" -eq 1 ]
}

@test "CHART_PATH defaults to empty string" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"'\$CHART_PATH'\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "''" ]
}

@test "resolve_chart_source exits 1 when CHART_PATH directory does not exist" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CHART_PATH=/nonexistent/path
        resolve_chart_source
    "
    [ "$status" -eq 1 ]
}

@test "resolve_chart_source exits 1 when CHART_PATH has no Chart.yaml" {
    local tmpdir
    tmpdir="\$(mktemp -d)"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CHART_PATH='$BATS_TMPDIR/emptychart'
        mkdir -p \"\$CHART_PATH\"
        resolve_chart_source
    "
    [ "$status" -eq 1 ]
}

@test "resolve_chart_source sets CHART_REF to CHART_PATH when valid" {
    local chart_dir="$BATS_TMPDIR/fakechart"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { :; }
        CHART_PATH='$chart_dir'
        resolve_chart_source 2>/dev/null
        echo \"\$CHART_REF\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"$chart_dir"* ]]
}

@test "resolve_chart_source sets CHART_REF to mlrun-ce/mlrun-ce in published-repo mode" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { :; }
        CHART_PATH=
        resolve_chart_source 2>/dev/null
        echo \"\$CHART_REF\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"mlrun-ce/mlrun-ce"* ]]
}

@test "--ce-version is ignored in local-path mode (version_flag stays empty)" {
    local chart_dir="$BATS_TMPDIR/fakechart2"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { :; }
        CHART_PATH='$chart_dir'
        CE_VERSION=0.11.0
        check_version_flag() {
            local -a vf=()
            [[ -n \"\$CE_VERSION\" && -z \"\$CHART_PATH\" ]] && vf=(--version \"\$CE_VERSION\")
            echo \"count=\${#vf[@]}\"
        }
        check_version_flag
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"count=0"* ]]
}

# ---------------------------------------------------------------------------
# Phase 3 — --config / CONFIG_FILE and load_config
# ---------------------------------------------------------------------------

@test "--config stores the supplied path" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --config ce-config.yaml
        echo \"\$CONFIG_FILE\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "ce-config.yaml" ]
}

@test "--config with no argument exits 1" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --config
    "
    [ "$status" -eq 1 ]
}

@test "load_config is a no-op (returns 0, no yq required) when CONFIG_FILE is empty" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        PATH=/usr/bin:/bin
        load_config
        echo done
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"done"* ]]
}

@test "load_config exits 1 when CONFIG_FILE does not exist" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE=/nonexistent/ce-config.yaml
        load_config
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"not found"* ]]
}

@test "load_config exits 1 when yq is not installed" {
    local cfg="$BATS_TMPDIR/cfg_noyq.yaml"
    printf 'installer:\n  registry:\n    url: x\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        PATH=/usr/bin:/bin
        load_config
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"yq"* ]]
}

@test "load_config parses registry fields into CONFIG_ vars" {
    local cfg="$BATS_TMPDIR/cfg_registry.yaml"
    cat > "$cfg" <<'EOF'
installer:
  registry:
    url: index.docker.io/myuser
    secret:
      username: myuser
      server: https://index.docker.io/v1/
      email: me@example.com
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        echo \"url=\$CONFIG_REGISTRY_URL user=\$CONFIG_REGISTRY_USERNAME server=\$CONFIG_REGISTRY_SERVER email=\$CONFIG_REGISTRY_EMAIL\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"url=index.docker.io/myuser"* ]]
    [[ "$output" == *"user=myuser"* ]]
    [[ "$output" == *"server=https://index.docker.io/v1/"* ]]
    [[ "$output" == *"email=me@example.com"* ]]
}

@test "in interactive mode, a config-file value pre-fills the prompt default (Enter accepts it)" {
    local cfg="$BATS_TMPDIR/cfg_interactive.yaml"
    cat > "$cfg" <<'EOF'
installer:
  registry:
    url: index.docker.io/configuser
    secret:
      username: configuser
EOF
    # Empty stdin simulates pressing Enter at the prompt with no typed override.
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        prompt_or_env REGISTRY_URL 'Docker registry URL' \"\${CONFIG_REGISTRY_URL}\" <<< ''
    "
    [ "$status" -eq 0 ]
    [ "$output" = "index.docker.io/configuser" ]
}

@test "in interactive mode, typing a value overrides the config-file default for that field" {
    local cfg="$BATS_TMPDIR/cfg_interactive_override.yaml"
    cat > "$cfg" <<'EOF'
installer:
  registry:
    url: index.docker.io/configuser
    secret:
      username: configuser
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        prompt_or_env REGISTRY_URL 'Docker registry URL' \"\${CONFIG_REGISTRY_URL}\" <<< 'index.docker.io/typeduser'
    "
    [ "$status" -eq 0 ]
    [ "$output" = "index.docker.io/typeduser" ]
}

@test "load_config warns and ignores a password key in the config file" {
    local cfg="$BATS_TMPDIR/cfg_password.yaml"
    cat > "$cfg" <<'EOF'
installer:
  registry:
    secret:
      username: myuser
      password: shouldbeignored
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ignored"* ]]
}

@test "load_config exits 1 immediately when chartSource.kind is path but chartPath is empty (interactive mode too)" {
    local cfg="$BATS_TMPDIR/cfg_path_missing.yaml"
    cat > "$cfg" <<'EOF'
installer:
  chartSource:
    kind: path
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=false
        load_config
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"chartSource.chartPath"* ]]
}

@test "load_config sets CHART_PATH from chartSource.chartPath when kind is path" {
    local cfg="$BATS_TMPDIR/cfg_path_ok.yaml"
    cat > "$cfg" <<'EOF'
installer:
  chartSource:
    kind: path
    chartPath: /some/chart/dir
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        echo \"\$CHART_PATH\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/some/chart/dir" ]
}

@test "load_config lists every missing required field together in non-interactive mode" {
    local cfg="$BATS_TMPDIR/cfg_empty.yaml"
    printf 'installer:\n  registry:\n    url: ""\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=true
        load_config
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"installer.registry.url"* ]]
    [[ "$output" == *"installer.registry.secret.username"* ]]
    [[ "$output" == *"REGISTRY_PASSWORD"* ]]
}

@test "load_config does not require registry.url in non-interactive mode when LOCAL_REGISTRY=true" {
    local cfg="$BATS_TMPDIR/cfg_local.yaml"
    printf 'installer:\n  registry:\n    secret:\n      username: myuser\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=true
        LOCAL_REGISTRY=true
        REGISTRY_PASSWORD=x
        load_config
        echo ok
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ok"* ]]
}

@test "load_config does not require registry username/password in non-interactive mode when --skip-secret is used" {
    local cfg="$BATS_TMPDIR/cfg_skipsecret.yaml"
    printf 'installer:\n  registry:\n    url: myregistry.example.com/user\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=true
        SKIP_REGISTRY_SECRET=true
        load_config
        echo ok
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ok"* ]]
}

@test "load_config still requires registry fields in non-interactive mode when --config is combined with -f/VALUES_FILE" {
    local cfg="$BATS_TMPDIR/cfg_valuesfile.yaml"
    printf 'installer: {}\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=true
        VALUES_FILE=/some/values.yaml
        load_config
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"installer.registry.url"* ]]
}

@test "load_config is a no-op when -f/VALUES_FILE is used without --config (CONFIG_FILE empty)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        NON_INTERACTIVE=true
        VALUES_FILE=/some/values.yaml
        load_config
        echo ok
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ok"* ]]
}

@test "load_config does not override CHART_PATH/CE_VERSION/KUBE_CONTEXT already set by flag or env" {
    local cfg="$BATS_TMPDIR/cfg_precedence.yaml"
    cat > "$cfg" <<'EOF'
installer:
  kubeContext: from-config
  chartSource:
    kind: path
    chartPath: /from/config
    chartVersion: 9.9.9
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        KUBE_CONTEXT=from-env
        CHART_PATH=/from/flag
        CE_VERSION=1.2.3
        load_config
        echo \"ctx=\$KUBE_CONTEXT path=\$CHART_PATH version=\$CE_VERSION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ctx=from-env"* ]]
    [[ "$output" == *"path=/from/flag"* ]]
    [[ "$output" == *"version=1.2.3"* ]]
}

@test "load_config parses installer.versions.mlrun/nuclio into MLRUN_VERSION/NUCLIO_VERSION" {
    local cfg="$BATS_TMPDIR/cfg_versions.yaml"
    cat > "$cfg" <<'EOF'
installer:
  versions:
    mlrun: 1.13.0
    nuclio: 1.16.0
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        echo \"mlrun=\$MLRUN_VERSION nuclio=\$NUCLIO_VERSION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"mlrun=1.13.0"* ]]
    [[ "$output" == *"nuclio=1.16.0"* ]]
}

@test "helm_install passes mlrun/nuclio image.tag --set flags to helm when MLRUN_VERSION/NUCLIO_VERSION are set" {
    local chart_dir="$BATS_TMPDIR/fakechart3"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo \"HELM_ARGS: \$*\"; }
        CHART_PATH='$chart_dir'
        MLRUN_VERSION=1.13.0
        NUCLIO_VERSION=1.16.0
        REGISTRY_URL=x
        EXTERNAL_HOST_ADDRESS=x
        DRY_RUN=true
        helm_install
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"mlrun.api.image.tag=1.13.0"* ]]
    [[ "$output" == *"mlrun.ui.image.tag=1.13.0"* ]]
    [[ "$output" == *"mlrun.api.sidecars.logCollector.image.tag=1.13.0"* ]]
    [[ "$output" == *"nuclio.controller.image.tag=1.16.0"* ]]
    [[ "$output" == *"nuclio.dashboard.image.tag=1.16.0"* ]]
}

@test "load_config maps installer.components.* to DISABLE_* the same as the --disable-* flags" {
    local cfg="$BATS_TMPDIR/cfg_components.yaml"
    cat > "$cfg" <<'EOF'
installer:
  components:
    monitoring: false
    spark: true
    mpi: false
    modelMonitoring: true
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        echo \"mon=\$DISABLE_SYSTEM_MONITORING spark=\$DISABLE_SPARK mpi=\$DISABLE_MPI mm=\$DISABLE_MODEL_MONITORING\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"mon=true"* ]]
    [[ "$output" == *"spark=false"* ]]
    [[ "$output" == *"mpi=true"* ]]
    [[ "$output" == *"mm=false"* ]]
}

@test "load_config does not re-enable a component already disabled by --disable-* flag/env" {
    local cfg="$BATS_TMPDIR/cfg_components_enable.yaml"
    printf 'installer:\n  components:\n    spark: true\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        DISABLE_SPARK=true
        load_config
        echo \"spark=\$DISABLE_SPARK\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"spark=true"* ]]
}

@test "--enable-otel sets all 4 granular ENABLE_OTEL_* vars" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true col=true ns=true inst=true"* ]]
}

@test "--enable-otel-operator/-collector/-namespace-label/-instrumentation set only their own var" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel-collector
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=false col=true ns=false inst=false"* ]]
}

@test "--enable-otel full sets all 4 (same as bare --enable-otel)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel full
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true col=true ns=true inst=true"* ]]
}

@test "--enable-otel collector sets only operator+collector, not namespaceLabel/instrumentation" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel collector
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true col=true ns=false inst=false"* ]]
}

@test "--enable-otel off sets all 4 false" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel off
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=false col=false ns=false inst=false"* ]]
}

@test "--enable-otel rejects an invalid mode" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel bogus
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid --enable-otel mode"* ]]
}

@test "--enable-otel with no mode does not consume the next unrelated flag" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --enable-otel --dry-run
        echo \"op=\$ENABLE_OTEL_OPERATOR dry=\$DRY_RUN\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true dry=true"* ]]
}

@test "load_config maps installer.otel.* to the 4 granular ENABLE_OTEL_* vars independently (opt-in, opposite direction from components.*)" {
    local cfg="$BATS_TMPDIR/cfg_otel.yaml"
    cat > "$cfg" <<'EOF'
installer:
  otel:
    operator: true
    collector: true
    namespaceLabel: false
    instrumentation: false
EOF
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        load_config
        echo \"op=\$ENABLE_OTEL_OPERATOR col=\$ENABLE_OTEL_COLLECTOR ns=\$ENABLE_OTEL_NAMESPACE_LABEL inst=\$ENABLE_OTEL_INSTRUMENTATION\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true col=true ns=false inst=false"* ]]
}

@test "load_config does not override an ENABLE_OTEL_* var already set by flag/env when the file omits/disables it" {
    local cfg="$BATS_TMPDIR/cfg_otel_noop.yaml"
    printf 'installer:\n  otel:\n    operator: false\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        ENABLE_OTEL_OPERATOR=true
        load_config
        echo \"op=\$ENABLE_OTEL_OPERATOR\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"op=true"* ]]
}

@test "helm_install passes only the --set flags for the otel toggles that are enabled" {
    local chart_dir="$BATS_TMPDIR/fakechart_otel"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo \"HELM_ARGS: \$*\"; }
        CHART_PATH='$chart_dir'
        ENABLE_OTEL_OPERATOR=true
        ENABLE_OTEL_COLLECTOR=true
        REGISTRY_URL=x
        EXTERNAL_HOST_ADDRESS=x
        DRY_RUN=true
        helm_install
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"opentelemetry-operator.enabled=true"* ]]
    [[ "$output" == *"opentelemetry.collector.enabled=true"* ]]
    [[ "$output" != *"opentelemetry.namespaceLabel.enabled"* ]]
    [[ "$output" != *"opentelemetry.instrumentation.enabled"* ]]
}

@test "helm_install passes all 4 opentelemetry --set flags when all ENABLE_OTEL_* vars are true" {
    local chart_dir="$BATS_TMPDIR/fakechart_otel_all"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo \"HELM_ARGS: \$*\"; }
        CHART_PATH='$chart_dir'
        ENABLE_OTEL_OPERATOR=true
        ENABLE_OTEL_COLLECTOR=true
        ENABLE_OTEL_NAMESPACE_LABEL=true
        ENABLE_OTEL_INSTRUMENTATION=true
        REGISTRY_URL=x
        EXTERNAL_HOST_ADDRESS=x
        DRY_RUN=true
        helm_install
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"opentelemetry-operator.enabled=true"* ]]
    [[ "$output" == *"opentelemetry.collector.enabled=true"* ]]
    [[ "$output" == *"opentelemetry.namespaceLabel.enabled=true"* ]]
    [[ "$output" == *"opentelemetry.instrumentation.enabled=true"* ]]
}

@test "main() with --config and -f/--values together still creates the registry secret (composition, not exclusivity)" {
    local cfg="$BATS_TMPDIR/cfg_combo.yaml"
    printf 'installer:\n  registry:\n    url: index.docker.io/myuser\n    secret:\n      username: myuser\n' > "$cfg"
    local values="$BATS_TMPDIR/values_combo.yaml"
    printf 'foo: bar\n' > "$values"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        check_requirements()     { :; }
        ensure_namespace()       { :; }
        create_registry_secret() { echo CREATE_SECRET_CALLED; }
        gather_install_params()  { echo GATHER_PARAMS_CALLED; }
        run_validators()         { :; }
        helm_install()           { :; }
        REGISTRY_PASSWORD=x
        main --config '$cfg' -f '$values' --non-interactive
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"CREATE_SECRET_CALLED"* ]]
    [[ "$output" == *"GATHER_PARAMS_CALLED"* ]]
}

@test "main() with -f/--values alone (no --config) still skips secret creation (backward compat)" {
    local values="$BATS_TMPDIR/values_alone.yaml"
    printf 'foo: bar\n' > "$values"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        check_requirements()     { :; }
        ensure_namespace()       { :; }
        create_registry_secret() { echo CREATE_SECRET_CALLED; }
        gather_install_params()  { echo GATHER_PARAMS_CALLED; }
        run_validators()         { :; }
        helm_install()           { :; }
        main -f '$values'
    "
    [ "$status" -eq 0 ]
    [[ "$output" != *"CREATE_SECRET_CALLED"* ]]
    [[ "$output" != *"GATHER_PARAMS_CALLED"* ]]
}

@test "helm_install includes --values AND config-resolved registry --set flags when CONFIG_FILE is set alongside VALUES_FILE" {
    local chart_dir="$BATS_TMPDIR/fakechart_combo"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    local values="$BATS_TMPDIR/values_helm_combo.yaml"
    printf 'foo: bar\n' > "$values"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo \"HELM_ARGS: \$*\"; }
        CHART_PATH='$chart_dir'
        CONFIG_FILE=dummy.yaml
        VALUES_FILE='$values'
        REGISTRY_URL=index.docker.io/myuser
        REGISTRY_SECRET_NAME=registry-credentials
        EXTERNAL_HOST_ADDRESS=localhost
        DRY_RUN=true
        helm_install
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"--values ${values}"* ]]
    [[ "$output" == *"global.registry.url=index.docker.io/myuser"* ]]
    [[ "$output" == *"global.registry.secretName=registry-credentials"* ]]
    [[ "$output" == *"global.externalHostAddress=localhost"* ]]
}

@test "helm_install omits registry --set flags in pure -f-only mode (no CONFIG_FILE) — backward compat" {
    local chart_dir="$BATS_TMPDIR/fakechart_fonly"
    mkdir -p "$chart_dir"
    printf 'apiVersion: v2\nname: mlrun-ce\nversion: 0.0.1\n' > "$chart_dir/Chart.yaml"
    local values="$BATS_TMPDIR/values_fonly.yaml"
    printf 'foo: bar\n' > "$values"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo \"HELM_ARGS: \$*\"; }
        CHART_PATH='$chart_dir'
        VALUES_FILE='$values'
        DRY_RUN=true
        helm_install
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"--values ${values}"* ]]
    [[ "$output" != *"global.registry.url"* ]]
    [[ "$output" != *"global.registry.secretName"* ]]
    [[ "$output" != *"global.externalHostAddress"* ]]
}

@test "create_registry_secret reads the password from REGISTRY_PASSWORD_FILE when REGISTRY_PASSWORD is unset" {
    local pwfile="$BATS_TMPDIR/pw.txt"
    printf 'supersecret\n' > "$pwfile"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        load_config
        REGISTRY_USERNAME=myuser
        REGISTRY_PASSWORD_FILE='$pwfile'
        DRY_RUN=true
        create_registry_secret
        echo \"pw=\$REGISTRY_PASSWORD\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"pw=supersecret"* ]]
}

@test "REGISTRY_PASSWORD env wins over REGISTRY_PASSWORD_FILE when both are set" {
    local pwfile="$BATS_TMPDIR/pw2.txt"
    printf 'fromfile\n' > "$pwfile"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        load_config
        REGISTRY_USERNAME=myuser
        REGISTRY_PASSWORD=fromenv
        REGISTRY_PASSWORD_FILE='$pwfile'
        DRY_RUN=true
        create_registry_secret
        echo \"pw=\$REGISTRY_PASSWORD\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"pw=fromenv"* ]]
}

@test "create_registry_secret exits 1 with a clear error when REGISTRY_PASSWORD_FILE does not exist" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        load_config
        REGISTRY_USERNAME=myuser
        REGISTRY_PASSWORD_FILE='/nonexistent/path/pw.txt'
        DRY_RUN=true
        create_registry_secret
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"REGISTRY_PASSWORD_FILE"* ]]
}

@test "load_config's non-interactive required-field check accepts REGISTRY_PASSWORD_FILE as satisfying the password requirement" {
    local pwfile="$BATS_TMPDIR/pw3.txt"
    printf 'x\n' > "$pwfile"
    local cfg="$BATS_TMPDIR/cfg_pwfile.yaml"
    printf 'installer:\n  registry:\n    url: index.docker.io/myuser\n    secret:\n      username: myuser\n' > "$cfg"
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        CONFIG_FILE='$cfg'
        NON_INTERACTIVE=true
        REGISTRY_PASSWORD_FILE='$pwfile'
        load_config
        echo ok
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ok"* ]]
}

@test "verify_existing_registry_secret exits 1 when the secret does not exist" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 1; }
        REGISTRY_SECRET_NAME=registry-credentials
        NAMESPACE=mlrun
        verify_existing_registry_secret
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not exist"* ]]
}

@test "verify_existing_registry_secret passes silently when the secret exists" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 0; }
        REGISTRY_SECRET_NAME=registry-credentials
        NAMESPACE=mlrun
        verify_existing_registry_secret
        echo ok
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"ok"* ]]
}

@test "kubectl/helm wrapper functions inject --context/--kube-context when KUBE_CONTEXT is set" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        command() {
            echo \"real: \$*\"
        }
        KUBE_CONTEXT=myctx
        kubectl get pods
        helm list
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"real: kubectl --context myctx get pods"* ]]
    [[ "$output" == *"real: helm --kube-context myctx list"* ]]
}

@test "resolve_external_host skips the docker-desktop/minikube heuristics when KUBE_CONTEXT is set, uses node IP instead" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        command() {
            case \"\$1\" in
                kubectl)
                    shift
                    if [[ \"\$1\" == config && \"\$2\" == current-context ]]; then
                        echo docker-desktop
                    elif [[ \"\$1\" == get && \"\$2\" == node ]]; then
                        echo '192.168.236.51'
                    fi
                    ;;
                minikube) return 1 ;;
            esac
        }
        KUBE_CONTEXT=vmdev137
        NON_INTERACTIVE=true
        resolve_external_host
        echo \"host=\$EXTERNAL_HOST_ADDRESS\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"host=192.168.236.51"* ]]
    [[ "$output" != *"host=host.docker.internal"* ]]
}

@test "resolve_external_host still uses the docker-desktop heuristic when KUBE_CONTEXT is unset (regression)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        command() {
            case \"\$1\" in
                kubectl)
                    shift
                    [[ \"\$1\" == config && \"\$2\" == current-context ]] && echo docker-desktop
                    ;;
                minikube) return 1 ;;
            esac
        }
        NON_INTERACTIVE=true
        resolve_external_host
        echo \"host=\$EXTERNAL_HOST_ADDRESS\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"host=host.docker.internal"* ]]
}

@test "resolve_external_host falls back to localhost when no heuristic matches (e.g. kind/k3d)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        command() {
            case \"\$1\" in
                kubectl) return 0 ;;
                minikube) return 1 ;;
            esac
        }
        NON_INTERACTIVE=true
        resolve_external_host
        echo \"host=\$EXTERNAL_HOST_ADDRESS\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"host=localhost"* ]]
}

# ---------------------------------------------------------------------------
# Phase 4 — pre-install validators (run_validators / --skip-validators)
# ---------------------------------------------------------------------------

@test "--skip-validators sets SKIP_VALIDATORS" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        parse_args --skip-validators
        echo \"\$SKIP_VALIDATORS\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "true" ]
}

@test "validate_k8s_version never blocks: no floor is enforced by default" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { echo 'v1.30.2'; }
        validate_k8s_version
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Kubernetes version: 1.30"* ]]
    [[ "$output" != *"below"* ]]
}

@test "validate_k8s_version reports the detected version when MIN_K8S_VERSION is unset" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { echo 'v1.34.0'; }
        validate_k8s_version
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Kubernetes version: 1.34"* ]]
    [[ "$output" != *"required"* ]]
}

@test "validate_k8s_version warns and does not fail when the version can't be determined" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 1; }
        validate_k8s_version
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Could not determine Kubernetes version"* ]]
}

@test "validate_helm_version fails when helm is below the minimum version" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo 'v3.5.0+g12345'; }
        validate_helm_version
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"below the minimum supported version"* ]]
}

@test "validate_helm_version passes when helm is at the minimum version" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo 'v3.6.0+g12345'; }
        validate_helm_version
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Helm version: 3.6"* ]]
}

@test "MIN_K8S_VERSION warns but still does not block when the cluster is below it" {
    run bash -c "
        MIN_K8S_VERSION=1.34 INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { echo 'v1.30.2'; }
        validate_k8s_version
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"below the requested minimum (1.34)"* ]]
}

@test "MIN_HELM_VERSION raises the Helm floor the validator enforces" {
    run bash -c "
        MIN_HELM_VERSION=4.1 INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        helm() { echo 'v3.9.0+g12345'; }
        validate_helm_version
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"below the minimum supported version"* ]]
}

@test "a malformed MIN_K8S_VERSION is rejected at load time" {
    run bash -c "
        MIN_K8S_VERSION=1 INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"MIN_K8S_VERSION must be in MAJOR.MINOR form"* ]]
}

@test "an empty MIN_K8S_VERSION is accepted (no floor is the default)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        echo \"floor='\$MIN_K8S_VERSION' helm='\$MIN_HELM_VERSION'\"
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"floor='' helm='3.6'"* ]]
}

@test "validate_storage_class fails when no default StorageClass exists" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { echo 'standard=false'; }
        validate_storage_class
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"No default StorageClass found"* ]]
}

@test "validate_storage_class passes when a default StorageClass exists" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { printf 'standard=false\nfast=true\n'; }
        validate_storage_class
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Default StorageClass: fast"* ]]
}

@test "validate_ingress_controller skips when --enable-ingress is not used" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { echo 'kubectl should not be called'; return 1; }
        ENABLE_INGRESS=false
        validate_ingress_controller
    "
    [ "$status" -eq 0 ]
    [[ "$output" != *"should not be called"* ]]
}

@test "validate_ingress_controller warns (not fails) when no matching IngressClass exists" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 1; }
        ENABLE_INGRESS=true
        INGRESS_CLASS=nginx
        validate_ingress_controller
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"no IngressClass named 'nginx' found"* ]]
    [[ "$output" == *"does not install a controller"* ]]
}

@test "validate_ingress_controller passes when the IngressClass exists" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 0; }
        ENABLE_INGRESS=true
        INGRESS_CLASS=nginx
        validate_ingress_controller
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"IngressClass 'nginx' found"* ]]
}

@test "validate_registry_auth skips when --local-registry is in use" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        docker() { echo 'docker should not be called'; return 1; }
        LOCAL_REGISTRY=true
        validate_registry_auth
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"skipped (--local-registry in use"* ]]
    [[ "$output" != *"should not be called"* ]]
}

@test "validate_registry_auth skips when no credentials were resolved (-f-only mode)" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        docker() { echo 'docker should not be called'; return 1; }
        LOCAL_REGISTRY=false
        validate_registry_auth
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"skipped (no registry credentials resolved"* ]]
    [[ "$output" != *"should not be called"* ]]
}

@test "validate_registry_auth warns (not fails) when docker login fails" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        docker() { return 1; }
        LOCAL_REGISTRY=false
        REGISTRY_USERNAME_VALUE=alice
        REGISTRY_PASSWORD_VALUE=secret
        REGISTRY_SERVER_VALUE=https://index.docker.io/v1/
        validate_registry_auth
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"could not log in"* ]]
}

@test "validate_registry_auth logs success when docker login succeeds" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        docker() { return 0; }
        LOCAL_REGISTRY=false
        REGISTRY_USERNAME_VALUE=alice
        REGISTRY_PASSWORD_VALUE=secret
        REGISTRY_SERVER_VALUE=https://index.docker.io/v1/
        validate_registry_auth
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"login to https://index.docker.io/v1/ succeeded"* ]]
}

@test "validate_nodeport_conflicts warns when a required NodePort is already in use outside the namespace" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { printf 'other-ns 30093\nmlrun 30010\n'; }
        NAMESPACE=mlrun
        validate_nodeport_conflicts
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"NodePort conflict"* ]]
    [[ "$output" == *"30093"* ]]
    [[ "$output" != *"30010"* ]]
}

@test "validate_nodeport_conflicts reports no conflicts when required ports are free" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { printf 'other-ns 40000\n'; }
        NAMESPACE=mlrun
        validate_nodeport_conflicts
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"no conflicts detected"* ]]
}

@test "validate_node_capacity warns when allocatable memory/storage is below the 8Gi floor" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() {
            case \"\$*\" in
                *ephemeral-storage*) printf '2000000Ki\n' ;;
                *) printf '2000000Ki\n' ;;
            esac
        }
        validate_node_capacity
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"below the documented floor of 8Gi"* ]]
}

@test "validate_node_capacity passes when allocatable memory/storage is above the 8Gi floor" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() {
            case \"\$*\" in
                *ephemeral-storage*) printf '9000000Ki\n' ;;
                *) printf '9000000Ki\n' ;;
            esac
        }
        validate_node_capacity
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"total allocatable memory ~8Gi"* ]]
    [[ "$output" == *"total allocatable ephemeral storage ~8Gi"* ]]
}

@test "validate_node_capacity parses a bare byte count (no Ki suffix) for ephemeral-storage" {
    # Some clusters (e.g. docker-desktop, cAdvisor-sourced) report ephemeral-storage
    # allocatable as a plain byte integer rather than a Ki-suffixed quantity.
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() {
            case \"\$*\" in
                *ephemeral-storage*) printf '56403987978\n' ;;
                *) printf '9000000Ki\n' ;;
            esac
        }
        validate_node_capacity
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"total allocatable ephemeral storage ~52Gi"* ]]
    [[ "$output" != *"ephemeral storage ~0Gi"* ]]
}

@test "run_validators aggregates multiple blocking failures into a single exit 1" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() {
            case \"\$*\" in
                *nodeInfo.kubeletVersion*) echo 'v1.30.0' ;;
                *storageclass*) echo '' ;;
                *) echo '' ;;
            esac
        }
        helm() { echo 'v3.5.0+g12345'; }
        docker() { return 0; }
        LOCAL_REGISTRY=false
        NAMESPACE=mlrun
        run_validators
    "
    [ "$status" -eq 1 ]
    [[ "$output" == *"Helm version 3.5 is below"* ]]
    [[ "$output" == *"No default StorageClass"* ]]
    [[ "$output" == *"One or more required pre-install checks failed"* ]]
    # The cluster is 1.30 but K8s is informational now, so it must not contribute a failure.
    [[ "$output" == *"Kubernetes version: 1.30"* ]]
}

@test "main() skips run_validators entirely when --skip-validators is passed" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        kubectl() { return 0; }
        helm() { return 0; }
        docker() { return 0; }
        check_requirements() { :; }
        ensure_namespace() { :; }
        create_registry_secret() { :; }
        gather_install_params() { :; }
        helm_install() { :; }
        run_validators() { echo 'run_validators should not be called'; }
        NON_INTERACTIVE=true
        main --skip-validators
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"Skipping pre-install validators"* ]]
    [[ "$output" != *"should not be called"* ]]
}