## What this directory is

`scripts/install.sh` — single-file bash wrapper around `helm install mlrun-ce/mlrun-ce`
(published chart repo: `https://mlrun.github.io/ce`). Local execution only, no SSH, no git
fetching.

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
8. `run_validators` — skippable via `--skip-validators`; includes `validate_ingress_controller`, which warns (never blocks) if `--enable-ingress`'s IngressClass isn't found
9. `helm_install` → `resolve_chart_source` (published repo vs `--chart-path`) → `helm install/upgrade`, with `--values`/`--set` composed per the precedence rule below

Value precedence, highest first: flag > env > `ce-config.yaml` (applied as `--set`) >
`-f`/`--values` (passed raw) > chart defaults. `--config` and `-f` compose rather than
conflict — helm applies `--set` after `--values`, so config-resolved values always win
without any merge logic. `-f` used alone (no `--config`) keeps its self-contained
behaviour: no secret is created and the values file must reference an existing one.

`install_ingress_controller` (which used to `helm install` the `ingress-nginx` chart when
`--enable-ingress` was passed) was **removed**: installing a cluster's ingress controller
is not the installer's job. `--enable-ingress` is now BYO-controller-only — it sets the
chart's Ingress toggles and `validate_ingress_controller` warns if no matching
IngressClass exists.

## Versioning and releases

`installer_version()` (printed by `--version`) reads `version:` out of
`charts/mlrun-ce/Chart.yaml` next to the script, so bumping the chart bumps the installer
and there's no second copy to carry forward. Running standalone — `curl | bash`, or copied
to a bin directory — there's no chart to read and nothing in the script recording its
origin, so it reports `unknown` rather than inventing a number; that's the case pinning by
release tag exists to answer.

They're coupled because the installer encodes chart internals: `REQUIRED_NODEPORTS` is the
chart's fixed NodePort list, and `helm_install` writes chart-specific `--set` paths
(`global.registry.*`, `mlrun.{api,ui}.image.tag`, the `opentelemetry.*` and `components.*`
keys). An installer and a chart from the same tag are the only pairing guaranteed to
agree; a renamed value path would otherwise become a `--set` that silently does nothing.

There is no separate installer release. `.github/workflows/release.yml` runs
chart-releaser on every push to `development`/`X.Y.x`, tagging `mlrun-ce-<version>`, and
that tag's tree contains `scripts/install.sh` — which is what the pinned
`raw.githubusercontent.com/mlrun/ce/<tag>/scripts/install.sh` URLs resolve against.
Shipping an installer change is merging it with a chart version bump. The published chart
tarball packages `charts/mlrun-ce` only, so the installer ships via the git tag, not the
`.tgz`.

## Version floors

The installer's floors track the chart's own prerequisites, not the product install docs:

- **Helm >= 3.6, blocking** — mirrors `charts/mlrun-ce/README.md`'s prerequisites, so the
  installer can't refuse a Helm the chart itself supports. `MIN_HELM_VERSION` raises it.
- **No Kubernetes floor.** `validate_k8s_version` is informational: it reports the detected
  version, returns 0 on every path, and is not in `run_validators`' `|| failed=1` group.
  The chart declares no `kubeVersion` and the README states no cluster version, so there is
  no requirement to enforce. `MIN_K8S_VERSION` defaults to empty and only *warns* when set.

Both env vars exist to tighten, never to loosen. Keep `.github/workflows/installer-ci.yaml`'s
kind job unpinned for the same reason — with no floor to satisfy, the action's own default
node image is the safest choice.

## Known non-bugs

- **CE does not officially support upgrades**, so a `helm upgrade` over an existing release
  is out of scope as a supported path. The concrete symptom seen live (0.11.0 →
  0.12.0-rc.11 on a real cluster): the Kafka broker crash-loops with
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
  (found via live testing against a real remote cluster): both helm invocations in
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
  testing against a real remote cluster — a `--hard-clean` run sat blocked for
  18+ hours): both the PVC and PV delete loops fall back to
  `kubectl delete ... --force --grace-period=0` when the graceful `--timeout 60s` delete
  fails, but neither fallback passed `--wait=false` — by default `kubectl delete` still
  blocks waiting for the object to actually disappear from the API, `--force` only skips
  *graceful* deletion of the underlying pod, not the wait. When a PVC has a lingering
  `kubernetes.io/pvc-protection` finalizer (the exact orphaned-Strimzi-Kafka scenario in
  "Known non-bugs" above), nothing ever removes that finalizer, so the fallback hung just
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
  `KUBE_CONTEXT` branch (node IP; needed for the SSH-tunnelled remote-cluster pattern,
  where the node's real internal IP is reachable on the private network but `localhost`
  would resolve to nothing since only the API server port is tunnelled) and the minikube/
  docker-desktop heuristics are untouched. The generic case (no `KUBE_CONTEXT`, not
  minikube, not docker-desktop — e.g. kind/k3d) previously did a node-IP lookup that's
  frequently unreachable for those tools, which typically NodePort-map to `localhost`
  instead. Still just a suggested default — override with `EXTERNAL_HOST_ADDRESS` when
  it's wrong for a given cluster.

## Testing

- Unit: `make installer-test` (`bats tests/install_tests.bats`) — 102 tests, no cluster needed (sources
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
- **Never hide a tool by hardcoding a PATH of real system directories.** The
  GitHub runners ship `yq` in `/usr/bin`, so `PATH=/usr/bin:/bin` hides it on a
  macOS box (where it's in `/opt/homebrew/bin`) but not in CI — which is how the
  "load_config exits 1 when yq is not installed" test came to assert nothing in
  the only environment that was checking it. Use the `_empty_bin` helper, which
  points PATH at a directory that provably contains no executables.
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
  such a tunnel against a live `rke2` cluster, in addition to local
  `docker-desktop` runs. The remote run reached a fully healthy state (every
  container ready, `helm status` → `deployed`) — notably including `mlrun-ui`,
  which fails on local Apple Silicon `docker-desktop` runs only because that
  image has no `linux/arm64` build; the remote cluster was x86_64.

  Tear a verification release down with `KUBE_CONTEXT=<ctx> ./scripts/install.sh
  --uninstall --hard-clean --non-interactive` (also deletes its PVCs). That
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
