## What this directory is

A wrapper around `helm install mlrun-ce/mlrun-ce` (published chart repo:
`https://mlrun.github.io/ce`). Local execution only, no SSH, no git fetching.

It lives in the same repo as the chart it installs (`charts/mlrun-ce`), but installs the
**published** chart by default — the in-repo chart is used only when the caller passes
`--chart-path ./charts/mlrun-ce` explicitly. That's deliberate: the installer is also run
detached from any checkout, so the same invocation has to mean the same thing in both
places.

For chart-side conventions (values.yaml layout, `requirements.lock`, adding components) see
the repo-root `AGENTS.md`/`CONTRIBUTING.md`. This file covers the installer only.

## The port: install.py ships, install.sh is the oracle

`install.py` + the `ce_installer/` package is **the** installer. `install.sh` (single-file
bash, ~1545 lines) is not a supported alternative and is not released alongside it — it
stays in the tree only for as long as the port needs something to be checked against, and
is deleted at cutover. User-facing docs describe `install.py` only; don't reintroduce the
bash one as an option.

So: extend `install.py`. Touch `install.sh` only to keep the differential harness honest.

**The contract is the argv log, not the source.** `tests/installer/matrix.sh` runs both over
~28 invocations with recording stubs standing in for helm/kubectl/docker and fails if the
calls or exit codes differ. A change to `install.py` that is supposed to preserve behaviour
must keep that green; a change that is supposed to *alter* behaviour has to change
`install.sh` too, or drop the case from the matrix with a note saying why. Once `install.sh`
is deleted the harness loses its oracle, so the cases worth keeping have to be converted to
assertions against recorded expectations before that happens.

### Deliberate divergences from the bash behaviour

Three, recorded here because the matrix would otherwise be expected to catch them, and
because each is a behaviour change users can observe:

1. **`docker` is not a prerequisite.** `check_requirements` no longer gates on
   `docker info`; `validate_registry_auth` reports `skipped (docker not available)`. The
   pull secret is created by `kubectl create secret docker-registry`, never by Docker, so
   the only thing lost is a best-effort `docker login` that already degraded to a warning.
   This is what makes in-pod execution possible — a containerd/CRI-O node has no daemon.
2. **`--chart-path` prefers `helm dependency build`.** `update` re-resolves
   `requirements.yaml` and rewrites the lock, which is the maintainer operation;
   consumers want the lock honoured. `chart_deps_satisfied()` skips the fetch entirely
   when `charts/` already holds every tarball `requirements.lock` names, and
   `--skip-dependency-update` suppresses it unconditionally. Both exist so an
   egress-restricted host is not forced to reach the upstream Helm repos.
   Tarball names do not always equal the dependency name — the lock's
   `strimzi-kafka-operator` ships as `strimzi-kafka-operator-helm-3-chart-<v>.tgz` — so the
   match is a name prefix plus a version suffix, not an exact filename.
3. **No `yq`.** `--config` is parsed with pyyaml.

One bug the port fixes for free: `curl -sSL … | bash` makes the script itself bash's stdin,
so `read -r -p` consumes script text instead of the user's answer and the interactive
prompts are unusable. uv writes the script to a file before running it, leaving stdin
attached to the terminal.

### Module layout (`ce_installer/`)

In dependency order — each imports only from the ones above it. `install.py` is *only* a
launcher: PEP 723 metadata, the `_bootstrap()` uv re-exec, and a call to `main()`.

| Module | Holds |
|---|---|
| `console.py` | rich consoles, `log_info/warn/error`, `InstallerError`, `die` |
| `settings.py` | built-in defaults, `env_str`/`env_true`, the `Settings` dataclass, `prompt_or_env`, version floors |
| `shell.py` | `run`/`stream`, the KUBE_CONTEXT-aware `kubectl`/`helm` wrappers, `check_requirements` |
| `config.py` | the `ce-config.yaml` `installer:` block |
| `cluster.py` | namespace, external host address, chart source resolution |
| `registry.py` | pull secret, the optional in-cluster registry, the CoreDNS patch |
| `validators.py` | pre-install checks, blocking and advisory |
| `ui.py` | the live progress table and the access-URL table |
| `helm_ops.py` | `--set` composition, install, uninstall, hard clean |
| `cli.py` | installer version, argv pre-parse, `execute()` run order, the typer command |

`Settings` is the only place precedence is applied (flag > env > `ce-config.yaml` >
default). Nothing below `cli.py` and `config.py` reads `os.environ` for a tunable.

### Why the argv pre-parse in `cli.py` exists

click cannot express three things the bash `parse_args` does, so raw argv is rewritten
before click sees it:

1. `--enable-ingress [CLASS]` and `--enable-otel [MODE]` take an *optional* value, consumed
   only when the next token does not start with `--`. Rewritten to `--flag=value`.
2. The otel flags are **order-sensitive** — a MODE names a complete state, so
   `--enable-otel collector --enable-otel-instrumentation` ends with instrumentation on and
   the reverse order does not. click does not preserve inter-option order, so
   `resolve_otel_flags` folds the *raw* argv left to right. The five otel parameters on the
   typer command exist only so they render in `--help`; their parsed values are unused.
3. An unrecognised option warns and is ignored rather than aborting
   (`ignore_unknown_options` + `allow_extra_args`, then a warn loop over `ctx.args`).

One click trap worth knowing: with `standalone_mode=False`, `command.main()` **returns** a
`typer.Exit`'s code instead of raising it. `main()` has to honour the return value or every
failure raised inside the command silently exits 0.

## Install flow (`execute()` in `cli.py`; `main()` in install.sh ~1319-1378)

0. `parse_command` — pulls an optional leading verb (`install`/`uninstall`/`version`/`help`)
   off the front, leaving the rest in `COMMAND_ARGS`. Kept out of `parse_args` so that stays
   a pure flag parser. No verb (or a leading flag) means `install`, which is what every
   invocation predating commands relied on; an unrecognised bare word is an error rather
   than an install, so a typo can't deploy. `main()` expands `COMMAND_ARGS` with the
   `${a[@]+"${a[@]}"}` guard — bash < 4.4 treats an empty array as unset under `set -u`
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

`installer_version()` (printed by the `version` command) reads `version:` out of
`charts/mlrun-ce/Chart.yaml` next to the script, so bumping the chart bumps the installer
and there's no second copy to carry forward. It walks symlinks to the real file first —
`make installer-link` puts the command on PATH as a link into the checkout, and the link's
own directory has no chart in it. Running standalone — `curl | bash`, or copied
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
that tag's tree contains `scripts/`, which is what the pinned
`uvx --from "git+https://github.com/mlrun/ce@<tag>#subdirectory=scripts" mlrun-ce-installer`
invocation resolves against. Shipping an installer change is merging it with a chart version
bump. The published chart tarball packages `charts/mlrun-ce` only, so the installer ships
via the git tag, not the `.tgz`.

`scripts/pyproject.toml` exists purely to make that `uvx --from git+…` form work — it
declares the `mlrun-ce-installer` console script. Its `version` is a placeholder and is
**not** bumped per release; `installer_version()` reads the chart, so there is only ever one
number to maintain. A clone never goes through it at all: `install.py` carries its own
PEP 723 metadata and `uv run --script` ignores the surrounding project.

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
- `helm uninstall` (and `--hard-clean`) leaves orphaned Strimzi `Kafka`/
  `KafkaNodePool`/`StrimziPodSet` custom resources and their broker pod behind, so the
  pod's `kubernetes.io/pvc-protection` finalizer holds its PVC in `Terminating` forever.
  Fix is manual: delete the `strimzipodset` and pod directly (releasing the finalizer),
  then the `kafka`/`kafkanodepool` CRs.

  **Reconfirmed live on rke2 (2026-09-15)**, with a more specific cause than "ordering":
  the Kafka CRs are created as *helm hook* resources (`helm.sh/hook: post-install`), and
  helm never deletes hook-created resources on uninstall — they are not in the release
  manifest. The `strimzi-kafka-operator` Deployment *is* in the manifest, so it goes away
  while the CRs it was reconciling stay. `--hard-clean` reported success and exited 0 with
  the PVC still `Terminating`, because `--wait=false` means it never observes the outcome.
  A future `do_hard_clean` could reap this deterministically: after deleting PVCs, any left
  in `Terminating` with `pvc-protection` are pinned by a pod, and the pods are discoverable
  from `.spec.volumes[].persistentVolumeClaim.claimName`. Deliberately not done yet —
  deleting pods the release does not own is a bigger blast radius than it looks.

## Fixed bugs

- **The access-URL table put the wrong text in the URL column** (found by the first live
  install of the port, on an rke2 lab). `print_notes_table` assigned *every* non-empty line
  after a `X is available at:` header to `url`, so the last line won rather than the first:
  SeaweedFS showed `-  S3 credentials: seaweed / seaweed123` as its address, and TimescaleDB
  — the last entry in the NOTES — absorbed the whole trailing otel section and displayed a
  sentence of prose. Now the first non-empty line wins and a blank line closes the entry;
  a combined `-  ... credentials: <user> / <pass>` line is read as credentials, and a
  service with only one half no longer renders a dangling `postgres / `.

  **`install.sh` still has this bug** (`url="$line"` in its own `print_notes_table`) and is
  deliberately left alone — it is being deleted, and changing it would only churn the
  oracle. This is also a reminder of what the differential harness does *not* cover: it
  compares the helm/kubectl calls two implementations make, not what they print, so no
  number of matrix cases would have caught this. Output formatting needs its own tests or a
  live run.

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

  Follow-up: `do_uninstall` kept its literal `960s` and so ignored the new variable —
  same default, but a raised `HELM_TIMEOUT` didn't reach uninstall. It now passes
  `--timeout "${HELM_TIMEOUT}"` too.

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
- **`deploy_local_registry()` ignored `--dry-run` entirely** (found while rehearsing a demo
  of `--local-registry` on `docker-desktop`): the function had no `DRY_RUN` guard, so it ran
  `kubectl apply` unconditionally. Two failure modes, one loud and one quiet. On a cluster
  without the namespace — the normal case for a first dry run — the apply failed with a raw
  `Error from server (NotFound): namespaces "mlrun" not found`, `errexit` aborted, and the
  run exited 1, so `--local-registry --dry-run` was simply unusable. On a cluster where the
  namespace already existed, the apply *succeeded*: a run advertised as rendering-only
  really deployed a `local-registry` Deployment and Service, and reported success. CI never
  caught it because the `kind-install` job uses `--local-registry` for a real install, never
  with `--dry-run`. Fix: an early `return 0` under `DRY_RUN`, placed *after* the
  `LOCAL_REGISTRY_URL` assignment so the URL still reaches the rendered `--set` flags —
  verified live, the dry run renders the URL into nuclio's `registry_url` ConfigMap and
  mlrun's api chief/worker deployments while creating nothing. Three tests cover it: no
  apply under dry-run, the URL still resolving, and a real run still applying.

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
  node IP itself isn't reachable from where the installer runs (e.g. still behind an
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

`make installer-test` runs everything. `make installer-lint` runs shellcheck over
`install.sh` and `uvx ruff check` + `ruff format --check` over the Python;
`make installer-format` fixes what ruff can fix.

### Differential suite (`make installer-test-diff`)

The main guard on the port. `tests/installer/matrix.sh` drives
`tests/installer/difftest.sh` over ~28 invocations; each one runs `install.sh` and
`install.py` with `tests/installer/stub.py` symlinked onto a temporary PATH as `helm`,
`kubectl`, `docker` and `minikube`, and fails if the two disagree on either the exit code
or the sequence of recorded calls. No cluster is contacted and the temp PATH is torn down
afterwards.

- The stub derives its answers from the arguments rather than returning fixed values, so a
  test cannot pass by accident once a script stops asking the question it was supposed to
  ask. `STUB_*` env vars steer the interesting branches (`STUB_SC_STABLE`,
  `STUB_HELM_EXIT`, `STUB_NODE_MEMORY`, …).
- Add a case to `matrix.sh` whenever a flag gains behaviour that reaches helm or kubectl.
  Refusals belong there too — the two must agree on *how* they reject a bad value, not
  only on how they succeed.
- `VERBOSE=1 tests/installer/difftest.sh <flags>` prints both transcripts for one case.
- **Watch for jsonpath escaping in the stub.** Annotation keys reach kubectl as
  `storageclass\.kubernetes\.io/is-default-class`; `jsonpath_of()` strips the backslashes
  before matching, because matching the escaped form made every lookup silently miss and
  turned the StorageClass validator permanently red for both scripts at once — which
  *looked* like parity.

### Regression suite (`make installer-test-python`)

`tests/installer/test_regressions.py`, run by pytest under uv. One named test per entry in
"Fixed bugs" above, plus the output formatting the differential suite cannot see. Test names
end in the symptom a user would report, so a failure says what regressed.

- **Add a test here for every new "Fixed bugs" entry.** A bug that reached a user once is
  the cheapest possible test case, and the harness above will not catch a second occurrence
  unless the bug changes which commands get run.
- **Verify a new test by reintroducing the bug and watching it fail.** Two of these
  originally passed against the reverted fix because the parser fixes overlapped — either
  one alone kept the real-world fixture correct — so `PLAIN_DETAIL_NOTES` and
  `LATER_SECTION_NOTES` exist purely to isolate them. A test that cannot fail is
  documentation wearing a test's clothes.
- The `Recorder` helper stands in for the kubectl/helm wrappers and records argv. Patch the
  attribute **on the module under test** (`cluster.kubectl`), not on `shell` — each module
  imports the wrappers into its own namespace.
- The `settings` fixture pins its fields explicitly rather than reading the environment, so
  an exported `HELM_TIMEOUT` in a developer's shell cannot change a result.

### Legacy bats suite (`make installer-test-bash`)

- 118 tests over `install.sh`, no cluster needed (sources it with
  `INSTALL_SH_SOURCE_ONLY=true`, stubs external binaries). Retired with `install.sh`; the
  cases worth keeping move to the Python side rather than being rewritten in bats.
- **Test 87 (`resolve_external_host still uses the docker-desktop heuristic when
  KUBE_CONTEXT is unset`) currently fails**, producing `localhost` where it expects
  `host.docker.internal`. It predates the port — `install.sh` and the bats file have not
  changed since `6b445fa` — and is not being fixed in a script that is about to be deleted.
  The behaviour it guards is covered on the Python side by
  `test_external_host_without_kube_context_keeps_the_docker_desktop_heuristic`. Because of
  this, `make installer-test` is currently red on the bash leg only; use
  `installer-test-python` and `installer-test-diff` as the gate.
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
- **The Python installer verified end-to-end on rke2 (2026-09-15)**: v1.36.1, single node,
  `nfs-client` default StorageClass, helm **4.1.1**, reached through an
  `ssh -L 16443:127.0.0.1:6443` tunnel. A `--hard-clean` uninstall of an existing
  0.12.0-rc.11 release followed by
  `--chart-path ./charts/mlrun-ce --enable-otel collector --skip-secret` produced a
  `deployed` rc.12 release with 27/27 pods ready in ~3 minutes (warm image cache), the
  Kafka post-install hooks applied, and only the usual single `mlrun-api-chief` restart
  while it waits for the DB. `--skip-secret` correctly reused the pre-existing
  `registry-credentials`, which survives uninstall because the installer creates it with
  kubectl rather than through the chart.

  Two things to know about that run. **Helm 4 logs
  `Conflict: cannot merge map onto non-map for "registry". Skipping.` three times** during
  install; it is informational, caused by the chart's `global.registry: &userRegistry`
  anchor being multiplexed into `nuclio.*` and `mlrun.*`, and the value does apply —
  `index.docker.io/demo` reached the MLRun API pod and the user-supplied values matched the
  previous release exactly. And the **dependency skip works**: with all 8 lockfile subcharts
  vendored it logged `Chart dependencies already vendored and match requirements.lock;
  skipping fetch` and made no network call, where `install.sh` would have re-run
  `helm dependency update`.

- **Verified against a real remote cluster via `--kube-context`**: the installer
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

  Tear a verification release down with `KUBE_CONTEXT=<ctx> ./scripts/install.py
  uninstall --hard-clean --non-interactive` (also deletes its PVCs). That
  command is destructive enough against shared remote infra that it's worth
  running deliberately rather than as a matter of course.

## Cross-reference: the chart

The chart is now in this same repo at `charts/mlrun-ce` (it used to be a separate clone
reached via an absolute `--chart-path`; the installer was merged into the chart repo).
It has `Chart.yaml`, and its dependency subcharts are fetched into
`charts/mlrun-ce/charts/` by `resolve_chart_source`, which runs automatically in
local-path mode — `helm dependency build` when `requirements.lock` is present, and nothing
at all when `charts/` already satisfies the lock.

The installer only ever **reads** the chart — it never writes to `charts/`. Chart changes
follow the repo-root `AGENTS.md`/`CONTRIBUTING.md` (values.yaml conventions,
`requirements.lock`, version bumps), which are a separate concern from this directory.

Re-run the live dry-run test from the repo root:

```
REGISTRY_USERNAME=x REGISTRY_PASSWORD=y REGISTRY_EMAIL=z@z.com \
  ./scripts/install.py --chart-path ./charts/mlrun-ce --dry-run --non-interactive
```
