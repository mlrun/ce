# Configuration

How to drive `install.sh` beyond the interactive defaults: the `ce-config.yaml` file,
values files, precedence between all the input sources, and the flows that configure
ingress, a local registry, disabled components, and OpenTelemetry.

For the full flag/env-var list, see [parameters.md](parameters.md). For "why is X
behaving this way" gotchas, see [faq.md](faq.md).

---

## Install from a config file

Reads registry/chart-source/host defaults from a `ce-config.yaml`. See
[Config file schema](#config-file-ce-configyaml) below. CLI flags and env vars still
take precedence over the file.

**`--config` works in both interactive and non-interactive mode — you don't have to
choose.** Without `--non-interactive`/`CI=true`, you still get prompted for anything not
already set by a flag/env var, but each prompt's default is now pre-filled from the
config file — press Enter to accept it, or type something else to override just that one
field for this run:

```bash
cp scripts/ce-config.yaml.example ce-config.yaml   # edit with your registry/chart-source values
./scripts/install.sh --config ce-config.yaml
# e.g. "Docker registry password [from a value you can't put in the file]: " still prompts,
# but "Docker registry URL for images [index.docker.io/myuser]: " now shows the file's value —
# just press Enter to accept it.
```

For CI, add `--non-interactive` (or `CI=true`) so any field missing from *both* the file
and a flag/env fails fast with exit 1 instead of hanging on a prompt:

```bash
CI=true ./scripts/install.sh --config ce-config.yaml
```

## Install from a values file

Skips secret creation and all prompts:

```bash
./scripts/install.sh -f my-values.yaml
```

## Install with a remote kubeconfig context

```bash
KUBE_CONTEXT=my-remote-cluster ./scripts/install.sh
```

`install.sh` itself never SSHes anywhere — it still only runs `kubectl`/`helm` against
whatever cluster `KUBE_CONTEXT` (or the ambient current-context) points at. To reach a
cluster that's only accessible over SSH (e.g. a lab VM), open a local port-forward
tunnel yourself first, then add a context whose `server:` points at `localhost:<port>`:

```bash
ssh -f -N -L 16443:<remote-cluster-ip>:6443 user@jump-host
kubectl config set-cluster my-remote-cluster --server=https://localhost:16443 \
  --certificate-authority=<ca.crt> --embed-certs=true
kubectl config set-credentials my-remote-cluster --client-certificate=<client.crt> \
  --client-key=<client.key> --embed-certs=true
kubectl config set-context my-remote-cluster --cluster=my-remote-cluster --user=my-remote-cluster

KUBE_CONTEXT=my-remote-cluster ./scripts/install.sh --dry-run --non-interactive
```

This works cleanly when the remote cluster's serving cert already lists `localhost`/
`127.0.0.1` in its SANs (true for kubeadm/rke2 defaults). When targeting a non-current
`KUBE_CONTEXT`, `EXTERNAL_HOST_ADDRESS` autodetection skips the minikube/docker-desktop
heuristics (which only make sense for the *ambient* local environment) and falls back to
the target cluster's node IP — set `EXTERNAL_HOST_ADDRESS`/`installer.externalHostAddress`
explicitly if that node IP isn't actually reachable from where you're running
`install.sh` (e.g. still behind the SSH tunnel). See [faq.md](faq.md) for the full
autodetect fallback chain.

## Install with live progress UI

Shows a refreshing table of deployments and statefulsets while Helm runs:

```bash
./scripts/install.sh --show-progress
```

## Pin the chart version

```bash
./scripts/install.sh --ce-version 0.11.0
```

## Install with optional components disabled

```bash
./scripts/install.sh \
  --disable-system-monitoring \
  --disable-spark \
  --disable-mpi \
  --disable-model-monitoring
```

## Install with OpenTelemetry

```bash
# Basic metrics pipeline (operator + collector), no auto-instrumentation
./scripts/install.sh --enable-otel collector

# Everything, including namespace-wide auto-instrumentation
./scripts/install.sh --enable-otel full
# same as:
./scripts/install.sh --enable-otel

# Or pick individual knobs directly
./scripts/install.sh --enable-otel-operator --enable-otel-collector
```

`--enable-otel-namespace-label` has a namespace-wide blast radius (it auto-instruments
every Python pod in the release namespace once `--enable-otel-instrumentation` is also
on) — `full` mode includes it, `collector` mode doesn't.

## Install with ingress

Enables the chart's own Ingress resources (UI, API, Jupyter, Nuclio dashboard) for HTTP
hostname routing. **This installer does not install an ingress controller** — bring your
own (e.g. [ingress-nginx](https://kubernetes.github.io/ingress-nginx/)) and have it
already running in the cluster first. If no IngressClass matching `--enable-ingress`'s
class (default `nginx`) is found, the pre-install validators warn but the install still
proceeds — the Ingress resources are created either way, they just won't resolve until a
controller providing that class exists.

```bash
./scripts/install.sh --enable-ingress

# Use a custom ingress class name (must match a controller already in the cluster)
./scripts/install.sh --enable-ingress myclass
```

## Install with local registry

Deploys a `registry:2` container inside the cluster as a ClusterIP service and wires
MLRun CE to use it.

Without `--enable-ingress`, the registry is reachable only in-cluster via its Kubernetes
service DNS name (`local-registry.<namespace>.svc.cluster.local:5000`):

```bash
./scripts/install.sh --local-registry
```

## Install with ingress + local registry

Combines both flags: an Ingress route is created for the local registry at
`registry.<host>`, on top of whatever ingress controller you already have running in the
cluster (see "Install with ingress" above — this installer never installs one). This
makes the registry reachable from the same hostname both inside pods and from your
terminal.

```bash
./scripts/install.sh --enable-ingress --local-registry
```

Docker Desktop TLS/hosts-file setup for this combo is covered in [faq.md](faq.md).

## Pre-install validators

Before every real (or dry-run) install, the installer runs a set of read-only pre-flight
checks against the target cluster:

- **Blocking** (exit 1, no `helm` call is made): Helm CLI version >= 3.6, a default
  StorageClass exists.
- **Warning only** (logged, install continues): the cluster's Kubernetes version (reported
  always, and compared only against an explicitly set `MIN_K8S_VERSION`), registry login with the resolved
  credentials (skipped for `--local-registry` or when no credentials were resolved yet,
  e.g. `-f`-only mode), an IngressClass matching `--enable-ingress`'s class exists
  (skipped when `--enable-ingress` isn't used — this installer never installs a
  controller itself), the chart's fixed NodePorts
  (`30010/20/40/50/60/70`, `30093/94`, `30100`, `30110`) already in use by another
  Service outside the target namespace, and total cluster node capacity below the
  documented floor (8Gi allocatable RAM / 8Gi allocatable ephemeral storage).

All checks run and report together — a blocking failure doesn't stop the others from
running, so you see every problem in one pass. Skip the whole dispatcher with
`--skip-validators` / `SKIP_VALIDATORS=true` if you need to proceed anyway:

```bash
./scripts/install.sh --skip-validators
```

### Version floors

The Helm >= 3.6 floor mirrors the prerequisite in
[the chart README](../../charts/mlrun-ce/README.md#prerequisites), so the installer never
refuses a Helm version the chart itself supports.

**There is no Kubernetes floor.** The chart declares no `kubeVersion` in `Chart.yaml` and the
README states no cluster version, so the installer has nothing to enforce and doesn't invent
one — it reports the version it finds and moves on.

Both are overridable, which is mainly useful for tightening rather than loosening. Set
`MIN_K8S_VERSION` to get a warning on clusters below a version you care about, and raise
`MIN_HELM_VERSION` to hard-require a newer Helm:

```bash
MIN_K8S_VERSION=1.34 MIN_HELM_VERSION=4.1 ./scripts/install.sh --chart-path ./charts/mlrun-ce --dry-run
```

`MIN_K8S_VERSION` only ever warns; it never blocks the install. Only `MIN_HELM_VERSION` is
enforced as a hard floor.

---

## Config file (`ce-config.yaml`)

`--config FILE` (or `CONFIG_FILE` env) reads a single reserved `installer:` block from a
YAML file. Every key resolves to the same flag/env var above it, following the usual
precedence: **flag > env var > `ce-config.yaml` > default**. In interactive mode, a value
found in the config file becomes the prompt's default (press Enter to accept it); in
`--non-interactive`/CI mode it's used directly with no prompt.

```yaml
installer:
  kubeContext: ""                # optional; blank = current local kubeconfig context
  externalHostAddress: auto      # "auto" = existing autodetect (minikube/docker-desktop/node IP); or pin a value
  registry:
    url: index.docker.io/myuser  # used if REGISTRY_URL isn't set on the CLI/env
    secret:
      username: myuser           # used if REGISTRY_USERNAME isn't set
      server: https://index.docker.io/v1/
      email: me@example.com
      # password: never put this here — set REGISTRY_PASSWORD (env), REGISTRY_PASSWORD_FILE
      #           (path to a file containing just the password), or use the interactive
      #           masked prompt. A password key here is detected and ignored with a warning.
  chartSource:
    kind: repo                   # repo | path
    chartVersion: 0.11.0         # repo mode -> --ce-version
    chartPath: ""                # path mode -> --chart-path; REQUIRED (and validated) when kind: path
  versions:
    mlrun: ""                    # -> --set mlrun.{api,ui}.image.tag, mlrun.api.sidecars.logCollector.image.tag
    nuclio: ""                   # -> --set nuclio.{controller,dashboard}.image.tag
  components:                    # mirrors --disable-system-monitoring/-spark/-mpi/-model-monitoring
    monitoring: true             # false -> same --set as --disable-system-monitoring
    spark: true                  # false -> same --set as --disable-spark
    mpi: true                    # false -> same --set as --disable-mpi
    modelMonitoring: true        # false -> same --set as --disable-model-monitoring
  otel:                          # all 4 ship OFF in the chart, so each key opts IN (mirrors
                                  # --enable-otel-*/ENABLE_OTEL_*) — independent knobs, not one
                                  # bundled toggle, so you can enable e.g. just operator+collector
                                  # without namespaceLabel/instrumentation's namespace-wide effect.
    operator: false              # -> same --set as --enable-otel-operator
    collector: false             # -> same --set as --enable-otel-collector
    namespaceLabel: false        # -> same --set as --enable-otel-namespace-label
    instrumentation: false       # -> same --set as --enable-otel-instrumentation
```

### Required fields

`load_config` exits 1 with every missing field listed, rather than one at a time:

| Config key                           | Mirrors today's     | Required unless                            |
|---------------------------------------|----------------------|---------------------------------------------|
| `installer.registry.url`              | `REGISTRY_URL`       | `--local-registry` is used                  |
| `installer.registry.secret.username`  | `REGISTRY_USERNAME`  | —                                            |
| `installer.registry.secret.password`  | `REGISTRY_PASSWORD`  | — (**never put this in the file** — `REGISTRY_PASSWORD`/`REGISTRY_PASSWORD_FILE` env, or the interactive prompt, only) |
| `installer.chartSource.chartPath`     | `CHART_PATH`         | `installer.chartSource.kind` isn't `path` (checked immediately, in every mode — there's no prompt fallback for this one) |

The username/url/password checks above only hard-fail in `--non-interactive` mode (there's
no prompt to fall back on there); interactively, a missing value just falls through to the
existing prompt.

### Optional fields

Have working defaults:

| Config key                           | Default / behavior when omitted                     |
|----------------------------------------|-------------------------------------------------------|
| `installer.registry.secret.server`     | `https://index.docker.io/v1/`                        |
| `installer.registry.secret.email`      | unset                                                 |
| `installer.chartSource.kind`           | `repo` (published chart)                             |
| `installer.chartSource.chartVersion`   | latest                                                |
| `installer.kubeContext`                | current local kubeconfig context                     |
| `installer.externalHostAddress`        | `auto` — existing autodetect (minikube/docker-desktop/node IP) |
| `installer.versions.mlrun`             | chart default (no override)                          |
| `installer.versions.nuclio`            | chart default (no override)                          |
| `installer.components.monitoring`      | `true` — enabled                                     |
| `installer.components.spark`           | `true` — enabled                                     |
| `installer.components.mpi`             | `true` — enabled                                     |
| `installer.components.modelMonitoring` | `true` — enabled                                     |
| `installer.otel.operator`              | `false` — disabled (chart default)                   |
| `installer.otel.collector`             | `false` — disabled (chart default)                   |
| `installer.otel.namespaceLabel`        | `false` — disabled (chart default)                   |
| `installer.otel.instrumentation`       | `false` — disabled (chart default)                   |

`installer.components.{monitoring,spark,mpi,modelMonitoring}` mirror the existing
`--disable-*` **flags** exactly (`false` maps to the same `--set` as the matching flag;
a flag/env-set `DISABLE_*` is never un-set by the file — there's no "explicitly
re-enable" flag to begin with, so the file can only ever add a disable, never remove
one). `installer.otel.*` is the reverse: the chart ships all 4 OpenTelemetry values
**disabled** by default, so `true` on any of these keys mirrors the matching
`--enable-otel-*`/`ENABLE_OTEL_*` flag instead — opt-IN, not opt-out — and a
flag/env-set `ENABLE_OTEL_*=true` is never un-set by the file the same way. The 4 are
independent (not one bundled toggle): `operator` installs the operator subchart
(CRDs/webhook/manager); `collector` deploys the Collector (receives OTLP, exports to
Prometheus); `instrumentation` creates the Instrumentation CR (propagators, sampler,
Python/Java agent images); `namespaceLabel` labels/annotates the release namespace so
every Python pod in it is auto-instrumented once `instrumentation` is also on — this
one has a namespace-wide blast radius, review before enabling. `--enable-otel [MODE]`
(`off`/`collector`/`full`, bare flag = `full`) is a convenience over the 4 granular
flags/vars — it doesn't replace them, and they still work individually alongside it.
`ingress.*` toggles, and anything that triggers real infra beyond a `--set` (e.g.
`--local-registry` deploying a registry), are **not** implemented — use the existing
`--enable-ingress`/`--local-registry` flags for those. (`--enable-ingress` itself is now
just chart `--set`s plus a warning check — it doesn't install an ingress controller.)

### Combining `--config` with `-f`/`--values`

`--config` and `-f`/`--values` can be used together. `-f` supplies the base values file;
`ce-config.yaml`'s curated fields resolve to `--set` flags exactly as they do when
`--config` is used alone. Nothing extra to configure — helm always applies `--set` after
`--values`, so a config-resolved field wins over the same key in a `-f` file with no merge
logic needed.

Used alone (no `--config`), `-f` keeps its original, fully self-contained behavior: you
supply `global.registry.*`, image tags, everything yourself, `install.sh` sets nothing on
top, and it skips creating the registry secret entirely (the secret named in your values
file must already exist). Add `--config` and that changes: secret creation and registry/
host resolution run exactly as they do in `--config`-only mode, using the config file's
(or flag/env's) registry fields — so `-f` no longer needs to carry the registry secret
itself when `--config` is supplying it.

### Precedence

The full waterfall, highest wins:

**CLI flag > environment variable > `ce-config.yaml` (`installer:` block, resolved to `--set`) > `-f`/`--values` file (raw values) > chart defaults**

Concretely: for any key the curated `installer:` schema covers (registry, chartSource,
versions, components, otel), a flag, env var, or `ce-config.yaml` value always overrides
the same key in a `-f` file or the chart's own default — because it's applied as `--set`,
and helm applies `--set` after `--values`. For anything **outside** that curated schema
(arbitrary chart values — resource limits, replica counts, etc.), `-f` is authoritative;
nothing in `install.sh` touches those keys.

Requires `yq` — but only when `--config`/`CONFIG_FILE` is actually used; installs that
don't use a config file have no new dependency.

```bash
./scripts/install.sh --config scripts/ce-config.yaml.example --dry-run

# Combined with a values file — config's registry/chart-source/component fields still
# resolve as --set, layered on top of my-values.yaml:
./scripts/install.sh --config scripts/ce-config.yaml.example -f my-values.yaml --dry-run
```
