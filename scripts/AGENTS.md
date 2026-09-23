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

## The port from bash, and what survives it

`install.py` + the `ce_installer/` package is **the** installer. It replaced `install.sh`
(single-file bash, ~1545 lines), which was never released alongside it: the bash script
stayed in the tree only while the port needed something to be checked against, and was
deleted once that role ended.

**The contract is the argv log, not the source.** While both existed,
`tests/installer/matrix.sh` ran them over 32 invocations with recording stubs standing in
for helm/kubectl/docker and failed if the calls or exit codes differed. At cutover all 32
were identical, and that log was frozen into `tests/installer/golden/` as the expectation
for `install.py` alone. So the guard did not disappear with the oracle — it changed from
"these two agree" to "this one still does what it did on the day they agreed", which is the
same property with one fewer moving part.

Practically: if `make installer-test-golden` fails, you changed what the installer does to a
cluster. Re-record only after reading the diff (`make installer-test-golden-update`).

### Deliberate divergences from the bash behaviour

Recorded here because each is a behaviour change users can observe, and because the golden
expectations bake them in — a reader comparing against the old bash script would otherwise
read them as regressions. The first five came out of the port itself; the rest out of the
two review rounds on #315, where a faithful port turned out to have faithfully carried a
bug across.

1. **`docker` is not a prerequisite.** `check_requirements` no longer gates on
   `docker info`; `validate_registry_auth` reports `skipped (docker not available)`. The
   pull secret is a Secret manifest applied with kubectl, never built by Docker, so the
   only thing lost is a best-effort `docker login` that already degraded to a warning.
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
4. **A malformed config file is now fatal.** bash ran `yq eval … 2>/dev/null || true`, so a
   YAML syntax error read as an empty value for *every* field and the install continued on
   built-in defaults — the user got a working-looking run that silently ignored their
   config. The port raises `Could not parse config file`. Related: bash could not tell YAML
   null from the string `null` and blanked both, so `url: "null"` came through empty there
   and stays `"null"` here.
5. **stderr is no longer folded into stdout.** `shell.run` captures the two separately.
   Merging them was the bash behaviour only by accident (`2>/dev/null` discarded stderr
   outright), and it actively broke things once output started being parsed rather than
   just displayed: `deploy_local_registry` pipes rendered YAML from one kubectl into the
   stdin of the next, where a single kubectl warning line would have been applied to the
   cluster as part of the manifest, and `parse_major_minor` takes the first `vN.N` anywhere
   in its input, so a deprecation warning naming a Kubernetes version would be read as the
   cluster's own. Found while porting the validator tests.
6. **An unrecognised option is fatal.** bash logged `Unknown option: X (ignored)` and
   carried on, which meant a typo silently changed what the run did: `--dry-rnu` performed
   a real install, and a misspelled `--skip-secret` rewrote a registry secret the user
   meant to keep. There is no forward-compatibility argument on the other side, because
   the installer ships with the chart and its flags and its chart are one version. click's
   own default behaviour, reached by dropping `ignore_unknown_options`/`allow_extra_args`;
   exit code 2, not 1.
7. **An option value may not start with a dash.** bash tested `!= --*`, so
   `--enable-ingress -f values.yaml` read `-f` as the ingress class and stranded the path.
   Everything that takes a value — ingress class, otel mode, chart path, values file,
   version — now treats any dash-prefixed token as the next option instead. Under (6) the
   stranded argument is a hard error rather than a warning, so the invocation fails
   outright instead of installing something subtly different from what was asked for.
8. **`installer.chartSource.kind` is validated.** Only `repo`, `path` or unset. `kind: pth`
   used to fall through to repo mode and install the *published* chart while the file was
   plainly asking for the local one — the difference between testing your branch and
   testing whatever is on the chart repo, reported neither way.
9. **A `--config` file cannot override an `--enable-otel` mode.** The four otel booleans
   are the one set where a flag legitimately resolves to `False`, so "still False" cannot
   be read as "unset": `--enable-otel off` looked identical to silence, and any
   `otel.*: true` in the file switched back on what the flag had just turned off.
   `otel_set_by_cli` records that the CLI spoke, and `load_config` then leaves the whole
   block alone. Same rule the flags already follow — a MODE names the complete state.
10. **The `-f` path is checked before anything touches the cluster.** It used to be
    validated after `ensure_namespace` and `deploy_local_registry`, so a mistyped path left
    a namespace and a running registry Deployment behind on the way to reporting that the
    file was never there.
11. **The pull secret is piped in as a manifest.** `kubectl create secret docker-registry`
    takes the password as an argv element, which puts it in the process table for the
    length of the call and — via the `Command failed: …` message `run` prints on a
    `check=True` failure — into stderr and any CI log capturing it. `docker_config_secret`
    renders the identical `kubernetes.io/dockerconfigjson` Secret and `kubectl apply -f -`
    reads it from stdin. `shell.redact` masks known credential flags in that message as
    well, so a future call site that does pass one is not a fresh leak.
12. **`REGISTRY_PASSWORD_FILE` is not exported.** bash assigned the file's contents to
    `REGISTRY_PASSWORD`, which every helm, kubectl and docker subprocess then inherited —
    defeating the point of supplying it as a file. It is a local variable now.
13. **The NodePort check is scoped by ownership, not by namespace.** bash skipped every
    Service in the target namespace as "probably ours". NodePorts are cluster-wide and helm
    will not adopt a Service it does not own, so an unrelated Service in `mlrun` holding
    30040 was reported as "no conflicts detected" and the install then failed on it. The
    jsonpath now carries `meta.helm.sh/release-name` and only this release's own Services
    are skipped.
14. **The ingress controller Service is looked for where it usually is.** bash hardcoded
    `ingress-nginx-controller` in the release namespace; a stock ingress-nginx installs
    into `ingress-nginx`, so on most clusters the CoreDNS patch was quietly skipped.
    `ingress-nginx/ingress-nginx-controller` is tried first and the release namespace
    second, and `INGRESS_CONTROLLER_SERVICE` (or
    `installer.localRegistry.ingressControllerService`) names it outright for Traefik and
    anything else that cannot be guessed at.
15. **`helm repo add` is forced and checked.** bash ran it as `… 2>/dev/null || true`, and
    plain `repo add` errors when the `mlrun-ce` alias already points somewhere else — so an
    alias left from an earlier run decided where the chart came from and `--helm-repo-url`
    was silently ignored. `--force-update` plus `check=True` makes the requested URL the
    one that serves the chart.
16. **kaniko is told the local registry is insecure, in both modes and at a key a chart
    reads.** Two bugs in one flag. bash gated it on `LOCAL_REGISTRY && ENABLE_INGRESS`, but
    `registry:2` serves plain HTTP on its ClusterIP exactly as it does behind the ingress,
    so `--local-registry` alone had kaniko pushing to an `https://` URL and failing on TLS
    — the condition is now just `--local-registry`. The flag itself was also
    `mlrun.api.kaniko.insecureRegistry`, which no chart in the umbrella has ever read: the
    builder is nuclio's dashboard (`nuclio.dashboard.containerBuilderKind: kaniko`) and the
    keys are `nuclio.dashboard.kaniko.insecurePushRegistry` / `insecurePullRegistry`. helm
    accepts an unknown `--set` path silently, so the flag looked present and did nothing for
    as long as it existed. `--disable-mpi` had the same disease: `mpi-operator.rbac.create`
    does not exist, the subchart splits it into `rbac.clusterResources.create` and
    `rbac.namespaced.create`, and the latter defaults on — so disabling MPI still created
    the operator's ServiceAccount and RoleBinding. `test_every_set_flag_names_a_key_the_
    chart_declares` resolves every non-`global.*` `--set` path against the umbrella's
    values merged with each vendored subchart's, so a third one cannot be added quietly.
17. **The CoreDNS hosts entry is reported, not applied.** bash rewrote the `kube-system`
    `coredns` ConfigMap with a regex and restarted the Deployment. That is cluster-wide
    configuration nothing in the release namespace owns, in a file format the installer does
    not control; a bad rewrite takes DNS down for every workload on the cluster, and
    `--uninstall` never took the entry back out. `report_coredns_entry_for_registry` prints
    the `ip host` line and the two commands to apply it, and touches nothing. The
    ingress-controller ClusterIP lookup is kept — it is what supplies the IP in the message.
18. **The pull secret is updated in place.** The delete-then-create was required by bash's
    `kubectl create secret`, which refuses to overwrite; the port kept it after switching to
    `apply`, which does not need it. That left a window with no credentials on the release,
    and lost them outright if anything failed in between. The delete survives only as a
    fallback for the one thing `apply` cannot do — change a Secret's immutable `type`.
19. **`--show-progress` only takes over a terminal.** The progress UI already returned
    immediately off a tty, but helm was still being run into a temp file that a successful
    run then deleted — so `--show-progress` under CI produced no install output at all.
    bash had the same hole. The terminal check moved into the branch condition, so a
    redirected run streams helm's own output, which is what the docstring always claimed.
20. **`--dry-run` is honoured on the uninstall path.** It was only ever read while building
    the install command, so `--uninstall --hard-clean --dry-run` really removed the release
    and really deleted every PVC and bound PV. The reads still run — listing what would go
    is the point — and every mutation is now guarded and reported instead.
21. **`--non-interactive` works on the documented CI path.** Three separate stops. The
    required-field check was the last statement of `load_config`, which returns early when
    there is no `--config`, so an env-var-only run got no up-front check and instead died
    later, one variable at a time, inside `prompt_or_env`; it moved to `execute()`. It also
    ran ahead of the uninstall branch, so tearing a release down demanded registry
    credentials. And `REGISTRY_EMAIL` — which registries stopped caring about years ago —
    had no default, so "no default available" made it mandatory; `prompt_or_env` grew an
    `allow_empty` for fields whose real value is empty rather than unset.
22. **A Service with no ports no longer hides every NodePort conflict behind it.** The
    newline in `NODEPORT_JSONPATH` sat inside the `.spec.ports[*]` range, so a portless
    Service emitted no line terminator, the next Service's `ns/release` prefix was parsed as
    its port number, and every real port on that line fell out of the parser's reach. The
    newline terminates the Service now and each line is one Service.
23. **`--dry-run` degrades instead of failing on Helm below 3.13.** `--dry-run` only became
    a string flag in 3.13; before that `--dry-run=server` is an invalid boolean and the
    install never starts. The chart's own floor is 3.6, so the documented flag was unusable
    on most supported helms. `dry_run_flag` checks the version and falls back to a
    client-side dry run with a warning saying what that no longer covers.
24. **A bad `--chart-path` is caught before the cluster is touched.** `resolve_chart_source`
    validates it, but only runs after `ensure_namespace` and `deploy_local_registry` — so a
    typo left a namespace and a running registry behind on the way to the error message.
    `validate_chart_path` is called from `execute()` first, the same fix `-f` already had.
25. **A dry run does not log in to the registry.** `validate_registry_auth` runs a real
    `docker login`, and a successful one rewrites the invoking user's
    `~/.docker/config.json` — the one thing a dry run promises not to do to the machine it
    runs on. It is skipped under `--dry-run` and says so.
26. **The reported Kubernetes version is the API server's.** It came from
    `.items[0].status.nodeInfo.kubeletVersion`: an arbitrary node's kubelet, which is
    allowed to trail the control plane by two minor versions and can be either side of the
    split on a cluster mid-upgrade. `kubectl version -o json` asks the API server, and needs
    a much smaller permission than listing nodes.
27. **Every default StorageClass is named.** The validator matched them all and then printed
    `matched[0]`, so a cluster with two defaults — a misconfiguration Kubernetes accepts and
    then resolves arbitrarily — was reported as a healthy single default. All are listed,
    with a warning when there is more than one.
28. **A hard clean leaves PVs that are still Bound.** The phase was fetched into the
    `custom-columns` query and then ignored, so every PV whose `claimRef` named the
    namespace was deleted, including ones still bound to a live PVC that the release may not
    own. Only `Released` and `Failed` are deleted; anything else is listed as skipped.
29. **`minikube ip` is asked once.** `resolve_external_host` called it to test the exit code
    and again to read the answer, doubling the wait on every run for a value it already had.
30. **The user's helm repository list is left alone.** `helm repo add --force-update
    mlrun-ce` rewrites `~/.config/helm/repositories.yaml`, permanently rebinding an alias the
    user may have pointed elsewhere, and nothing put it back. The run gets a throwaway
    `HELM_REPOSITORY_CONFIG` unless the user named one themselves.

Known limitation, not a divergence: with `--local-registry` and no `--enable-ingress` the
registry is addressed as `local-registry.<ns>.svc.cluster.local:5000`. kaniko resolves that
from inside the cluster and pushes fine, but the image reference it writes is pulled by the
node's container runtime, which reads the host resolver and generally knows nothing about
cluster DNS — so the build succeeds and the function pod then fails to pull. bash had the
same shape and fixing it properly means exposing a node-resolvable endpoint (a NodePort, or
requiring ingress), which is a design change rather than a review fix. `gather_install_params`
warns about it at the point it prints the registry URL.

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
| `registry.py` | pull secret, the optional in-cluster registry, the CoreDNS entry report |
| `validators.py` | pre-install checks, blocking and advisory |
| `ui.py` | the live progress table and the access-URL table |
| `helm_ops.py` | `--set` composition, install, uninstall, hard clean |
| `cli.py` | installer version, argv pre-parse, `execute()` run order, the typer command |

`Settings` is the only place precedence is applied (flag > env > `ce-config.yaml` >
default). Nothing below `cli.py` and `config.py` reads `os.environ` for a tunable.

### Why the argv pre-parse in `cli.py` exists

click cannot express two things the bash `parse_args` does, so raw argv is rewritten
before click sees it:

1. `--enable-ingress [CLASS]` and `--enable-otel [MODE]` take an *optional* value, consumed
   only when the next token is not itself an option. Rewritten to `--flag=value`.
2. The otel flags are **order-sensitive** — a MODE names a complete state, so
   `--enable-otel collector --enable-otel-instrumentation` ends with instrumentation on and
   the reverse order does not. click does not preserve inter-option order, so
   `resolve_otel_flags` folds the *raw* argv left to right. The five otel parameters on the
   typer command exist only so they render in `--help`; their parsed values are unused.
   `otel_flags_present` reads the same raw argv for divergence 9 — whether the CLI said
   anything about otel at all, which the resolved booleans cannot answer.

The third thing bash did here, tolerating unknown options, is divergence 6: click rejects
them and the pre-parse does not intervene.

One click trap worth knowing: with `standalone_mode=False`, `command.main()` **returns** a
`typer.Exit`'s code instead of raising it. `main()` has to honour the return value or every
failure raised inside the command silently exits 0.

## Install flow (`execute()` in `cli.py`)

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

  `install.sh` had the identical bug (`url="$line"` in its own `print_notes_table`) and was
  left alone, since it was already scheduled for deletion. That is the point worth keeping:
  the differential harness compared the helm/kubectl calls two implementations made, not
  what they printed, so no number of matrix cases would have caught this — and the golden
  suite that replaced it has the same blind spot. Output formatting needs its own tests or a
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

`make installer-test` runs everything. `make installer-lint` runs `uvx ruff check` +
`ruff format --check`; `make installer-format` fixes what ruff can fix. The developer-facing
workflow — fixtures, naming, how to add a case — lives in
[`.claude/skills/run-tests`](../.claude/skills/run-tests/SKILL.md); this section covers why
the suites are shaped the way they are.

### Golden argv suite (`make installer-test-golden`)

The successor to the differential harness, and the main guard on behaviour.
`tests/installer/test_golden_argv.py` runs the whole installer as a subprocess over 34
invocations with `tests/installer/stub.py` symlinked onto a temporary PATH as `helm`,
`kubectl`, `docker` and `minikube`, then compares the recorded calls and exit code against
`tests/installer/golden/`. No cluster is contacted and the temp PATH is torn down after.

Those expectations are not arbitrary snapshots. They were recorded from `install.py` while
`install.sh` still existed, and the differential harness confirmed all 32 of the cases that
existed then identical between the two on the same commit — so they encode the bash
script's behaviour, which is what makes deleting it safe. Where a line has since moved, the
divergence list above says why; `dry-rnu.txt` (empty, exit 2) is the clearest of them,
recording that a typo now reaches nothing at all.

- The stub derives its answers from the arguments rather than returning fixed values, so a
  test cannot pass by accident once the installer stops asking the question it was supposed
  to ask. `STUB_*` env vars steer the interesting branches (`STUB_SC_STABLE`,
  `STUB_HELM_EXIT`, `STUB_NODE_MEMORY`, …).
- Add a case to `CASES` whenever a flag gains behaviour that reaches helm or kubectl.
  Refusals belong there too — *how* the installer rejects a bad value is as much a contract
  as how it succeeds, and six of the 34 cases exist only to pin that.
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
  turned the StorageClass validator permanently red — which, when two implementations were
  being compared, *looked* like parity.

### Unit suites (`make installer-test-unit`)

182 tests across `test_cli.py`, `test_config.py`, `test_validators.py`, `test_cluster.py`,
`test_registry.py` and `test_regressions.py`. They patch the helm/kubectl wrappers and
exercise one function at a time, covering what the golden suite structurally cannot: values
computed and never sent to a command, text printed to the user, and the precedence rules
between flags, environment variables and `ce-config.yaml`.

Most were ported from the 118-case bats suite that covered `install.sh`. One case did not
survive: bash needed `yq` to read a config file and had a test for its absence, where the
Python port parses YAML with pyyaml and has no such dependency.

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
the bash suite's `resolve_external_host` test fail with `localhost`, which reads exactly like
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
