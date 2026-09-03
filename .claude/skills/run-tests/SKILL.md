---
name: run-tests
description: >-
  Run and extend the bats test suite for the CE installer (scripts/install.sh).
  Use when a developer asks to run installer tests, check coverage, add a new
  test, or verify a change to scripts/install.sh didn't break existing behaviour.
---

# CE installer test suite

Tests live in `tests/install_tests.bats` and use
[bats-core](https://github.com/bats-core/bats-core). They source
`scripts/install.sh` without executing it (via `INSTALL_SH_SOURCE_ONLY=true`) and
stub out external binaries so no live cluster is needed.

These cover the installer only. The chart's own tests are the other scripts in
`tests/` (`helm-template-test.sh`, `kind-test.sh`) plus `make helm-lint` — all
unrelated to this suite.

## Prerequisites

```bash
brew install bats-core   # macOS; already installed if tests have been run before
```

## Run the full suite

From the repo root:

```bash
make installer-test
# or directly:
bats tests/install_tests.bats
```

Expected output: `1..112` followed by `ok N <test-name>` for every test.

Keep the count in this file in sync when you add tests — it's the quickest way to
notice a test silently failing to register.

## Run a single test by name

```bash
bats --filter "CI=true sets NON_INTERACTIVE" tests/install_tests.bats
```

## Run with verbose output

```bash
bats --verbose-run tests/install_tests.bats
```

## How tests source the script safely

Every test opens with:

```bash
run bash -c "
    INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
    ...
"
```

`SCRIPT` is defined once at the top of the file as
`"$BATS_TEST_DIRNAME/../scripts/install.sh"`, so the suite works regardless of the
directory bats is invoked from.

`INSTALL_SH_SOURCE_ONLY=true` skips the `main "$@"` call at the bottom of
`install.sh` (guarded by `[[ "${INSTALL_SH_SOURCE_ONLY:-}" == "true" ]] || main "$@"`),
so sourcing only defines functions and global variables — no cluster, no
prompts, no helm calls.

External binaries (`helm`, `kubectl`, `docker`) are **not** needed for
flag-parsing or `prompt_or_env` tests. For tests that exercise `main()`, stub
every function it calls:

```bash
check_requirements()             { :; }
ensure_namespace()               { :; }
create_registry_secret()         { :; }
verify_existing_registry_secret(){ :; }
gather_install_params()          { :; }
run_validators()                 { echo "run_validators called"; }
helm_install()                   { echo "sentinel output"; }
```

Tests that exercise the validators individually stub `kubectl`/`helm`/`docker` as
shell functions instead, echoing whatever the check parses (a `kubeletVersion`,
a `helm version --short` string, an allocatable quantity, and so on).

## Current coverage — 112 tests

| Phase / area | Tests |
|--------------|-------|
| **Commands** — `parse_command` | `install`/`uninstall` consume the verb and keep their flags, `version`/`help` print and exit 0, a leading flag or no arguments at all still means install (the empty-array case that trips `set -u` on bash 3.2), an unknown word exits 1 instead of installing |
| **Output** — color handling | no escape sequences when stdout isn't a TTY; `NO_COLOR` honored |
| **Versioning** — `installer_version` / `--version` | reads the version from the chart beside the script, tracks it when the chart version changes, reports `unknown` when run standalone, `-v` short form |
| **Phase 1** — `--ce-version` parsing | stores value, rejects missing arg, respects env var, defaults empty |
| **Phase 1** — `--dry-run` / `--non-interactive` | each sets its var, each defaults false, flag consumed cleanly |
| **Phase 1** — `prompt_or_env` non-interactive | returns default, exits 1 with no default, env var wins over default |
| **Phase 1** — CI auto-detect | `CI=true` sets `NON_INTERACTIVE`; unset CI leaves it false |
| **Phase 2** — `--chart-path` / `resolve_chart_source` | flag parsing (missing arg, flag-looking value), missing dir, missing `Chart.yaml`, `CHART_REF` in both path and published-repo mode, `--ce-version` ignored in path mode |
| **Phase 3** — `--config` / `load_config` | flag parsing, no-op when unset, missing file, missing `yq`, registry field parsing, config value as interactive prompt default, password key warned+ignored, `chartPath` required when `kind: path`, all missing required fields listed together in one pass, `--skip-secret`/`--local-registry` relaxations, never overriding flag/env-set values |
| **Phase 3** — `-f` + `--config` composition | secret still created when both are passed, `-f` alone still skips it (back-compat), registry `--set`s present with both and absent in pure `-f`-only mode |
| **Phase 3** — versions / components / otel | `installer.versions.*` → image-tag `--set`s, `components.*` → `DISABLE_*` (never re-enabling one set by flag), `otel.*` → the 4 `ENABLE_OTEL_*` opt-ins, `--enable-otel [off\|collector\|full]` modes + invalid mode |
| **Phase 3** — registry secret / password | `REGISTRY_PASSWORD_FILE` read, env password wins over it, missing file exits 1, file satisfies the non-interactive password requirement, `verify_existing_registry_secret` present/absent |
| **Phase 3** — `KUBE_CONTEXT` | wrapper functions inject `--context`/`--kube-context`; `resolve_external_host` skips the docker-desktop/minikube heuristics when set, keeps them when unset, falls back to `localhost` when nothing matches |
| **Phase 4** — validators | `--skip-validators` flag and `main()` honoring it; Helm version and StorageClass blocking failures and passes; k8s version reported but never blocking; ingress-controller, registry-auth, NodePort and node-capacity warnings; bare-byte ephemeral-storage parsing; `MIN_HELM_VERSION` raising the floor, `MIN_K8S_VERSION` warning without blocking, empty default accepted and a malformed value rejected at load time; `run_validators` aggregating multiple blocking failures into one `exit 1` |

## Adding a new test

1. Open `tests/install_tests.bats`.
2. Add a `@test` block after the relevant section comment.
3. Follow the sourcing pattern above — `INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'`
   inside a `run bash -c "..."` block.
4. Assert with standard bats: `[ "$status" -eq 0 ]`, `[ "$output" = "..." ]`,
   `[[ "$output" == *"substring"* ]]`.
5. Run `bats tests/install_tests.bats` to confirm green.

> **Green on macOS is not green on CI.** Under macOS's system bash (3.2), a
> failed assertion that isn't the *last* statement of a `@test` is silently
> swallowed and the test still prints `ok`; CI runs bash 5, where it fails.
> After writing a test that stubs external commands, run its inner `bash -c`
> body standalone once and eyeball the output, or `brew install bash` so local
> runs behave like CI. Note that stubs of `command` must account for the
> `kubectl`/`helm` wrappers injecting `--context`/`--kube-context` before the
> real arguments.

### Minimal test template

```bash
@test "description of what is being tested" {
    run bash -c "
        INSTALL_SH_SOURCE_ONLY=true source '$SCRIPT'
        # exercise the function or flag
        parse_args --your-flag
        echo \"\$YOUR_VAR\"
    "
    [ "$status" -eq 0 ]
    [ "$output" = "expected" ]
}
```

## Key scripts/install.sh pointers

| Symbol | Location | Notes |
|--------|----------|-------|
| Global vars | lines 37-68 | All flags and env vars initialised here |
| `kubectl()` / `helm()` wrappers | lines 82-83 | Inject `KUBE_CONTEXT` into every call |
| `prompt_or_env()` | ~line 377 | Handles interactive/non-interactive/env-var precedence |
| `load_config()` | ~line 411 | Reads the `installer:` block of a `ce-config.yaml` |
| `resolve_external_host()` | ~line 602 | `EXTERNAL_HOST_ADDRESS` autodetect fallback chain |
| `resolve_chart_source()` | ~line 778 | Published-repo vs `--chart-path` mode |
| `helm_install()` | ~line 803 | Builds `extra_set_flags` and runs helm |
| `parse_args()` | ~line 934 | Flag → variable mapping; add new flags here |
| `run_validators()` | ~line 1300 | Pre-install check dispatcher (blocking checks `return 1`) |
| `main()` | ~line 1319 | Orchestration; CI auto-detect lives here |
| Source guard | last line | `[[ "${INSTALL_SH_SOURCE_ONLY:-}" == "true" ]] \|\| main "$@"` |

These line numbers drift with every change to `install.sh` — prefer grepping for
the function name over trusting them.