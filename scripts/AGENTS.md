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

## What pins the installer's behaviour

`install.py` plus the `ce_installer/` package is **the** installer. What it does to a
cluster is pinned by `tests/installer/golden/`: one recorded file per invocation, holding
every `helm`/`kubectl`/`docker` argv it issues and the exit code it ends on. The source is
free to be refactored; those recordings are not, unless you mean to change behaviour.

Practically: if `make installer-test-golden` fails, you changed what the installer does to
a cluster. Read the diff before re-recording with `make installer-test-golden-update` —
every changed line is something a user can observe.

Known limitation: with `--local-registry` and no `--enable-ingress`, the registry is
addressed as `local-registry.<ns>.svc.cluster.local:5000`. kaniko resolves that from inside
the cluster and pushes fine, but the image reference it writes is pulled by the node's
container runtime, which reads the host resolver and generally knows nothing about cluster
DNS — so the build succeeds and the function pod then fails to pull. Fixing it properly
means exposing a node-resolvable endpoint (a NodePort, or requiring ingress), which is a
design change rather than a bug fix. `gather_install_params` warns about it at the point it
prints the registry URL.

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
| `registry.py` | pull secret, the optional in-cluster registry, the CoreDNS entry report |
| `validators.py` | pre-install checks, blocking and advisory |
| `ui.py` | the live progress table and the access-URL table |
| `helm_ops.py` | `--set` composition, install, uninstall, hard clean |
| `cli.py` | installer version, argv pre-parse, `execute()` run order, the typer command |

`Settings` is the only place precedence is applied (flag > env > `ce-config.yaml` >
default). Nothing below `cli.py` and `config.py` reads `os.environ` for a tunable.

### Why the argv pre-parse in `cli.py` exists

click cannot express two things this CLI needs, so raw argv is rewritten before click
sees it:

1. `--enable-ingress [CLASS]` and `--enable-otel [MODE]` take an *optional* value, consumed
   only when the next token is not itself an option. Rewritten to `--flag=value`.
2. The otel flags are **order-sensitive** — a MODE names a complete state, so
   `--enable-otel collector --enable-otel-instrumentation` ends with instrumentation on and
   the reverse order does not. click does not preserve inter-option order, so
   `resolve_otel_flags` folds the *raw* argv left to right. The five otel parameters on the
   typer command exist only so they render in `--help`; their parsed values are unused.
   `otel_flags_present` reads the same raw argv to answer a question the resolved booleans
   cannot: whether the CLI said anything about otel at all. `ce-config.yaml`'s otel block is
   skipped entirely when it did, because a MODE can legitimately resolve to all-off, and
   "still False" cannot be told apart from "never mentioned".

Unknown options are rejected, and the pre-parse deliberately does not intervene to soften
that. The flags most worth typo-proofing are the ones that make a run safe: `--dry-rnu` is
not a dry run, and a misspelled `--skip-secret` replaces a registry secret you meant to
keep.

One click trap worth knowing: with `standalone_mode=False`, `command.main()` **returns** a
`typer.Exit`'s code instead of raising it. `main()` has to honour the return value or every
failure raised inside the command silently exits 0.

## Install flow (`execute()` in `cli.py`)

0. `parse_command` — pulls an optional leading verb (`install`/`uninstall`/`version`/`help`)
   off the front, leaving the rest in `COMMAND_ARGS`. Kept out of `parse_args` so that stays
   a pure flag parser. No verb (or a leading flag) means `install`, which is what every
   invocation predating commands relied on; an unrecognised bare word is an error rather
   than an install, so a typo can't deploy.
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

`installer_version()` (printed by the `version` command) resolves to the chart's version, so
bumping the chart bumps the installer and there's no second copy to carry forward. It
reaches that number two ways depending on whether a chart is on disk beside it — see
[Where the version comes from](#where-the-version-comes-from) below for the resolution
order and the build hook that covers the installed case.

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
declares the `mlrun-ce-installer` console script. A clone never goes through it at all:
`install.py` carries its own PEP 723 metadata and `uv run --script` ignores the surrounding
project.

### Where the version comes from

`charts/mlrun-ce/Chart.yaml` is the only version number in the repo, and nothing here is
bumped per release. It reaches `install.py version` two ways, tried in that order:

1. **The chart in the surrounding checkout.** Authoritative and always current — edit
   `Chart.yaml` and the next run reports the new value with nothing to rebuild.
2. **A value baked into the wheel at build time** by `scripts/hatch_build.py`, for an
   installed copy with no checkout around it.

The second exists because the wheel packages `ce_installer` only, so an installed copy has
no chart to read and used to report `unknown` — including for the pinned `uvx --from git+…`
form the README recommends to users without a clone. Hardcoding a version in
`pyproject.toml` would have fixed that by introducing exactly the second number the
chart-reading was meant to avoid, so `hatch_build.py` reads `../charts/mlrun-ce/Chart.yaml`
at build time instead; uv clones the whole repo before building the `scripts/` subdirectory,
so the chart is there to read. Two hooks off that one source:

- a metadata hook sets the **distribution** version, converted to PEP 440 — Helm's
  `0.12.0-rc.12` is not a legal Python version and becomes `0.12.0rc12`
- a build hook writes `ce_installer/_chart_version.py` holding the **literal** chart string,
  which is what gets displayed, then deletes it in `finalize()` so a build never leaves the
  checkout dirty

Building with no chart in reach (an sdist of `scripts/` alone) degrades to the old
behaviour — distribution `0.0.0`, `version` reports `unknown` — rather than failing. A
version string the translation does not recognise falls back the same way, deliberately: a
wrong version silently misidentifies what a user is running, which is worse than an absent
one.

Two consequences worth knowing. `make installer-test` pulls in `hatchling` because the
suite covers the hook. And moving either `scripts/` or `charts/` breaks the relative path
the hook depends on, which would change the version of every installed copy and nothing
else — `test_the_hook_reads_the_real_chart_in_this_repo` is there to catch that.

### Dependencies and `install.py.lock`

The four third-party dependencies — typer, click, rich, pyyaml — are declared **twice**, for
the two entry paths, and both declarations have to be kept in step:

| Declared in | Consumed by | Form |
|---|---|---|
| the PEP 723 header in `install.py` | `uv run --script`, i.e. the clone path | pinned via `install.py.lock` |
| `[project.dependencies]` in `pyproject.toml` | `uvx --from git+…`, `--with-editable ./scripts` | a range, resolved fresh |

`scripts/install.py.lock` is committed, and it is what makes `./scripts/install.py`
reproducible: `uv run --script` otherwise re-resolves the four on every user's machine, so a
new typer release could change behaviour for a user who changed nothing. Regenerate it
whenever the PEP 723 header changes:

```bash
uv lock --script scripts/install.py
```

Forgetting is caught — CI runs `uv lock --script scripts/install.py --check`, which exits 1
on a stale lock — but only after a push, so it is worth doing in the same commit.

The two declarations stay ranges-and-pins rather than pins-and-pins on purpose. The
installed path may land in an environment a user already has, so pinning there would cause
conflicts it has no business causing; the script path owns its environment outright, so it
can afford exact pins. Note that the ranges are what `make installer-test` resolves against,
via `--with-editable ./scripts` — the lock does not constrain the test run, which is why CI
tests both 3.9 and 3.13 rather than trusting one resolution.

The ranges carry upper bounds (`typer<1`, `click<9`, `rich<15`, `pyyaml<7`) because the
lock does **not** reach the `uvx --from "git+…#subdirectory=scripts"` path the README
recommends to users without a clone. Open-ended floors there meant a release-tagged install
resolved whatever those projects had published that morning, which is not what a tag is
for; a ceiling at the next major keeps it inside a range this chart was tested against
while still letting a patch land without a chart release.
`test_every_declared_dependency_has_an_upper_bound` enforces it.

`scripts/uv.lock` is a local development artifact and is gitignored; nothing consumes it.
Do not confuse either with `charts/mlrun-ce/requirements.lock`, which pins the chart's
sub-chart tarballs and has nothing to do with Python.

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
  can't fully dry-run without those CRDs present), not an installer bug.
- **Two `mlrun-ce` releases can't coexist on one cluster**, even in different
  namespaces with different release/secret names and NodePort overrides via `-f`.
  Confirmed live: the chart's `workflow-controller` `PriorityClass` is
  cluster-scoped with a hardcoded name (no values.yaml knob), so a second
  release's `helm install` fails immediately with an ownership-metadata error
  once one release already owns it. Not an installer bug — the chart itself
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

- **`helm --wait` returns before the OpenTelemetry collector is ready**, so the installer
  prints its success table while `kubectl get pods` still shows
  `mlrun-ce-otel-collector-* 0/1`. Same structural cause as the orphaned Strimzi CRs above,
  in the other direction: the collector is not in the release manifest either. The chart
  installs the *operator*, and the operator then reconciles an `OpenTelemetryCollector` CR
  into a Deployment — which only begins once helm has finished. There is nothing for
  `--wait` to wait on. **Observed live on rke2 (2026-09-16):** `0/1` at 80s,
  `1/1` shortly after, with the pod's own logs already reporting
  `Everything is ready. Begin running and processing data.` Not worth "fixing" by polling
  for it: the install genuinely is complete, and blocking on a component the release does
  not own would make every install slower to report what already succeeded.

- **`mlrun-api-chief` restarts once or twice on a fresh install.** It starts before
  `mlrun-db` accepts connections, fails its own startup with
  `sqlalchemy.exc.OperationalError: (pymysql.err.OperationalError) (2003, "Can't connect to
  MySQL server on 'mlrun-db' ([Errno 111] Connection refused)")`, and is restarted by the
  kubelet until the database is up. Self-healing, and the restart counter is the only
  lasting trace. Confirmed live on rke2 (2026-09-16): two restarts, then `2/2 Running`.
  Worth recognising on sight, because a non-zero restart count on the API pod is the first
  thing anyone looks at when an install is suspected of having gone wrong.

## Fixed bugs

- **A click usage error was a traceback on Python 3.10+ and a clean line on 3.9** (found
  while making unknown options fatal). typer >= 0.24 ships a vendored copy of click as
  `typer._click`, so the command `typer.main.get_command` builds raises *that* copy's
  `ClickException` — not a subclass of the `click.ClickException` `main()` was catching.
  Python 3.9 resolves typer 0.23, which still uses the real click, so the same invocation
  behaved differently on the two versions CI tests. It stayed hidden because nothing
  routine reached click's error path until unknown options stopped being ignored;
  `--dry-rnu` then printed a stack trace and exited 1 instead of one line and exit 2.
  `click_exception_types()` catches whichever applies. Worth remembering when catching
  anything else from click: the class depends on the resolved typer version, which depends
  on the user's Python.

- **The access-URL table put the wrong text in the URL column** (found by the first live
  install of the port, on an rke2 lab). `print_notes_table` assigned *every* non-empty line
  after a `X is available at:` header to `url`, so the last line won rather than the first:
  SeaweedFS showed `-  S3 credentials: seaweed / seaweed123` as its address, and TimescaleDB
  — the last entry in the NOTES — absorbed the whole trailing otel section and displayed a
  sentence of prose. Now the first non-empty line wins and a blank line closes the entry;
  a combined `-  ... credentials: <user> / <pass>` line is read as credentials, and a
  service with only one half no longer renders a dangling `postgres / `.

  The point worth keeping: the golden suite compares the helm/kubectl calls the installer
  makes, not what it prints, so no number of golden cases would have caught this. Output
  formatting needs its own tests or a live run.

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
  the release went `deployed`. Two regression tests assert the default and the override.

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
  (found via live testing against a remote `--kube-context`): the
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

`make installer-test` runs everything. `make installer-lint` runs `uvx ruff check` +
`ruff format --check`; `make installer-format` fixes what ruff can fix. The developer-facing
workflow — fixtures, naming, how to add a case — lives in
[`.claude/skills/run-tests`](../.claude/skills/run-tests/SKILL.md); this section covers why
the suites are shaped the way they are.

### Golden argv suite (`make installer-test-golden`)

The main guard on behaviour. `tests/installer/test_golden_argv.py` runs the whole
installer as a subprocess over 36 invocations with `tests/installer/stub.py` symlinked onto
a temporary PATH as `helm`, `kubectl`, `docker` and `minikube`, then compares the recorded
calls and exit code against `tests/installer/golden/`. No cluster is contacted and the temp
PATH is torn down after.

These files are the specification, not a snapshot of whatever the code happened to do. A
reviewer can read `git diff tests/installer/golden/` and see the full behavioural effect of
a change without reading a line of Python — which commands run, in what order, with which
flags, and what the installer exits with. `dry-rnu.txt` is the clearest example: empty, exit
2, recording that a misspelled safety flag reaches nothing at all.

- The stub derives its answers from the arguments rather than returning fixed values, so a
  test cannot pass by accident once the installer stops asking the question it was supposed
  to ask. `STUB_*` env vars steer the interesting branches (`STUB_SC_STABLE`,
  `STUB_HELM_EXIT`, `STUB_NODE_MEMORY`, …).
- Add a case to `CASES` whenever a flag gains behaviour that reaches helm or kubectl.
  Refusals belong there too — *how* the installer rejects a bad value is as much a contract
  as how it succeeds, and six of the 36 cases exist only to pin that.
- **The config-file cases carry an env rule.** `--config` supplies the registry identity,
  and flag/env beats file, so `run_case` drops `REGISTRY_URL`/`USERNAME`/`EMAIL` and
  `EXTERNAL_HOST_ADDRESS` for those invocations — otherwise the recording would look the
  same whether the file was parsed or ignored. `REGISTRY_PASSWORD` stays set, because it is
  never read from a config file. The fixtures in `tests/installer/fixtures/` use values like
  `host.from.config` so a flag carrying the wrong source is obvious in a diff.
- **Re-record deliberately.** `make installer-test-golden-update` rewrites the files;
  `git diff tests/installer/golden/` is then the reviewable part of the change. Re-recording
  without reading the diff turns the suite into a rubber stamp.
- **Watch for jsonpath escaping in the stub.** Annotation keys reach kubectl as
  `storageclass\.kubernetes\.io/is-default-class`; `jsonpath_of()` strips the backslashes
  before matching, because matching the escaped form made every lookup silently miss and
  turned the StorageClass validator permanently red while looking, in a diff, like nothing
  was wrong.

### Unit suites (`make installer-test-unit`)

253 tests across `test_cli.py`, `test_config.py`, `test_validators.py`, `test_cluster.py`,
`test_registry.py` and `test_regressions.py`. They patch the helm/kubectl wrappers and
exercise one function at a time, covering what the golden suite structurally cannot: values
computed and never sent to a command, text printed to the user, and the precedence rules
between flags, environment variables and `ce-config.yaml`.

#### Regression tests

`tests/installer/test_regressions.py` holds one named test per entry in "Fixed bugs" above,
plus the output formatting no argv comparison can see. Test names end in the symptom a user
would report, so a failure says what regressed.

- **Add a test here for every new "Fixed bugs" entry.** A bug that reached a user once is
  the cheapest possible test case, and the golden suite will not catch a second occurrence
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

#### Never let the environment decide a test

Every installer tunable is an environment variable, so a shell that has been used to drive a
real cluster is a hostile test environment. A leftover `export KUBE_CONTEXT=<lab>` once made
the `resolve_external_host` test fail with `localhost`, which reads exactly like
a code regression — `KUBE_CONTEXT` makes that function skip the local heuristics by design.
The hunt for a bug that did not exist cost more than the test was worth.

Two guards, both in `tests/installer/conftest.py`: an autouse `clean_env` fixture unsets
every installer variable before each test, and the `settings` fixture pins its fields rather
than reading the environment. Keep both in mind when adding a fixture of your own — the
moment a test reads `os.environ` directly, it can pass or fail based on who ran it.

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
