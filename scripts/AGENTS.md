## What this directory is

`scripts/install.sh` — single-file bash wrapper around `helm install mlrun-ce/mlrun-ce`
(published chart repo: `https://mlrun.github.io/ce`). Local execution only, no SSH, no git
fetching. Full design/rationale: `docs/design-proposal.md`.

It lives in the same repo as the chart it installs (`charts/mlrun-ce`), but installs the
**published** chart by default — the in-repo chart is used only when the caller passes
`--chart-path ./charts/mlrun-ce` explicitly. That's deliberate: the script is also served
via `curl | bash`, where no repo exists around it, so the same invocation has to mean the
same thing in both places.

For chart-side conventions (values.yaml layout, `requirements.lock`, adding components) see
the repo-root `AGENTS.md`/`CONTRIBUTING.md`. This file covers the installer only.

## Install flow (`main()`, install.sh ~1319-1378)

1. `parse_args` — flags/env, precedence flag > env > default
2. `check_requirements` — helm, kubectl, docker present and reachable
3. `ensure_namespace` — creates `NAMESPACE`; in `--dry-run` only logs what it would do (no cluster mutation)
4. `resolve_external_host` — only if `LOCAL_REGISTRY` or `ENABLE_INGRESS`
5. `deploy_local_registry` — only if `--local-registry`
6. `create_registry_secret` — skipped via `--skip-secret`, or when `-f VALUES_FILE` is given **and** `--config` is absent (pure `-f`-only mode keeps its old self-contained behavior; `-f` + `--config` together still creates the secret). In `--dry-run` only logs, doesn't touch the cluster
7. `gather_install_params` — resolves `REGISTRY_URL` (same `-f`-only skip condition as above)
8. `run_validators` — skippable via `--skip-validators`; includes `validate_ingress_controller` (warns if `--enable-ingress`'s IngressClass isn't found — see Phase 4/5 notes below)
9. `helm_install` → `resolve_chart_source` (published repo vs `--chart-path`) → `helm install/upgrade`, with `--values`/`--set` composed per the precedence rule below

`install_ingress_controller` (which used to `helm install` the `ingress-nginx` chart when
`--enable-ingress` was passed) was **removed** — see "Phase 5" below.

## Phase status (see docs/design-proposal.md for full plan)

- **Phase 1 (done):** `--ce-version` fix, `--dry-run`, `--non-interactive` + `CI=true`
  auto-detect. Originally shipped as `--helm-version`/`HELM_VERSION`; renamed (hard
  rename, no alias) once it became clear the flag pins the **mlrun-ce chart** version,
  not the Helm CLI binary — `check_requirements`/`validate_helm_version` (Phase 4) are
  the ones actually about the Helm binary, and shared the word "helm" confusingly with
  this one.
- **Phase 2 (done):** `--chart-path DIR` / `resolve_chart_source` — install from a locally cloned chart dir instead of the published repo
- **Phase 3 (done):** `--config`/`CONFIG_FILE` + `load_config()` — reads the `installer:`
  block of a `ce-config.yaml` (requires `yq`, only when `--config` is used) and fills in
  registry url/username/server/email, `externalHostAddress`, `chartSource.{kind,chartVersion,chartPath}`
  and the new `KUBE_CONTEXT` as defaults, at flag/env > config-file > default precedence.
  `KUBE_CONTEXT` is threaded through every `kubectl`/`helm` call via wrapper functions
  (install.sh top-of-file) rather than editing each call site — `check_requirements` uses
  `type -P` instead of `command -v` for helm/kubectl so those wrappers don't cause a false
  "installed" detection. `installer.ingress.*` from the original design draft was **not**
  implemented (deferred; use existing `--enable-ingress` instead) — at the time this
  triggered real infra (nginx-ingress install) beyond a `--set`; `--enable-ingress` no
  longer installs anything (see "Phase 5" below), but `installer.ingress.*` config-file
  keys remain unimplemented since `--enable-ingress`/`--enable-ingress CLASS` already
  cover the same knobs via flag/env.
  `installer.components.{monitoring,spark,mpi,modelMonitoring}` **was** added in a
  follow-up pass — mirrors the `--disable-*` flags' exact `--set` effects 1:1 (pure value
  resolution, `false` disables, flag/env always wins and is never re-enabled by the
  file); `--local-registry`'s infra-deploying behavior was explicitly scoped out the same
  way as `ingress.*`. OpenTelemetry support was added in a later follow-up pass as its
  own `installer.otel.{operator,collector,namespaceLabel,instrumentation}` block (not
  under `components.*`, since it's 4 independent knobs, not one flat bool) — the reverse
  direction from `components.*`: those 4 chart values (`opentelemetry-operator.enabled`,
  `opentelemetry.collector.enabled`, `opentelemetry.namespaceLabel.enabled`,
  `opentelemetry.instrumentation.enabled`) all ship `false` by default (unlike
  `kube-prometheus-stack`/`spark-operator`/etc., which ship `true`), so each key opts IN
  via the matching `--enable-otel-{operator,collector,namespace-label,instrumentation}`/
  `ENABLE_OTEL_*` flag/env pair (a bare `--enable-otel` is a convenience that sets all
  four). Kept as 4 independent toggles rather than one bundled flag deliberately —
  `namespaceLabel` auto-instruments every pod in the release namespace once
  `instrumentation` is also on, a materially bigger blast radius than `operator`/
  `collector` alone, so a user should be able to pick just the latter two. A flag/env-set
  `ENABLE_OTEL_*=true` still always wins and is never unset by the file, same "add,
  never override" rule as `components.*`. Later given an optional MODE argument —
  `--enable-otel [off|collector|full]` (bare = `full`, matching the original behavior
  exactly) — as a convenience on top of, not a replacement for, the 4 granular flags
  (which still work individually and combine with it). `collector` exists as a named
  middle ground: operator+collector only (a metrics pipeline), without opting into
  `namespaceLabel`'s bigger blast radius.
  **`--config` and `-f`/`--values` compose** (a later follow-up reverted an earlier
  deviation): the original design draft had `--config` knobs layer as `--set` overrides
  on top of `-f`'s `--values` (helm applies `--set` after `--values`, so `--config`
  always wins, no merge logic needed) — the first implementation pass never actually
  wired that up (silently dead instead) and was "fixed" at the time by making the two
  mutually exclusive (`main()` exit 1). That exclusivity was itself the wrong fix and was
  removed: `main()` now runs `create_registry_secret`/`gather_install_params` whenever
  `--config` is given (even alongside `-f`) rather than only when `-f` is absent, and
  `helm_install` moved the 3 registry `--set`s (`global.registry.{url,secretName}`,
  `global.externalHostAddress`) into the same `extra_set_flags` array the other
  `--set`s already used, gated off only in pure `-f`-only mode (no `--config`) — so
  every config-resolved field now actually reaches helm as a `--set` layered on top of
  `--values` in every combination. `-f` used alone (no `--config`) keeps its exact prior
  self-contained behavior unchanged (no secret creation, values file must reference an
  existing secret) — only adding `--config` changes that. Precedence documented in
  README as: flag > env > `ce-config.yaml` (→ `--set`) > `-f`/`--values` (raw) > chart
  defaults. Added `installer.versions.{mlrun,nuclio}` / `MLRUN_VERSION`/`NUCLIO_VERSION`
  env vars (→ `--set mlrun.{api,ui}.image.tag`, `nuclio.{controller,dashboard}.image.tag`)
  for per-service version pins, independent of `--ce-version`/`chartSource.chartVersion`
  which pins the mlrun-ce umbrella chart as a whole. Also added
  `verify_existing_registry_secret()`: `--skip-secret` now exits 1 immediately if the
  named secret doesn't exist in the namespace, instead of failing later via an opaque
  `helm --wait` timeout. `REGISTRY_PASSWORD_FILE` was added as a third way to supply the
  registry password (path to a local file, trailing newline stripped, never echoed) —
  same "never settable via `ce-config.yaml`" rule as `REGISTRY_PASSWORD`; precedence is
  `REGISTRY_PASSWORD` env > `REGISTRY_PASSWORD_FILE` > interactive masked prompt.
- **Phase 4 (done):** `run_validators()` — a pre-install dispatcher, called from `main()`
  right after the `-f`/`--config` secret-creation branch and right before `helm_install`,
  skippable via `--skip-validators`/`SKIP_VALIDATORS` (mirrors `--skip-secret`'s exact
  flag/env/`main()` pattern). Runs unconditionally in every mode, including pure
  `-f`-only (the cluster-level checks don't depend on registry resolution; the
  registry-auth check self-skips when nothing's resolved yet). Six checks, all read-only
  (no `--dry-run` gating needed):
  - **Blocking** (`validate_helm_version`, `validate_storage_class`): Helm CLI >= 3.6
    (parsed from `helm version --short`) and a default StorageClass exists
    (`is-default-class` annotation). These `return 1` instead of calling `exit 1` directly
    (the one deliberate deviation from the rest of the script's inline
    `log_error; exit 1` style) so `run_validators` can run every check and report *all*
    failures in one pass, then exit 1 once at the end — rather than stopping at the first
    problem found.
  - **Warning-only** (`validate_k8s_version`, `validate_registry_auth`,
    `validate_nodeport_conflicts`, `validate_node_capacity`): the cluster's Kubernetes
    version, read via `kubectl get nodes` `.status.nodeInfo.kubeletVersion` (the same
    jsonpath-on-nodes style `resolve_external_host` already uses — avoids depending on
    `kubectl version` supporting `-o jsonpath`), reported always and compared only against
    an explicitly set `MIN_K8S_VERSION`; best-effort `docker login` with the resolved registry
    creds (skipped, not warned, under `--local-registry` or when nothing's resolved yet);
    the chart's fixed NodePorts (`30010/20/40/50/60/70`, `30093/94`, `30100`, `30110`)
    already bound by a Service outside the target namespace (excluding the target
    namespace so `helm upgrade` of the same release never self-flags); total cluster
    allocatable RAM/ephemeral-storage under the documented 8Gi/8Gi floor (no CPU floor
    exists in the docs to check against, despite the original proposal bullet loosely
    saying "CPU/mem").
  - **Supporting fix needed along the way:** `create_registry_secret`'s
    `username`/`password`/`server` are locals — `prompt_or_env` returns a value via
    stdout, it never sets the named env var globally, so in the (common) interactive-prompt
    case the real entered password/server were invisible outside the function. Only
    `REGISTRY_USERNAME_VALUE` existed as a side-channel export (for `gather_install_params`'s
    suggested-URL default). Added the same-shaped `REGISTRY_PASSWORD_VALUE` and
    `REGISTRY_SERVER_VALUE` so `validate_registry_auth` can see what was actually entered,
    regardless of whether it came from env, `REGISTRY_PASSWORD_FILE`, or an interactive
    prompt.
- **Phase 5 — dropped, folded into existing `--enable-ingress` instead of a new phase:**
  the chart's UI Ingress toggle (`mlrun.ui.ingress.enabled`) already ships as part of the
  pre-existing `--enable-ingress` flag (alongside `jupyterNotebook`/`nuclio.dashboard`/
  `mlrun.api` Ingress — install.sh `helm_install`'s `extra_set_flags`), so there was no
  separate toggle left to add. The one real gap — `--enable-ingress` used to `helm
  install` the actual `ingress-nginx` controller itself, a real infra dependency the
  installer had no business installing — was fixed directly on that flag: removed
  `install_ingress_controller()` entirely, and added `validate_ingress_controller()` as a
  warning-only check in `run_validators` (Phase 4) that looks for an existing IngressClass
  matching `--enable-ingress`'s class and warns (doesn't block, doesn't install) if none
  is found. `--enable-ingress` is now BYO-controller-only. `print_ui_ingress_url()` (the
  other original Phase 5 idea — reading the created Ingress back and printing its URL in
  the final access table) was not built; out of scope unless asked for separately.
- **Phase 6 (done, as real CI rather than the originally planned samples):**
  `.github/workflows/installer-ci.yaml` runs `make installer-lint` (`bash -n` +
  shellcheck) and `make installer-test` (the bats suite) via the Makefile targets rather
  than duplicating the commands, so CI and local can't drift. It runs on **every** PR: an
  earlier `paths: scripts/**` filter was dropped because the job takes about a minute and
  a filtered job lets the suite rot unnoticed between installer changes. Note the unit
  tests are hermetic (they stub `kubectl`/`helm`/`docker`), so running them on chart PRs
  does *not* catch chart/installer drift — only the kind job would, and that's
  `workflow_dispatch`-only, installing with `--chart-path ./charts/mlrun-ce
  --local-registry`, for the same reason `ci.yaml`'s `test:` job is commented out (pulling
  the full image set is too slow per-PR). It's a separate workflow file rather than a job
  in `ci.yaml` so it reports as an independent status check. No Jenkinsfile — this repo is
  GitHub Actions only.
## Phase 3 pre-work (resolved, see docs/design-proposal.md §6)

Before implementing Phase 3 (`ce-config.yaml` + `yq`), these open questions were
resolved by checking the official MLRun docs and the chart itself (`../charts/mlrun-ce`,
then still a separate repo):

- **`yq` dependency:** acceptable. No network calls at runtime, so runtime CVE exposure
  is limited to YAML parsing. Require it only when `--config` is passed (no new dep for
  existing flows); pin an exact release version and verify its checksum rather than an
  unpinned install.
- **K8s/Helm version floor** (for Phase 4's validator): originally taken from the official
  install docs (**Kubernetes >= 1.34**, **Helm >= 4.1**) and enforced as blocking. **This was
  later reversed** — see "Version floors realigned" below. Neither is enforced by the chart
  itself (no `kubeVersion` in `Chart.yaml`).
- **Mandatory `ce-config.yaml` fields** (cross-checked against what `install.sh` already
  hard-enforces in `create_registry_secret`/`gather_install_params`):
  `installer.registry.url` (unless `--local-registry`), `installer.registry.secret.username`,
  `installer.registry.secret.password` (required but **never read from the file** — env/
  prompt only), `installer.chartSource.chartPath` (only when `chartSource.kind: path`).
  Everything else (`registry.secret.server`, `registry.secret.email`, `chartVersion`,
  `kubeContext`, `externalHostAddress`, `ingress.*`, `components.*`) is optional —
  documented in full in `docs/configuration.md`'s "Config file (`ce-config.yaml`)" section.
- **NodePorts/components for Phase 4:** no additions beyond the list already in
  `docs/design-proposal.md` Phase 4.

## Version floors realigned (supersedes the Phase 3 pre-work finding above)

The blocking **K8s >= 1.34 / Helm >= 4.1** floors, sourced from docs.mlrun.org, were replaced
with the chart's own stated requirement:

- **Helm >= 3.6**, blocking — mirrors `charts/mlrun-ce/README.md`'s prerequisites, so the
  installer can't refuse a Helm the chart itself supports. Helm 4.1 as a *minimum* excluded
  every Helm 3 user for a chart that renders fine on Helm 3.
- **No Kubernetes floor.** `validate_k8s_version` is now informational: it reports the
  detected version, returns 0 on every path, and is no longer in `run_validators`'
  `|| failed=1` group. `MIN_K8S_VERSION` defaults to empty and only produces a *warning*
  when set. The chart declares no `kubeVersion` and the README states no cluster version,
  so there was no requirement to enforce — 1.34 as a hard minimum rejected nearly every
  supported managed cluster, including the local `docker-desktop` (1.30.5) used for testing.

An intermediate step (before this realignment) kept the strict floors but made them
overridable via `MIN_K8S_VERSION`/`MIN_HELM_VERSION`. Those env vars survive, but their
purpose inverted: they now exist to *tighten* rather than loosen. `MIN_HELM_VERSION` is the
only hard floor; `MIN_K8S_VERSION` never blocks.

Knock-on effect: `.github/workflows/installer-ci.yaml`'s kind job had pinned
`kubectl_version`/`node_image` to `v1.34.0` and Helm to `v4.1.1` purely to satisfy those
floors. Those pins were removed — `helm/kind-action@v1.10.0` bundles a kind release
predating K8s 1.34, so that node image likely wasn't even published for it.

## Known non-bugs

- **CE does not officially support upgrades**, so a `helm upgrade` over an existing release
  is out of scope as a supported path. The concrete symptom seen live (0.11.0 →
  0.12.0-rc.11 on the `vmdev137` lab): the Kafka broker crash-loops with
  `Invalid cluster.id in: /var/lib/kafka/data/kafka-log0/meta.properties. Expected
  ByHirbmSVDCwP7YDBt3V2A, but read <random>`. Commit 19fc711 pins
  `kafka.clusterId: "ByHirbmSVDCwP7YDBt3V2A"` in `values.yaml` so *re-installs* reuse
  retained PVC data, but a volume formatted before that pin holds a random ID that nothing
  migrates. **Fix: delete the Kafka PVC and pod** — `kubectl delete pvc
  data-kafka-stream-kafka-stream-pool-<n> -n <ns> --wait=false` then `kubectl delete pod
  kafka-stream-kafka-stream-pool-<n> -n <ns>` (deleting the pod releases the
  `pvc-protection` finalizer); Strimzi reprovisions and reformats with the pinned ID.
  Only transient model-monitoring stream data is lost. Setting `kafka.clusterId: ""`
  restores the pre-19fc711 random-ID behaviour if keeping the existing volume matters more.

- `--dry-run` uses `helm --dry-run=server`, which validates against the live API
  server. If the target cluster lacks the Prometheus Operator CRDs, the
  `kube-prometheus-stack` subchart's `PrometheusRule`/`ServiceMonitor` resources
  fail server-side validation. This is a Helm limitation (charts with CRDs
  can't fully dry-run without those CRDs present), not an `install.sh` bug.
- **Two `mlrun-ce` releases can't coexist on one cluster**, even in different
  namespaces with different release/secret names and NodePort overrides via `-f`.
  Confirmed live: the chart's `workflow-controller` `PriorityClass` is
  cluster-scoped with a hardcoded name (no values.yaml knob), so a second
  release's `helm install` fails immediately with an ownership-metadata error
  once one release already owns it. Not an `install.sh` bug — the chart itself
  has no multi-release story on a shared cluster short of patching that template.
- `helm uninstall` (and `--hard-clean`) can leave orphaned Strimzi `Kafka`/
  `KafkaNodePool`/`StrimziPodSet` custom resources and their broker pod behind:
  once the `strimzi-kafka-operator` Deployment is gone, nothing reconciles those
  CRs, so the broker pod keeps running and its PVC's `kubernetes.io/pvc-protection`
  finalizer blocks `--hard-clean`'s PVC deletion indefinitely. Fix is manual:
  delete the `strimzipodset` and pod directly (releases the finalizer), then the
  `kafka`/`kafkanodepool` CRs. Not something `do_hard_clean` can anticipate from
  `install.sh` alone — it's a chart/Strimzi ordering issue.

## Fixed bugs

- **`helm_install`'s `--wait` had no `--timeout`, so a slow image pull failed the release**
  (found via live testing against the `vmdev137` lab): both helm invocations in
  `helm_install` (the progress-UI branch and the plain branch) passed `--wait` without
  `--timeout`, silently inheriting helm's **5 minute** default. A single cold pull of
  `quay.io/mlrun/jupyter` (4.2Gi) took **5m40s** on that cluster, so helm gave up mid-pull
  with `UPGRADE FAILED: resource Deployment/mlrun/mlrun-jupyter not ready ... Pending
  termination: 1` and marked the release `failed` — even though the rollout completed
  seconds later and every pod went Running. A failed release record is worse than a slow
  one: it misreports a working install and leaves the release in a state that invites an
  unnecessary rollback. Notably `helm uninstall` (`do_uninstall`) *already* passed
  `--timeout 960s`, so this was an inconsistency rather than a deliberate choice. Fix:
  added `HELM_TIMEOUT` (default `960s`, matching uninstall) and passed
  `--timeout "${HELM_TIMEOUT}"` in both branches. Re-running with the fix took 3m12s and
  the release went `deployed`. Two regression tests assert the default and the override —
  note the override test must `export HELM_TIMEOUT` on its own line rather than using the
  `VAR=x source install.sh` prefix form, since bash discards that prefix assignment when
  `source` returns and `set -u` then trips inside `helm_install`.

- **`do_hard_clean()`'s force-delete fallback could hang indefinitely** (found via live
  testing against the `vmdev137` lab cluster — a real `--hard-clean` run sat blocked for
  18+ hours): both the PVC and PV delete loops fall back to
  `kubectl delete ... --force --grace-period=0` when the graceful `--timeout 60s` delete
  fails, but neither fallback passed `--wait=false` — by default `kubectl delete` still
  blocks waiting for the object to actually disappear from the API, `--force` only skips
  *graceful* deletion of the underlying pod, not the wait. When a PVC has a lingering
  `kubernetes.io/pvc-protection` finalizer (the exact orphaned-Strimzi-Kafka scenario in
  "Known non-bugs" below), nothing ever removes that finalizer, so the fallback hung just
  as long as the primary attempt — defeating the point of having a fallback at all,
  worse still under `run_in_background` where a silently hung command gives no signal
  anything is wrong. Fix: added `--wait=false` to both fallback commands, so they return
  immediately once the delete request is accepted, regardless of whether the object's
  removal actually completes.
- **`validate_node_capacity()` silently read ephemeral-storage as 0Gi on some clusters**
  (found via live testing against local `docker-desktop`): the parser only matched
  Ki-suffixed quantities (the form `.status.allocatable.memory` always uses), but
  `.status.allocatable.ephemeral-storage` is commonly reported as a bare byte integer
  with no unit suffix (cAdvisor-sourced, confirmed live: `56403987978` on this cluster,
  vs. memory's `7922684Ki`) — the regex silently skipped every line, so the sum stayed 0
  and the warning read "~0Gi" instead of the real ~52Gi. Fix: added `_allocatable_to_ki()`,
  a small quantity parser that handles `Ki`/`Mi`/`Gi`/`Ti` suffixes and a bare
  byte-integer form, used by both the memory and ephemeral-storage loops.
- **`resolve_external_host()`'s docker-desktop/minikube autodetect ignored `KUBE_CONTEXT`**
  (install.sh:602, found via live testing against a remote `--kube-context`): the
  `kubectl config current-context` check always reports the kubeconfig's *ambient*
  current-context, not the one selected by `--context`/`KUBE_CONTEXT` — that flag
  has no effect on that particular subcommand. So targeting a non-current
  `KUBE_CONTEXT` (e.g. a Jenkins agent with a shared kubeconfig selecting a named
  remote cluster by context, a common CI pattern especially with concurrent jobs on
  one agent where mutating global current-context per job is a race condition)
  could silently misdetect the ambient ("docker-desktop") environment instead of the
  actual target cluster's, and that value flows straight into the chart via
  `--set global.externalHostAddress=...` — not just cosmetic. Fix: when
  `KUBE_CONTEXT` is non-empty, skip the minikube/docker-desktop heuristics entirely
  (they're statements about the *local machine's* own environment, meaningless once
  a specific — possibly remote — context is explicitly selected) and go straight to
  the node-IP fallback, which already goes through the `KUBE_CONTEXT`-aware
  `kubectl` wrapper. Still just a suggested default (`prompt_or_env` default arg) —
  set `EXTERNAL_HOST_ADDRESS`/`installer.externalHostAddress` explicitly when the
  node IP itself isn't reachable from where `install.sh` runs (e.g. still behind an
  SSH tunnel to the target cluster).
- **`resolve_external_host()`'s generic fallback (no heuristic matched) now suggests
  `localhost` instead of a node-IP lookup.** Only the truly generic case changed — the
  `KUBE_CONTEXT` branch (node IP; needed for e.g. the `vmdev137` lab pattern, where the
  node's real internal IP is reachable on the corporate network but `localhost` would
  resolve to nothing since only the API server port is SSH-tunneled) and the minikube/
  docker-desktop heuristics are untouched. The generic case (no `KUBE_CONTEXT`, not
  minikube, not docker-desktop — e.g. kind/k3d) previously did a node-IP lookup that's
  frequently unreachable for those tools, which typically NodePort-map to `localhost`
  instead. Still just a suggested default — override with `EXTERNAL_HOST_ADDRESS` when
  it's wrong for a given cluster.

## Testing

- Unit: `make installer-test` (`bats tests/install_tests.bats`) — 98 tests, no cluster needed (sources
  `install.sh` with `INSTALL_SH_SOURCE_ONLY=true`, stubs external binaries).
- **A green local run on macOS does not mean a green CI run.** bats aborts a test
  on the first failed assertion via `set -e`, and under macOS's system bash (3.2)
  that only works for the *last* statement in a `@test` — a failed `[[ ]]`
  anywhere before it is silently swallowed and the test still reports `ok`. CI
  runs bash 5, where every assertion counts. A test whose stub doesn't match what
  the code actually calls can therefore pass locally and fail in CI (this is
  exactly how the `resolve_external_host` `KUBE_CONTEXT` test shipped broken).
  When a test is doing real work, verify the assertion holds — run the inner
  `bash -c` body standalone and look at the output, or install bash >= 4
  (`brew install bash`) so local runs match CI.
- Live/integration: exercise `--chart-path` against a real chart checkout (see
  below). Non-interactive runs need `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`
  (or `REGISTRY_PASSWORD_FILE`)/`REGISTRY_EMAIL` set or they'll fail on the
  required-value check in `create_registry_secret`.
- **Verified against a real remote cluster via `--kube-context`**: `install.sh`
  itself never SSHes anywhere (still local-execution-only), but `kubectl`/`helm`
  can target any cluster reachable from the local machine — including one behind
  SSH, via a local port-forward tunnel (`ssh -f -N -L <port>:<remote-ip>:6443
  user@jumphost`) plus a kubeconfig context whose `server:` points at
  `localhost:<port>` (works cleanly when the cert's SANs already include
  `localhost`/`127.0.0.1`, true for kubeadm/rke2 defaults). `--config` + `-f`
  composition and `REGISTRY_PASSWORD_FILE` were both confirmed working through
  such a tunnel against a live `rke2` lab cluster, in addition to local
  `docker-desktop` runs. The remote run reached a fully healthy state (26/26
  containers ready, `helm status` → `deployed`) — notably including `mlrun-ui`,
  which fails on local Apple Silicon `docker-desktop` runs only because that
  image has no `linux/arm64` build; this lab cluster is x86_64.

  **Not yet torn down** (deliberately, as of this writing): the `mlrun-ce`
  release from this verification is still `deployed` in the `mlrun` namespace
  on the shared `vmdev137` lab cluster, and the local SSH tunnel
  (`ssh -f -N -L 16443:192.168.236.51:6443 iguazio@app1.vmdev137ig4.lab.iguaz.io`,
  backing the local `vmdev137` kubeconfig context) is still running. Tear down
  with `KUBE_CONTEXT=vmdev137 ./scripts/install.sh --uninstall --hard-clean
  --non-interactive` (also deletes its PVCs) when done needing it — that
  command is destructive enough against shared remote infra that it's worth
  running deliberately rather than as a matter of course.

## Cross-reference: the chart

The chart is now in this same repo at `charts/mlrun-ce` (it used to be a separate clone
reached via an absolute `--chart-path`; the installer was merged into the chart repo).
It has `Chart.yaml`, and its dependency subcharts are fetched into
`charts/mlrun-ce/charts/` by `helm dependency update`, which `resolve_chart_source` runs
automatically in local-path mode.

The installer only ever **reads** the chart — it never writes to `charts/`. Chart changes
follow the repo-root `AGENTS.md`/`CONTRIBUTING.md` (values.yaml conventions,
`requirements.lock`, version bumps), which are a separate concern from this directory.

Re-run the live dry-run test from the repo root:

```
REGISTRY_USERNAME=x REGISTRY_PASSWORD=y REGISTRY_EMAIL=z@z.com \
  ./scripts/install.sh --chart-path ./charts/mlrun-ce --dry-run --non-interactive
```
