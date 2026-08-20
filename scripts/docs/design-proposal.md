# Proposal: Config-driven, validated MLRun CE installer

**Status:** Historical design record. Phases 1-5 are implemented; see `../AGENTS.md` for
what actually shipped and where it deviated from this document.
**Note:** written while the installer lived in its own repo, separate from the chart. Since
then it has moved into the chart repo as `scripts/install.sh`, so "this repo" below means
the installer, and the chart — described here as a separate clone — is now `charts/mlrun-ce`.
**Scope:** `scripts/install.sh` — stays pure bash + helm, single file, **local execution only**.

---

## 1. Context & motivation

`install.sh` is a single bash wrapper around `helm install mlrun-ce/mlrun-ce` (from the
published repo `https://mlrun.github.io/ce`). It works well, but has gaps we keep hitting:

- **Config sprawl** — ~20 env vars + 11 flags. The only file input (`-f values.yaml`)
  *bypasses* secret creation and other wiring.
- **Dead flag** — `--ce-version` is parsed but never passed to helm (`parse_args`, ~line 701).
- **Weak validation** — the only pre-flight check is "is the binary installed." Misconfig
  (wrong K8s version, no storage class, bad registry auth, NodePort clash) surfaces only
  after a multi-minute helm timeout. No validation of the config file itself.
- **Can't install a local chart checkout** — only the published, packaged chart. We want to
  install a `mlrun/ce` branch/PR that the developer has **cloned locally**, by pointing at
  its chart path.
- **No first-class CI story** for Jenkins / GitHub Actions.

**Goal:** adopt the high-value patterns from igzctl (typed config, validators, version
pinning, local chart-path install) while staying a **single, self-contained, pure bash +
helm** script that runs entirely on the local machine and still supports
`curl -sSL .../install.sh | bash`.

## 2. Design decisions (agreed)

| Decision | Choice | Rationale |
|---|---|---|
| Structure | **Single-file `install.sh`** | Preserves the `curl \| bash` one-liner; no `lib/` sourcing |
| Config format | **`yq`-parsed `ce-config.yaml`** | One declarative file; `yq` needed only when a config file is used |
| Chart source | **Published repo OR local path** | **No git fetching in the installer** — the developer clones `mlrun/ce` and passes `--chart-path` |
| Execution | **Local only — no SSH** | Unlike mlefi, the installer never remotes into a host; it runs locally against the current kubeconfig |
| Registry input | **CLI value, else YAML fallback** | A flag/env value wins; pressing Enter (or leaving it unset) falls back to the config YAML |
| Config safety | **Validate file exists + no empty required fields** | Fail fast with a clear message instead of a bad helm run |
| UI ingress | **Opt-in; react to the chart** | Chart ships the UI Ingress (`mlrun.ui.ingress.enabled: false`) but no controller — installer only flips that value, never installs a controller (see Phase 5) |
| Build order | **Quick wins first** | Safe, isolated, immediate value |

**Precedence everywhere:** flag/CLI > env var > config file > built-in default. Existing
flags/env keep working unchanged (back-compat).

---

## 3. Phased plan

### Phase 1 — Quick wins (no behavior change to existing flows)
1. **Fix `--ce-version`** — pass `--version "${CE_VERSION}"` to helm in published-repo mode.
2. **`--dry-run`** — inject `--dry-run=server`, skip the progress UI and the post-install
   notes table (no release exists yet).
3. **`--non-interactive` + CI auto-detect** — set automatically when `CI=true`; in
   `prompt_or_env`, return the default instead of calling `read`, so missing required values
   fail cleanly with exit 1 instead of hanging on stdin.
4. Update `usage()`.

### Phase 2 — Install from a locally cloned chart (`--chart-path`)
No git operations in the installer. The developer clones `mlrun/ce` themselves (any
branch/PR), then points the installer at the chart directory.

- New flag/env: **`--chart-path DIR`** / `CHART_PATH` (e.g. `./charts/mlrun-ce`).
- New `resolve_chart_source()` (called at the top of `helm_install`):
  - **Published-repo mode** (default, no `--chart-path`): `helm repo add/update`;
    `CHART_REF="mlrun-ce/mlrun-ce"`; optional `--version` from `--ce-version`.
  - **Local-path mode** (`--chart-path` set): verify the dir exists and contains
    `Chart.yaml` (**error otherwise**); `helm dependency update "${CHART_PATH}"`;
    `CHART_REF="${CHART_PATH}"`.
- In `helm_install`, the chart reference becomes `${CHART_REF}` (replaces the 4 hardcoded
  `mlrun-ce/mlrun-ce` occurrences).

### Phase 3 — `ce-config.yaml` (yq) + validation — done

Implemented as designed below, with two deviations from the original draft:

1. **Partial reduction in scope, later revisited:** `ingress.*`/`components.*` from the
   example YAML were initially **not** wired up — deferred to avoid half-finishing
   something that overlaps Phase 5's ingress work. `components.*` was added back in a
   follow-up pass (`installer.components.{monitoring,spark,mpi,modelMonitoring}`,
   mirroring the existing `--disable-*` flags' exact `--set` effects — pure value
   resolution, no new infra logic) after explicit scoping: `ingress.*` and anything that
   triggers real infra beyond a `--set` (installing nginx-ingress, deploying a local
   registry) stays deferred to Phase 5; only the pure `--set` toggles were in scope here.
2. **`-f`/`--config` composition — reversed, then reverted back:** the original design
   said `--config` knobs become `--set` flags that layer on top of `-f`'s `--values`
   (helm applies `--set` after `--values`, so `--config` would always win, "no extra
   merge logic needed"). The first implementation pass didn't actually do this — `-f`
   silently made every `--config` registry/host field dead code with no warning. That
   was "fixed" at the time by making **`--config` and `-f`/`--values` mutually
   exclusive** (`main()` exit 1 if both given) rather than building the layering — a
   misread of the actual ask, corrected in a later follow-up back to the originally
   designed composable behavior described in "Combining with `-f`/`--values`" below.
   `main()` now runs `create_registry_secret`/`gather_install_params` whenever
   `--config` is given (even alongside `-f`), and the 3 registry `--set`s
   (`global.registry.{url,secretName}`, `global.externalHostAddress`) moved into the
   same `extra_set_flags` array the other `--set`s already used — gated off only in
   pure `-f`-only mode (no `--config`), which keeps `-f`-alone's exact prior
   self-contained behavior (no secret creation, no registry `--set`s) unchanged. Added
   `installer.versions.{mlrun,nuclio}` (→
   `--set mlrun.{api,ui}.image.tag`/`nuclio.{controller,dashboard}.image.tag`) to close
   the version-pinning gap this raised — usable via `--config` or the `MLRUN_VERSION`/
   `NUCLIO_VERSION` env vars (the latter also works with `-f`, since it's not gated by
   `--config`).
3. **New safety check found along the way:** `--skip-secret` previously trusted the user
   completely — if the named secret didn't actually exist, the failure only surfaced
   after a `helm --wait` timeout with a cryptic pod-level error. Added
   `verify_existing_registry_secret()`: exits 1 immediately with a clear message if the
   secret is missing.
4. **`REGISTRY_PASSWORD_FILE` added:** the password was (and still is) deliberately
   never read from `ce-config.yaml` — only `REGISTRY_PASSWORD` (env) or the interactive
   masked (`read -s`) prompt could supply it. Added `REGISTRY_PASSWORD_FILE` as a third
   option — path to a local file containing just the password, trailing newline
   stripped, never echoed — for the common case where a CI runner or secrets manager
   mounts a secret as a file rather than an env var. Precedence: `REGISTRY_PASSWORD`
   env > `REGISTRY_PASSWORD_FILE` > interactive prompt; still never settable via
   `ce-config.yaml`.
One YAML file, **only** the reserved **`installer:`** block — a curated set of basic,
typed knobs (registry, chart source, ingress toggle, component enables). No generic
passthrough section: `ce-config.yaml` is not a values-file substitute, and never becomes
a `-f` values file itself. New **`--config FILE`** flag. **`-f`/`--values` stays fully
independent** and can be combined with `--config` — arbitrary/complex helm values always
go through `-f`, never through the config file. (Design note: an earlier draft of this
phase let `ce-config.yaml` pass "everything else" straight through to helm as a second
values file — dropped, since that duplicated `-f`'s job and risked the two silently
overwriting each other.)

```yaml
installer:                       # consumed by install.sh; stripped before helm
  kubeContext: ""                # optional; blank = current local context (no SSH, no remote exec)
  externalHostAddress: auto       # auto-detect locally, or pin a value
  registry:
    url: index.docker.io/myuser   # used if not given on the CLI (Enter falls back to this)
    secret:
      name: registry-credentials
      create: true                # user brings creds; PASSWORD via env/prompt only, never in file
      server: https://index.docker.io/v1/
      username: myuser
  chartSource:
    kind: repo                    # repo | path
    chartVersion: 0.11.0          # repo mode -> --version
    chartPath: ""                 # path mode -> local cloned chart dir
  ingress:
    ui: false                     # opt-in -> --set mlrun.ui.ingress.enabled=true (chart's own ingress)
    className: ""                 # ingress class (blank = chart/cluster default)
    host: ""                      # optional host override; else chart uses global.externalHostAddress
  components: { monitoring: false, spark: true }   # -> --set <chart>.enabled=...
```

- **`load_config()`**: 
  - **Error if `--config FILE` does not exist.**
  - Require `yq`; read `installer.*` into vars **only if not already set** by flag/env
    (precedence: flag > env > `ce-config.yaml` > default — same chain as every other
    setting; `ce-config.yaml` just slots in as the config layer). For registry values
    this is the "CLI wins, else YAML" behavior; in interactive mode the YAML value
    becomes the prompt default (Enter accepts it).
  - **Validate required fields are non-empty** (e.g. `registry.url`, `registry.secret.username`,
    and `chartPath` when `chartSource.kind: path`); **raise an error listing every empty
    required field** and exit 1.
  - Every `installer.*` key resolves to an explicit `--set` flag in `helm_install`, the same
    pattern `extra_set_flags` already uses for `DISABLE_SPARK`/`ENABLE_INGRESS`/etc. There is
    nothing else in the file to strip or pass through.
- **Host / context:** `externalHostAddress: auto` reuses the existing local autodetect
  (minikube / docker-desktop / node IP). `kubeContext` (if set) is passed as
  `--kube-context` to kubectl/helm — still local execution, just selects a context.
- **Combining with `-f`/`--values`:** the two are orthogonal, not ranked against each other.
  `--config` knobs become `--set` flags; `-f` is a raw values file. Helm applies `--set`
  after `--values`, so a `--config` knob always wins over the same key in a `-f` file with
  no extra merge logic needed. Implemented: `helm_install` uses a single code path with an
  optional `values_flag` (`--values`, only when `-f` is given) alongside `extra_set_flags`
  (which now includes the registry `--set`s too, gated off only in pure `-f`-only mode) —
  so `--config` and `-f` can be passed together, matching how `extra_set_flags` already
  layered on top of `-f` for versions/components/otel/ingress even before this change.

### Phase 4 — Pre-install validators (fail fast) — done

Implemented as designed, with one addition and one scope note:

- **Helm CLI version check added:** the original draft only listed the K8s version floor
  as blocking. Since §6 also resolved a Helm CLI floor (>= 4.1) for the same docs page,
  `validate_helm_version()` was added as a third blocking check alongside K8s version and
  StorageClass — same shape, same rationale (docs-level requirement, not chart-enforced).
- **Node capacity scoped to RAM + storage, no CPU floor:** the bullet above says
  "CPU/mem above a documented floor," but the only actual floor resolved in §6 is "≥8Gi
  RAM / 8Gi storage" — no CPU number exists in the docs to check against. Implemented
  `validate_node_capacity()` against the RAM/storage floor only.
- **Aggregate-then-report, not fail-at-first-check:** the three blocking checks
  (`validate_k8s_version`, `validate_helm_version`, `validate_storage_class`) `return 1`
  instead of calling `exit 1` directly, so `run_validators()` runs every check (blocking
  and warning) and reports all problems in one pass before exiting 1 — a deliberate,
  one-off deviation from the rest of the script's inline `log_error; exit 1` style, chosen
  so a user fixing pre-flight issues doesn't have to re-run the installer once per problem.
- **Bug found via live testing against `docker-desktop`:** `validate_node_capacity()`'s
  quantity parser only handled `Ki`-suffixed values (memory's format); this cluster's real
  `.status.allocatable.ephemeral-storage` is a bare byte integer with no suffix, so the
  check silently read it as 0Gi. Fixed with a small `_allocatable_to_ki()` helper that
  handles `Ki`/`Mi`/`Gi`/`Ti` suffixes and the bare-byte-integer form.
- **Supporting fix in `create_registry_secret`:** its `username`/`password`/`server` were
  locals invisible outside the function once resolved interactively (only
  `REGISTRY_USERNAME_VALUE` existed as a side-channel export). Added the same-shaped
  `REGISTRY_PASSWORD_VALUE`/`REGISTRY_SERVER_VALUE` so `validate_registry_auth` can
  actually see what was entered, regardless of source (env, `REGISTRY_PASSWORD_FILE`, or
  prompt).

Checks, as implemented:

> **Superseded:** the two version floors below were later realigned to the chart's own
> prerequisites — Helm >= 3.6 blocking, and no Kubernetes floor at all (the check is
> informational). See `scripts/AGENTS.md` "Version floors realigned" and
> `docs/configuration.md` "Version floors". The rest of this section still holds.

- **K8s version** ≥ 1.34 (blocking)
- **Helm CLI version** ≥ 4.1 (blocking)
- **Default StorageClass** exists (blocking)
- **Registry auth** — best-effort `docker login` with provided creds (warning; skipped
  under `--local-registry` or when no credentials were resolved yet, e.g. `-f`-only mode)
- **NodePort conflicts** — chart's fixed NodePorts (30010/20/40/50/60/70, 30093/94, 30100, 30110)
  not already bound by a Service outside the target namespace (warning)
- **Node capacity** — allocatable RAM/ephemeral-storage above 8Gi/8Gi (warning)

New flag/env: `--skip-validators` / `SKIP_VALIDATORS`, mirroring `--skip-secret`'s exact
pattern. `run_validators()` is called from `main()` unconditionally (in every mode,
including pure `-f`-only) right before `helm_install`.

### Phase 5 — dropped (folded into `--enable-ingress` directly)

The original idea was a UI-ingress toggle plus a controller story. Revisiting after Phase
4 shipped: `--enable-ingress` already flips `mlrun.ui.ingress.enabled` (and
`jupyterNotebook`/`nuclio.dashboard`/`mlrun.api` Ingress alongside it) — there was no
separate chart-value toggle left to add. The only real gap was that `--enable-ingress`
used to `helm install` the actual `ingress-nginx` controller itself
(`install_ingress_controller()`) — installing a third-party controller is more than this
installer should be doing on a user's behalf. Fixed directly on the existing flag instead
of as a new phase:

- **Removed** `install_ingress_controller()` and its call in `main()`. `--enable-ingress`
  is now BYO-controller-only — it configures the chart's Ingress resources via `--set`,
  nothing else.
- **Added** `validate_ingress_controller()` to Phase 4's `run_validators` (warning-only,
  no-op when `--enable-ingress` isn't used): checks `kubectl get ingressclass
  <INGRESS_CLASS>` and warns — doesn't block, doesn't install — if it's missing, so the
  Ingress resources not resolving is a known-cause warning instead of a silent mystery.
- **Not built:** `print_ui_ingress_url()` (reading the created Ingress back and printing
  its URL in the final access table) and `installer.ingress.*` ce-config.yaml keys (the
  CLI/env flag already covers the same knobs). Out of scope unless requested separately.

### Phase 6 — CI samples (Jenkins + GitHub Actions)
CI does its own `git checkout` (via `actions/checkout` / Jenkins SCM) — the installer never
fetches. To test a `ce` PR, CI checks out that ref and passes `--chart-path`.

- **`.github/workflows/ce-install.yml`** — `workflow_dispatch` with inputs `ce_ref`
  (branch/PR ref to checkout) and `dry_run`; steps: checkout this repo → checkout `mlrun/ce`
  at `ce_ref` → `azure/setup-helm` → install `yq` → `helm/kind-action` → run `install.sh`
  with `CI=true`, `REGISTRY_*` from `secrets`, and `--chart-path` to the checked-out chart.
  A separate `pull_request` job runs `bash -n` + `shellcheck` only.
- **`Jenkinsfile`** — parameters (CE_REF, DRY_RUN), `withCredentials` for the registry,
  `post { failure { sh './install.sh --uninstall || true' } }` teardown.

---

## 4. Files touched / added

| File | Change |
|---|---|
| `install.sh` | Phases 1-4: new vars, `resolve_chart_source`, `load_config` (+existence/empty-field validation), `run_validators`; edits to `parse_args`, `prompt_or_env`, `helm_install`, `main`, `usage` |
| `ce-config.yaml.example` | New — documented sample config |
| `.github/workflows/ce-install.yml` | New — GitHub Actions |
| `Jenkinsfile` | New — Jenkins pipeline |
| `README.md` | Document `--config`, `--chart-path`, validators, CI usage |

## 5. Verification

1. **Static:** `bash -n install.sh` + `shellcheck install.sh`.
2. **Help:** `./install.sh --help` lists all new flags.
3. **Config validation:**
   - `./install.sh --config missing.yaml` → clear "file not found" error, exit 1.
   - Config with an empty required field → error naming the empty field(s), exit 1.
4. **Dry-run (needs kind/minikube/docker-desktop):**
   - `./install.sh --dry-run` — renders published chart, no deploy.
   - `./install.sh --ce-version 0.11.0 --dry-run` — confirms `--version` is passed.
   - `./scripts/install.sh --chart-path ./charts/mlrun-ce --dry-run` — `helm dependency update` +
     renders from the local chart directory.
   - `./install.sh --config ce-config.yaml.example --dry-run` — `installer:` block consumed
     and stripped; registry falls back to YAML when not passed on the CLI.
5. **Ingress (opt-in, BYO controller):** `./install.sh --enable-ingress` on a cluster with
   an existing IngressClass → no warning, `kubectl get ingress` shows `mlrun-ui` etc. On a
   cluster with no matching IngressClass → `validate_ingress_controller` warns but the
   install still proceeds (no controller ever gets installed by this script).
6. **Non-interactive:** `CI=true ./install.sh` with a required var missing → clean exit 1 (no hang).
7. **CI:** trigger the GH Actions `workflow_dispatch` with a `ce_ref`; run the parameterized Jenkins build.

## 6. Open questions for reviewers — resolved

- **Is `yq` an acceptable dependency for the config path?** Yes, with conditions. `yq`
  (mikefarah/yq, Go) makes no network calls at runtime, so runtime CVEs are limited to
  YAML-parsing bugs — but the *installer* (or a CI image) fetching the `yq` binary is a
  supply-chain vector like any curl-installed tool. Mitigate: pin an exact released
  version, verify its checksum (GitHub releases publish `checksums`), and prefer an
  already-present `yq` (package manager / CI base image) over installing one at runtime.
  `load_config()` should hard-require `yq` only when `--config` is passed — no new
  dependency for users who don't use the config file (matches existing "quick wins first,
  no behavior change" principle).
- **Minimum supported K8s version to enforce in the validator (Phase 4)?** Per the
  official install docs (docs.mlrun.org, `install-mlrun-ce/kubernetes-install.md`):
  **Kubernetes >= 1.34**, **Helm >= 4.1** CLI, a default StorageClass, and >= 8Gi RAM /
  8Gi storage available. The chart itself sets no `kubeVersion` in `Chart.yaml` — the
  version floor is a docs-level requirement, not chart-enforced, so `run_validators()`
  is the only place it's actually checked today.
- **Which fields count as required (non-empty) in `ce-config.yaml`?** Cross-checked
  against what `install.sh` already hard-enforces (`create_registry_secret`,
  `gather_install_params` in the current `main`, ~lines 369-467):
  - `installer.registry.url` — required, unless `installer.local` registry mode is used
    (mirrors `REGISTRY_URL` today, install.sh:461-464).
  - `installer.registry.secret.username` — required (mirrors `REGISTRY_USERNAME`,
    install.sh:395-398).
  - `installer.registry.secret.password` — required, but **never read from the file** —
    only from `REGISTRY_PASSWORD` env or the interactive prompt (already a design
    decision in §3; the password is checked non-empty the same way the username is).
  - `installer.chartSource.chartPath` — required only when `installer.chartSource.kind:
    path`.
  - Everything else in the schema (`registry.secret.server` — has a working default;
    `registry.secret.email`; `chartVersion`; `kubeContext`; `externalHostAddress`;
    `ingress.*`; `components.*`) is optional, matching that these either have defaults
    or are opt-in features today.
- **Any additional NodePorts/components to validate beyond the chart defaults listed in
  Phase 4?** No — no additional ports/components identified; the list in Phase 4 stands
  as-is.