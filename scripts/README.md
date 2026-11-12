# MLRun CE Installer

`mlrun-ce-installer` installs the MLRun CE Helm chart on a Kubernetes cluster or your local machine. It checks the
cluster, creates the namespace and the registry pull secret, runs `helm upgrade --install`,
and prints where each service is reachable.

It installs the **published** chart from `https://mlrun.github.io/ce`. 

To install the chart in a clone of this repo instead, pass `--chart-path ./charts/mlrun-ce`
([Installing this repo's chart](#installing-this-repos-chart)).

- [Requirements](#requirements)
- [Local install on Docker Desktop](#local-install-on-docker-desktop)
- [Getting the installer](#getting-the-installer)
- [Commands](#commands)
- [Installing](#installing)
- [Configuration](#configuration)
- [Upgrading](#upgrading)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

---

## Requirements

| Tool | Notes |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh`. Supplies the Python interpreter and the installer's dependencies; nothing to `pip install` |
| [helm](https://helm.sh/docs/intro/install/) | 4.1 or newer, as the MLRun CE docs require; the installer refuses an older Helm |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Pointing at the target cluster |

The cluster needs a default StorageClass, and the MLRun CE docs ask for Kubernetes 1.34 or
newer (the installer warns below it). It also warns when the nodes have less
than 8Gi allocatable memory or ephemeral storage in total. The chart's NodePorts are 30010,
30020, 30040, 30050, 30060, 30070, 30093, 30094, 30100 and 30110; one already taken is
moved to a free port ([Pre-install checks](#pre-install-checks)).

A local install needs Docker, because the local cluster runs on it: Docker Desktop's
Kubernetes, kind and minikube's default driver all do, and so does pushing your own images to
a `--local-registry`. The installer itself only uses the `docker` CLI for a best-effort
registry login check, which reports `skipped` without it, so against a remote cluster or from
inside a pod you don't need Docker.

---

## Local install on Docker Desktop

1. Turn on Kubernetes under **Settings → Kubernetes**, and give Docker at least 10 GB of
   memory under **Settings → Resources**. An idle install uses about 5.5 GB; at 8 GB the
   [pre-install checks](#pre-install-checks) warn and about 2.5 GB is left for functions.
2. `kubectl config use-context docker-desktop`
3. Have a Docker Hub username and access token ready ([Choosing a registry](#choosing-a-registry)).
   `--local-registry` needs the extra steps under [Local registry](#local-registry), so
   Docker Hub is the simpler path.
4. On Apple Silicon, put MLRun's amd64 images on the node first
   ([Apple Silicon](#apple-silicon-arm64)).
5. Run `./scripts/install.py` from a clone, or the `uvx` command under
   [Without a clone](#without-a-clone). With a registry account, export
   `EXTERNAL_HOST_ADDRESS=localhost` (or answer `localhost` at the address prompt): the
   suggested `host.docker.internal` resolves inside the cluster but may not on your machine.
   With `--local-registry --enable-ingress`, keep `host.docker.internal`: the installer
   refuses `localhost` and `127.0.0.1` there, since the registry would be
   `registry.localhost`, which each pod resolves to itself.
6. If the access table's addresses don't open (Docker Desktop's kind-based cluster does not
   publish NodePorts to the host), forward each service you use to its NodePort number,
   then open `http://localhost:<port>`:

   ```bash
   kubectl -n mlrun port-forward svc/mlrun-ui 30060:80 &
   kubectl -n mlrun port-forward svc/mlrun-jupyter 30040:8888 &
   kubectl -n mlrun port-forward svc/nuclio-dashboard 30050:8070 &
   ```

   The API is `svc/mlrun-api 30070:8080` and Grafana `svc/grafana 30010:80`.

### Apple Silicon (arm64)

MLRun publishes `mlrun-api`, `mlrun-ui`, `jupyter`, `log-collector` and `mlrun` (the image
functions run on) for amd64 only. An arm64 node finds no image for its platform, and those
pods fail with `ImagePullBackOff` (`no match for platform in manifest`). The installer warns
about this before it installs, and prints these commands with the version filled in. Set
`MLRUN_VERSION` to the MLRun version of the chart you install, its `appVersion`
(`helm show chart mlrun-ce/mlrun-ce | grep appVersion`), and pull the amd64 images first;
Docker Desktop runs them under emulation:

```bash
MLRUN_VERSION=<appVersion>
for image in mlrun-api mlrun-ui jupyter log-collector mlrun; do
  docker pull --platform linux/amd64 "quay.io/mlrun/$image:$MLRUN_VERSION"
done
```

That is enough when the cluster uses Docker's own images (Docker Desktop's kubeadm cluster).
The kind cluster (node `desktop-control-plane`) keeps a separate image store, and there the
tag still points at a list with no arm64 entry. Pull each amd64 build into the node by
digest and point the tag at it; the chart's `imagePullPolicy: IfNotPresent` then uses it:

```bash
for image in mlrun-api mlrun-ui jupyter log-collector mlrun; do
  ref="quay.io/mlrun/$image:$MLRUN_VERSION"
  digest=$(docker buildx imagetools inspect "$ref" --format \
    '{{range .Manifest.Manifests}}{{if eq .Platform.Architecture "amd64"}}{{.Digest}}{{end}}{{end}}')
  docker exec desktop-control-plane crictl pull "quay.io/mlrun/$image@$digest"
  docker exec desktop-control-plane ctr -n k8s.io images tag --force "quay.io/mlrun/$image@$digest" "$ref"
done
```

Images MLRun builds for your functions start from the amd64 `mlrun` image too, so build them
for amd64 with Kaniko's `--custom-platform` flag, passed through `extra_args`
([MLRun: extra arguments](https://docs.mlrun.org/en/stable/runtimes/image-build.html#extra-arguments)):

```python
func.spec.build.extra_args = "--custom-platform=linux/amd64"
```

---

## Getting the installer

### Without a clone

Pin a release tag from [the releases page](https://github.com/mlrun/ce/releases):

```bash
CE_TAG=mlrun-ce-0.12.1-rc.10
uvx --from "git+https://github.com/mlrun/ce@${CE_TAG}#subdirectory=scripts" \
  mlrun-ce-installer install
```

The installer has no version of its own: the one from tag `mlrun-ce-X` reports version `X`
and installs chart `X` unless told otherwise. If the tag above is not on the releases page
yet, use the newest one listed. Tags older than the installer have no `scripts/` directory,
and `uvx` fails on them. `development` in place of a tag gets the newest installer, but it
changes with every merge.

This path needs access to GitHub and PyPI, and resolves typer, click, rich and pyyaml within
the ranges in `scripts/pyproject.toml`. A clone uses the exact versions pinned in
`scripts/install.py.lock`.

### As a command

```bash
uv tool install "git+https://github.com/mlrun/ce@mlrun-ce-0.12.1-rc.10#subdirectory=scripts"
mlrun-ce-installer version
```

`uv tool upgrade mlrun-ce-installer` updates it; `uv tool uninstall mlrun-ce-installer`
removes it.

### From a clone

```bash
./scripts/install.py
```

`python3 scripts/install.py` also works: without the dependencies it re-runs itself under
uv. To get the `mlrun-ce-installer` command, run `make installer-link` (links into
`~/.local/bin`; set `INSTALLER_BIN_DIR` for another directory). The link points at your
working tree, so edits take effect immediately. `make installer-unlink` removes it.

---

## Commands

```bash
mlrun-ce-installer install [options]     # the default when no command is given
mlrun-ce-installer uninstall [options]
mlrun-ce-installer version
mlrun-ce-installer help
```

Flags without a command mean `install`. `uninstall` is the same as the `--uninstall` flag.
Any other word is rejected, so a typo such as `unistall` cannot start an install. Color is
off when stdout is not a terminal or `NO_COLOR` is set.

The examples below use `./scripts/install.py`; `mlrun-ce-installer` takes the same
arguments.

---

## Installing

### Interactive

```bash
./scripts/install.py
```

You are asked for the registry username, password, server URL and email
([Choosing a registry](#choosing-a-registry)), the
[external host address](#external-host-address), and the registry URL images are pushed to.
Nothing on the cluster changes while you answer. Then the installer shows the target and
asks before its first write:

```
[INFO] About to install:
[INFO]   Cluster:   docker-desktop (https://127.0.0.1:6443)
[INFO]   Namespace: mlrun
[INFO]   Release:   mlrun-ce
[INFO]   Chart:     mlrun-ce/mlrun-ce 0.12.0 (newest stable)
[INFO]   Registry:  index.docker.io/myuser (pull secret 'registry-credentials')
[INFO]   Address:   host.docker.internal
Proceed? [Y/n]:
```

Check the cluster line: it is your kubeconfig's current context, or `--kube-context`.
Answering `n` leaves the cluster untouched (exit 1), and so does Ctrl-C at any prompt
(exit 130).

### Non-interactive (CI)

`--non-interactive`, or `CI=true`, never prompts and skips the confirmation. A missing
required value exits 1 before anything is changed.

```bash
export CI=true
export REGISTRY_USERNAME=myuser
export REGISTRY_PASSWORD_FILE=/run/secrets/registry-password   # or REGISTRY_PASSWORD
export REGISTRY_SERVER=https://index.docker.io/v1/
export REGISTRY_EMAIL=me@example.com
export REGISTRY_URL=index.docker.io/myuser
export EXTERNAL_HOST_ADDRESS=localhost
./scripts/install.py
```

Either way, the password stays out of the environment of the helm, kubectl and docker
processes the installer starts, and reaches `kubectl apply` and `docker login` on stdin,
never as a command-line argument.

### From a config file

A `ce-config.yaml` holds the settings of a run (registry, chart source, components) so every
run is the same. Start from the example for your cluster:

```bash
cp scripts/ce-config.local.yaml.example ce-config.yaml    # Docker Desktop, kind, minikube
cp scripts/ce-config.remote.yaml.example ce-config.yaml   # any other cluster
./scripts/install.py --config ce-config.yaml
```

Interactively, values from the file become the prompts' defaults. With `CI=true` they are
used directly. The password is never read from the file. See
[The config file](#the-config-file).

### From a values file

```bash
./scripts/install.py -f my-values.yaml
```

Used alone, `-f` is passed to helm as is (the installer adds only
`mlrunMigrations.enabled: false`, see [Upgrading](#upgrading)), no pull secret is created,
and no registry or address questions are asked. The file must set `global.registry.*` itself, and the secret
it names must already exist, or pods fail to pull images when helm waits for them. Combined with `--config`, the secret
and the registry settings are handled as usual. See [Precedence](#precedence).

### Choosing a registry

MLRun builds an image for each function you deploy, pushes it to a registry, and the
cluster pulls it from there. The installer stores the registry credentials as a pull
secret, `registry-credentials` (`REGISTRY_SECRET_NAME`).

- **Docker Hub**: your username, an
  [access token](https://docs.docker.com/security/for-developers/access-tokens/) as the
  password, and `index.docker.io/<username>` as the registry URL.
- **Another registry** (GHCR, ECR, Harbor, …): its host as the server URL and the
  repository prefix as the registry URL.
- **No registry account**: `--local-registry` runs one in the cluster
  ([Local registry](#local-registry)).
- **A secret you created yourself**: pass `--skip-secret`. The installer checks that it
  exists and asks for no credentials. A secret with that name that the installer did not
  create is never modified, and `uninstall` leaves it in place.

### Targeting another cluster

```bash
./scripts/install.py --kube-context my-cluster
```

`--kube-context` (or `KUBE_CONTEXT`, or `installer.kubeContext`) selects the kubeconfig
context for every `kubectl` and `helm` call. The installer never connects over SSH itself.
For a cluster reachable only over SSH, open a tunnel and point a context at it:

```bash
ssh -f -N -L 16443:<cluster-api-ip>:6443 user@jump-host
kubectl config set-cluster my-cluster --server=https://localhost:16443 \
  --certificate-authority=<ca.crt> --embed-certs=true
kubectl config set-credentials my-cluster --client-certificate=<client.crt> \
  --client-key=<client.key> --embed-certs=true
kubectl config set-context my-cluster --cluster=my-cluster --user=my-cluster
```

This needs `localhost` in the API server certificate's SANs (kubeadm and rke2 include it).
The tunnel carries only the API port, so set `EXTERNAL_HOST_ADDRESS` to an address that
reaches the nodes.

### Installing this repo's chart

To test a branch or a PR, check it out and point the installer at the chart:

```bash
./scripts/install.py --chart-path ./charts/mlrun-ce --update-dependencies --dry-run
./scripts/install.py --chart-path ./charts/mlrun-ce
```

The chart version is the one in `charts/mlrun-ce/Chart.yaml`; passing `--ce-version` too
is an error.
The chart's dependencies must already be in `charts/` (`make helm-update-dependencies`),
or the run stops before changing anything. `--update-dependencies` fetches the missing ones
with `helm dependency build`, which follows `requirements.lock`; once they are there, an
air-gapped host needs no flag.

### Dry run

```bash
./scripts/install.py --dry-run
```

Runs every check, prints the plan and renders the chart with
`helm upgrade --install --dry-run=server`. Nothing is created. It asks nothing about the
registry: a registry URL or username you pass is used, and without a URL the chart is
rendered with `dry-run.invalid/registry`. Helm reads the cluster (API
versions, CRDs, existing resources) but sends nothing to it, so admission webhooks are not
exercised. A first install on a cluster without the Prometheus Operator CRDs is rendered
without the monitoring stack ([why](#--dry-run-fails-with-no-matches-for-kind)).

The rendered manifests are not printed, since they hold the chart's Secrets; helm's errors
and warnings still are. The run ends by listing the files to review: the
[debug log](#the-debug-log) with the rendered manifests (Secret values redacted),
`mlrun-ce-values.yaml`, and on an upgrade the
release's current values and the values the upgrade changes ([Upgrading](#upgrading)).

### Pre-install checks

These run before anything is created, dry runs included.

- **Blocking**: Helm older than 4.1, and no default StorageClass. Both are checked before
  either fails, so one run reports both.
- **Warnings**: Kubernetes older than 1.34, or than `MIN_K8S_VERSION`
  when set; registry login with the resolved
  credentials, using a throwaway docker config (skipped without docker, with
  `--local-registry`, in a dry run, and when no credentials were given); no IngressClass
  matching `--enable-ingress`; less than 8Gi allocatable memory or ephemeral storage; arm64
  nodes, with the amd64 image pulls to run first ([Apple Silicon](#apple-silicon-arm64)).
- **NodePorts**: a chart NodePort held by a Service this release does not own is moved to
  the next free port, with a warning naming the Service and the new URL. The new port goes
  into `mlrun-ce-values.yaml`, so the printed URLs, Grafana's link and Jupyter's link to
  the MLRun UI all use it. A taken port you set yourself in `-f` stops the run instead. On
  kind, a moved port is reachable only through `kubectl port-forward`, unless the cluster
  maps it.

`--skip-validators` skips all of them, NodePorts included: a taken one then fails the
install. Two refusals are not validators and always apply: an `-f` file that is the
installer's own `GENERATED_VALUES_FILE`, and another MLRun CE release in the namespace
([Upgrading](#upgrading)).

### After installation

The run ends with a table of what the release serves, read from the chart's NOTES:

```
                          MLRun CE - Access URLs
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ SERVICE            ┃ URL                        ┃ CREDENTIALS          ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ Jupyter UI         │ host.docker.internal:30040 │                      │
│ Nuclio UI          │ host.docker.internal:30050 │                      │
│ MLRun UI           │ host.docker.internal:30060 │                      │
│ MLRun API          │ host.docker.internal:30070 │                      │
│ SeaweedFS S3 API   │ host.docker.internal:30094 │                      │
│ SeaweedFS Admin UI │ host.docker.internal:30093 │ seaweed / seaweed123 │
│ Grafana UI         │ host.docker.internal:30010 │                      │
│ Prometheus UI      │ host.docker.internal:30020 │                      │
│ TimescaleDB        │ host.docker.internal:30110 │ postgres             │
└────────────────────┴────────────────────────────┴──────────────────────┘
```

Each address is the [external host address](#external-host-address) and the service's
NodePort; on Docker Desktop see [Local install on Docker Desktop](#local-install-on-docker-desktop).
Open the addresses with `http://`. Disabled components are left out, and with
`--enable-ingress` each UI shows its ingress host.

---

## Configuration

### Precedence

Highest first:

**flag > environment variable > `ce-config.yaml` > `-f` values file > chart defaults**

Everything the installer resolves is written to `mlrun-ce-values.yaml`
(`GENERATED_VALUES_FILE`) and passed to helm as the last `--values`, after your `-f`, so it
wins for the keys it sets. Ingress hosts and classes are the exception: one set in your `-f`
file is kept. Chart values the installer does not manage (resources, replicas, …) come only
from your `-f` file.

If that file is already there and this run would change it, the installer lists what
differs and asks: **merge** keeps the keys only the old file sets (a value you added by
hand, say) and takes this run's value for the rest; **override** (the default) drops them;
**cancel** stops with nothing changed. Merged keys can be stale, an old local registry's
insecure flags for example, so check the list before merging. Either way the old file is
kept as `mlrun-ce-values.yaml.bak`, and `--non-interactive` overrides.

### The config file

`--config FILE` (or `CONFIG_FILE`) reads the `installer:` block of a YAML file. Every flag
has a key except the per-run actions (`--uninstall`, `--hard-clean`, `--dry-run`), and so
does every environment variable except the registry password. Both examples,
[`ce-config.local.yaml.example`](ce-config.local.yaml.example) and
[`ce-config.remote.yaml.example`](ce-config.remote.yaml.example), list every key with the
flag or variable it stands for.

- **The file is checked before anything runs.** A key the installer does not know, one in
  the wrong block, or a value of the wrong type stops the run with every problem listed and
  where each key belongs:

  ```text
  [ERROR] ce-config.yaml does not match the installer's config format:
  [ERROR]   - installer.secret: not allowed here; it belongs under installer.registry
  [ERROR]   - installer.components.sprak: unknown key; did you mean spark?
  ```

  Quote versions (`minK8sVersion: "1.30"`): YAML reads an unquoted `1.30` as the number 1.3.
- **A toggle** (`skipSecret`, `components.*`, `otel.*` and the rest) set by a flag or a
  variable is never turned back by the file.
- **Required in non-interactive runs**, unless the matching variable is set:
  `registry.url` (not with `--local-registry`) and `registry.secret.username` (not with
  `--skip-secret` or `--local-registry`). Missing fields are reported together.
- **The password** is never read from the file. A `password` key is ignored with a
  warning. Use `REGISTRY_PASSWORD`, `REGISTRY_PASSWORD_FILE` (`registry.passwordFile`) or
  the prompt.
- **`chartSource.chartPath`**, when set, installs that chart directory instead of the
  published chart, like `--chart-path`. Setting `chartVersion` as well is an error. A
  `--ce-version` or `--chart-path` (or its variable) overrides both keys, and the run says
  which key it ignored.
- **`components.*`** set to `false` disables a component, like the matching `--disable-*`
  flag. Set to `true`, it turns the component on over a `-f` file and over an earlier run's
  `--disable-*` kept by a merge, but not over a flag. Left out, the chart decides.
- **`otel.*`** set to `true` enables a piece, like the matching `--enable-otel-*` flag. Any
  `--enable-otel*` flag on the command line makes the file's `otel` block ignored.

### Optional components

```bash
./scripts/install.py --disable-system-monitoring --disable-spark --disable-mpi \
  --disable-model-monitoring
```

They turn off, respectively, Prometheus and Grafana, the Spark operator, the MPI operator,
and model monitoring's Kafka and TimescaleDB.

### Ingress

```bash
./scripts/install.py --enable-ingress           # IngressClass "nginx"
./scripts/install.py --enable-ingress traefik   # another class
```

Turns on the chart's Ingresses. The installer does not install an ingress controller: one
providing the class must already run in the cluster, otherwise the checks warn and the
Ingresses do nothing until one exists. Each gets a host under the
[external host address](#external-host-address): `mlrun.<host>`, `mlrun-api.<host>`,
`jupyter.<host>` and `nuclio.<host>`. These names must resolve to the controller, through
wildcard DNS for a domain, or a name such as `203.0.113.10.nip.io` for a bare IP. With a
bare IP the installer prints the `/etc/hosts` line to add. A host or class you set in an
`-f` file is kept.

If the cluster has no controller (`kubectl get ingressclass` is empty), install one first.
For example ingress-nginx, which provides the default class `nginx`:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace --wait
kubectl -n ingress-nginx get svc ingress-nginx-controller   # EXTERNAL-IP is localhost on Docker Desktop
```

Any controller works: pass its class to `--enable-ingress`. The names above, such as
`mlrun.host.docker.internal`, must also resolve on your machine for a browser to open them.
On a Mac, `host.docker.internal` resolves but its subdomains do not, so add them to
`/etc/hosts` as `127.0.0.1` (Docker Desktop) or use wildcard DNS.

### Local registry

```bash
./scripts/install.py --local-registry
./scripts/install.py --local-registry --enable-ingress
```

Runs `registry:2.8.3` in the namespace, with images on a 10Gi PVC (`local-registry`, default
StorageClass), and points MLRun at it over plain HTTP: both image builders, Nuclio's and
MLRun's (`MLRUN_HTTPDB__BUILDER__INSECURE_PULL_REGISTRY_MODE` and `..._PUSH_...` set to
`enabled` on the MLRun API), skip TLS for it. No registry account or pull secret
credentials are needed.

Without ingress the registry is `local-registry.<namespace>.svc.cluster.local:5000`. Builds
push there, but most nodes cannot resolve that name, so function pods then fail to pull.
Add `--enable-ingress` for a name the nodes can resolve, or use a regular registry instead.

With ingress it is `registry.<host>` (for example `registry.host.docker.internal`). It needs
an ingress controller ([Ingress](#ingress)) and three name-resolution steps, because two
different resolvers are involved. The installer prints the CoreDNS entry, the `/etc/hosts`
line and the `insecure-registries` setting, and edits none of them itself.

| Who resolves the name | Used for | Needs |
|---|---|---|
| Pods, through CoreDNS | the builder pushing the image | [1. CoreDNS entry](#1-coredns-entry) |
| The node's container runtime (Docker Desktop: the Docker daemon, which uses the Mac's resolver) | pulling the image to start the function pod | [2. `/etc/hosts`](#2-etchosts-docker-desktop) and [3. Insecure registry](#3-insecure-registry) |

#### 1. CoreDNS entry

```
[WARN] Pods cannot resolve registry.host.docker.internal until CoreDNS knows about it.
[WARN]   Add this to the hosts{} block of the CoreDNS Corefile:
[WARN]       10.96.0.10 registry.host.docker.internal
[WARN]   Then: kubectl -n kube-system edit configmap coredns
[WARN]         kubectl -n kube-system rollout restart deployment/coredns
```

The IP is your ingress controller Service's ClusterIP, looked up in `ingress-nginx`, then
in the release namespace. For a controller elsewhere, name it:
`INGRESS_CONTROLLER_SERVICE=kube-system/traefik`.

If the Corefile has no `hosts` block, add one after the `kubernetes` block. Keep
`fallthrough`: without it every other name that reaches the plugin gets an empty answer and
cluster DNS breaks.

```
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        hosts {
           <controller ClusterIP> registry.host.docker.internal
           fallthrough
        }
```

Check it from a pod:

```bash
kubectl run dnstest --rm -it --restart=Never --image=busybox -- nslookup registry.host.docker.internal
```

A `nip.io` or `sslip.io` host (`registry.203.0.113.10.nip.io`) needs no entry: CoreDNS
forwards it to public DNS, which answers with the IP in the name, so the installer prints no
warning. A cluster without outside DNS still needs the entry.

#### 2. `/etc/hosts` (Docker Desktop)

Docker Desktop resolves `host.docker.internal` itself, but not `registry.host.docker.internal`.
Without a hosts entry the pull fails with `lookup registry.host.docker.internal: no such host`.
With Docker Desktop's `docker-desktop` (kubeadm) cluster, the ingress controller's
LoadBalancer listens on the Mac's `localhost:80`:

```bash
echo "127.0.0.1 registry.host.docker.internal" | sudo tee -a /etc/hosts
```

#### 3. Insecure registry

The registry serves plain HTTP. Add its host to `insecure-registries` under
**Settings → Docker Engine**, then **Apply & Restart** (the cluster is down for a minute):

```json
{ "insecure-registries": ["registry.host.docker.internal"] }
```

### OpenTelemetry

```bash
./scripts/install.py --enable-otel collector   # operator + collector: a metrics pipeline
./scripts/install.py --enable-otel             # full: all four pieces
```

The four pieces are also flags of their own, applied left to right with `--enable-otel`:
`--enable-otel-operator` (the operator: CRDs, webhook, manager), `--enable-otel-collector`
(a Collector exporting to Prometheus), `--enable-otel-instrumentation` (the Instrumentation
resource) and `--enable-otel-namespace-label`, which auto-instruments every Python pod in
the namespace once instrumentation is on. `--enable-otel off` turns all four off.

### External host address

The address you reach the cluster's NodePorts at, and the base of the ingress hosts. The
installer suggests one from the targeted context (`--kube-context`, or the current context):

1. A minikube context: `minikube ip -p <context>`.
2. `docker-desktop`: `host.docker.internal`, which pods resolve but the Mac may not. When
   it doesn't, the installer prints the `/etc/hosts` line to add
   ([Local install on Docker Desktop](#local-install-on-docker-desktop)).
3. Any other context named by `--kube-context`: the first node's internal IP.
4. Anything else (kind, k3d, …): `localhost`.

Set `EXTERNAL_HOST_ADDRESS` (or `installer.externalHostAddress`) when the suggestion is wrong.

---

## Upgrading

Run the same command again: the installer always runs `helm upgrade --install`, so a re-run
upgrades the release in place. Use it to change a setting, move to another chart with
`--ce-version`, or retry an install that timed out.

- **No unasked downgrade.** Without `--ce-version` the upgrade takes the newest stable
  chart, which is older than a release installed from an rc. The installer then asks
  (default no), and under `--non-interactive` stops: `--ce-version <installed>` keeps the
  release's chart, `--devel` takes the newest prerelease.
- A re-run offers to reuse the pull secret the first run created, so the password is not
  needed again. Credentials passed in the environment replace it.
- The release keeps its NodePorts, including ports moved on an earlier run, unless `-f`
  sets them. This applies with `--skip-validators` too.
- Before upgrading, the installer saves the release's current values to
  `mlrun-ce-values.previous.yaml` (readable by you only; it may hold secrets you set). Every
  value the upgrade changes or resets to the chart default is printed as a warning and
  written to `mlrun-ce-values.changes.yaml`, with secret-like values redacted. To keep a
  value, pass it again.
- **A registry change does not move existing functions.** Each function stores the registry
  it was created with (`spec.image`, and for Nuclio functions `spec.build.registry` and
  `spec.runRegistry`), and a redeploy reuses it. After changing the registry (adding
  `--enable-ingress` to a `--local-registry` install, for example), create those functions
  again. The upgrade warns when the registry changes.
- **MLRun DB migrations** run only when asked: `--trigger-migrations` (or
  `TRIGGER_MIGRATIONS`, or `triggerMigrations` in the config file) runs the migrations Job
  with this upgrade. `mlrun-ce-values.yaml` always sets `mlrunMigrations.enabled: false`,
  so the request does not outlive the run, and a later plain `helm upgrade` with that file
  does not run them again. On a first install there is nothing to migrate, and the request
  is ignored with a warning.
- Keep `mlrun-ce-values.yaml` if you upgrade with plain helm. The run prints the
  equivalent helm commands; a `helm upgrade` without that file (and without
  `--reuse-values`) resets those values to the chart defaults.
- Any other MLRun CE release in the cluster (`my-mlrun` from the chart README, say, or one
  in another namespace) is refused rather than installed beside it. Set `NAMESPACE` and
  `RELEASE_NAME` to its namespace and name to upgrade it. A user who can't list other
  namespaces gets a warning, and only their namespace is checked.
- Kafka can crash-loop after upgrading a release whose Kafka volume predates the pinned
  `kafka.clusterId`, with `Invalid cluster.id in: .../meta.properties`. Delete the Kafka
  PVC (`data-kafka-stream-kafka-stream-pool-<n>`) and its pod; Strimzi recreates them. Only
  model-monitoring stream data is lost.

---

## Uninstalling

```bash
./scripts/install.py uninstall                 # keeps data
./scripts/install.py uninstall --hard-clean    # deletes data
./scripts/install.py uninstall --dry-run       # shows what would be removed
```

`uninstall` removes the helm release, then the objects the installer created outside the
chart: the pull secret and the local registry's Deployment, Service and Ingress. It finds
those by the `app.kubernetes.io/managed-by=mlrun-ce-installer` label, so a secret you
created yourself stays. It uses `HELM_TIMEOUT` (default `960s`).

**Kept:** the namespace, the chart's CRDs and every PVC (the registry's included), so a
reinstall finds its data. SeaweedFS's `seaweedfs-admin-secret`, `seaweedfs-s3-secret` and
`seaweedfs-sftp-secret` are kept on purpose so a reinstall reuses the keys, and the Spark
operator's `spark-operator-webhook-certs` has no release label. The run ends by naming
it and any leftover Nuclio image build Jobs (`nuclio-buildjob-*`), with the command that
deletes them.

**`--hard-clean`** also deletes every PVC in the namespace and the PVs bound to them. This
is irreversible. It first deletes the namespace's Nuclio functions and API gateways, while
Nuclio still runs to remove their Deployments and Services, and after the uninstall the
pods of MLRun runs and image builds (labelled `mlrun/class` or `mlrun/function`). When
there are any it lists them and asks first; `--non-interactive` deletes them. A plain
`uninstall` leaves both running. It also deletes the chart's Strimzi `Kafka` and
`KafkaNodePool`, which the chart creates as helm hooks and `helm uninstall` never deletes,
and waits for their broker pod to go; a plain `uninstall` leaves them and says so. The
certificate and build Jobs above go too, without a question. With no
release in the namespace it asks you to type the namespace name, and refuses under
`--non-interactive`.

The namespace itself is kept, since it may be yours. At the end, a hard clean lists what is
still in it, naming in full anything MLRun CE did not create, and asks whether to delete it
too (default no); `--delete-namespace` deletes it
without asking, and is the only way under `--non-interactive`.

Unless the namespace went, the run ends by listing anything still labelled with the release
that helm no longer tracks (a migrations Job from a failed upgrade, for example), with the
`kubectl delete` command for it. It deletes none of them.

---

## Troubleshooting

### The debug log

Every run writes `mlrun-ce-install.log` in the current directory (`INSTALL_LOG_FILE`): each
command with its exit code, timing and output, helm's full output, and the traceback if the
installer crashed. A failed run prints its path. The file is readable by you only and never
contains the registry password, nor the current release's values an upgrade reads. Values under `data:`/`stringData:` of rendered Secrets are
replaced with `<redacted>`, but values the chart puts in ConfigMaps, pod `env:` or its NOTES
are not, so check for your own before sharing the log.

`--log-level warning` (`LOG_LEVEL`, or `logLevel` in the config file) keeps the terminal to
warnings, errors, prompts and how the run ended (the access table, or where a dry run left
its files); the `[INFO]` lines and helm's standard output go to this log only.

### An install failed or timed out

```
[ERROR] Helm installation failed (exit code 1).
[INFO] To see what did not come up:
[INFO]   kubectl get pods -n mlrun
[INFO]   kubectl get events -n mlrun --sort-by=.lastTimestamp
[INFO] A timeout (HELM_TIMEOUT=960s) is often a slow first image pull; running the same command again upgrades the release in place.
[INFO] Common failures and their fixes: https://github.com/mlrun/ce/blob/development/scripts/README.md#troubleshooting
Full debug log: /home/me/mlrun-ce-install.log
```

A cold pull of the Jupyter image alone can take several minutes. Raise `HELM_TIMEOUT` on a
slow network, or run the same command again. If a pod cannot pull images, check the pull
secret: with `-f` alone the secret your file names must exist.

### `--dry-run` fails with `no matches for kind`

```
Error: unable to build kubernetes objects from release manifest: [resource mapping not found
for name: "monitoring-prometheus" namespace: "mlrun" from "": no matches for kind
"Prometheus" in version "monitoring.coreos.com/v1"
ensure CRDs are installed first, ...
```

A server dry run cannot render objects whose CRDs the chart itself installs, because they do
not exist before the first install. A real install is unaffected. The installer leaves the
monitoring stack out of a first install's dry run for this reason. If the error names
another kind (the OpenTelemetry operator's, with `--enable-otel`), dry-run without that
component, or after the first install.

### "No such option" (exit 2)

A flag is misspelled. This is an error, not a warning: a misspelled `--dry-run` would
otherwise run a real install, and a misspelled `--skip-secret` would replace your pull
secret. Nothing has touched the cluster. An
option value cannot start with a dash: in `--enable-ingress -f values.yaml`, `-f` is the
next option, not the ingress class.

### Ingress URLs do not resolve

Either no ingress controller provides the class (the checks warned about it), or the hosts
do not resolve to the controller. See [Ingress](#ingress). Once a controller is running, the
existing Ingresses start working without a reinstall.

### Function pod: `ImagePullBackOff` with `--local-registry`

The event names the registry the node tried. See [Local registry](#local-registry):

- `local-registry.<namespace>.svc.cluster.local:5000` and `no such host`: the install has no
  `--enable-ingress`, or the function was created before it did. Reinstall with
  `--enable-ingress`, then delete and redeploy the function.
- `registry.<host>` and `no such host`: the `/etc/hosts` entry (step 2) is missing.
- `registry.<host>` and an `https://` request (port 443) failing: the host is not in
  Docker's `insecure-registries` (step 3).
- Pushes fail from the builder pod: the CoreDNS entry (step 1) is missing.

### `ImagePullBackOff`: `no match for platform in manifest`

An MLRun image on an arm64 node; MLRun publishes it for amd64 only. See
[Apple Silicon](#apple-silicon-arm64), then delete the pod so it starts again.

### `--local-registry` refuses to take over existing objects

An object named `local-registry` (PVC, Deployment, Service or Ingress) exists in the
namespace without the installer's label. Taking it over would let the next `uninstall`
delete something that is not the installer's. Delete it, install into another namespace, or
use that registry through `REGISTRY_URL` instead of `--local-registry`.

### A hard clean left a Kafka pod and its PVC

The run ends with "Hard clean finished with leftovers" and names a Kafka PVC as
`Terminating`. Strimzi's Kafka resources are created by helm hooks, which `helm uninstall`
does not delete, so the broker pod outlives the operator and keeps its PVC. A hard clean
deletes them, but an older installer or a plain `uninstall` left them. Delete them:

```bash
kubectl delete strimzipodset --all -n mlrun
kubectl delete pod -l strimzi.io/cluster -n mlrun --force --grace-period=0
kubectl delete kafka,kafkanodepool --all -n mlrun
```

The PVC then goes on its own. Check what is left:

```bash
kubectl get pvc -n mlrun
kubectl get pv | grep mlrun/
```

If a PVC or a `Released` PV remains, run `./scripts/install.py uninstall --hard-clean`
again **interactively** and type the namespace when asked. Under `--non-interactive` it
[refuses](#--hard-clean-refused-release-is-not-in-namespace), since the release is gone.

### `--hard-clean` refused: release is not in namespace

A hard clean deletes every PVC in the namespace, so with no release there to confirm it is
the right one, it asks you to type the namespace name, and refuses under
`--non-interactive`. Check `NAMESPACE` and `--kube-context`, or run interactively.

### Known issues

- **Helm 4 prints `Conflict: cannot merge map onto non-map for registry. Skipping.`** It
  is harmless; the registry values still reach MLRun and Nuclio.
- **The OpenTelemetry collector shows `0/1` right after a successful install.** The
  operator creates it after helm has finished waiting; it becomes ready a minute or so
  later.
- **`mlrun-api-chief` restarts once or twice on a fresh install** while `mlrun-db` starts
  (`Can't connect to MySQL server on 'mlrun-db'`). It recovers by itself.
- **One MLRun CE release per cluster.** The chart's `workflow-controller` PriorityClass is
  cluster-scoped, so a second release fails even in another namespace. The installer
  refuses one up front (see [Upgrading](#upgrading)).

---

## Reference

### Flags

Each flag's environment variable is set to `true` or `1` for the same effect; anything
else, `false` and `0` included, leaves it off.

| Flag | Variable | Effect |
|---|---|---|
| `-h`, `--help` | | Show help |
| `-v`, `--version` | | Print the installer version (the chart version it ships with) |
| `--uninstall` | `UNINSTALL` | Same as the `uninstall` command |
| `--hard-clean` | `HARD_CLEAN` | With `uninstall`: also delete the namespace's PVCs and PVs, Nuclio functions, MLRun run pods and the chart's Kafka |
| `--delete-namespace` | `DELETE_NAMESPACE` | With `--hard-clean`: also delete the namespace, without asking |
| `--dry-run` | `DRY_RUN` | Change nothing: render the chart with `--dry-run=server`, or list what `uninstall` would remove |
| `--non-interactive` | `NON_INTERACTIVE`, `CI` | Never prompt; exit 1 on a missing required value |
| `--config FILE` | `CONFIG_FILE` | Read defaults from a `ce-config.yaml` |
| `-f`, `--values FILE` | | Base values file; see [From a values file](#from-a-values-file) |
| `--chart-path DIR` | `CHART_PATH` | Install a local chart directory instead of the published chart; not with `--ce-version` |
| `--update-dependencies` | `UPDATE_DEPENDENCIES` | With `--chart-path`: fetch the chart dependencies `charts/` lacks |
| `--ce-version VERSION` | `CE_VERSION` | Install this published chart version (default: the newest stable one); not with `--chart-path` |
| `--devel` | `DEVEL` | Without `--ce-version`: the newest chart, prereleases (rc) included |
| `--skip-secret` | `SKIP_REGISTRY_SECRET` | Use an existing pull secret instead of creating one |
| `--skip-validators` | `SKIP_VALIDATORS` | Skip the [pre-install checks](#pre-install-checks) |
| `--local-registry` | `LOCAL_REGISTRY` | Run a registry in the cluster |
| `--kube-context` | `KUBE_CONTEXT` | kubeconfig context for kubectl and helm (default: the current one) |
| `--enable-ingress [CLASS]` | `ENABLE_INGRESS`, `INGRESS_CLASS` | Turn on the chart's Ingresses (class `nginx` by default) |
| `--enable-otel [MODE]` | | `off`, `collector` or `full` (the default) |
| `--enable-otel-operator` | `ENABLE_OTEL_OPERATOR` | Install the OpenTelemetry operator |
| `--enable-otel-collector` | `ENABLE_OTEL_COLLECTOR` | Deploy the OpenTelemetry Collector |
| `--enable-otel-instrumentation` | `ENABLE_OTEL_INSTRUMENTATION` | Create the Instrumentation resource |
| `--enable-otel-namespace-label` | `ENABLE_OTEL_NAMESPACE_LABEL` | Auto-instrument every Python pod in the namespace |
| `--disable-system-monitoring` | `DISABLE_SYSTEM_MONITORING` | Leave out Prometheus and Grafana |
| `--disable-spark` | `DISABLE_SPARK` | Leave out the Spark operator |
| `--disable-mpi` | `DISABLE_MPI` | Leave out the MPI operator |
| `--disable-model-monitoring` | `DISABLE_MODEL_MONITORING` | Leave out Kafka and TimescaleDB |
| `--show-progress` | `SHOW_PROGRESS` | Show a live table of rollouts while helm runs (terminals only) |
| `--log-level LEVEL` | `LOG_LEVEL` | `info` (the default) or `warning`; see [the debug log](#the-debug-log) |
| `--trigger-migrations` | `TRIGGER_MIGRATIONS` | On an upgrade, run the MLRun DB migrations; see [Upgrading](#upgrading) |

### Environment variables

These have no flag. An empty value counts as unset.

| Variable | Default | Meaning |
|---|---|---|
| `NAMESPACE` | `mlrun` | Target namespace |
| `RELEASE_NAME` | `mlrun-ce` | Helm release name |
| `REGISTRY_USERNAME` | | Registry username |
| `REGISTRY_PASSWORD` | | Registry password |
| `REGISTRY_PASSWORD_FILE` | | File holding only the password; `REGISTRY_PASSWORD` wins if both are set |
| `REGISTRY_SERVER` | `https://index.docker.io/v1/` | Registry server for the pull secret |
| `REGISTRY_EMAIL` | | Registry email (optional) |
| `REGISTRY_URL` | | Where images are pushed, e.g. `index.docker.io/myuser` |
| `REGISTRY_SECRET_NAME` | `registry-credentials` | Pull secret name |
| `EXTERNAL_HOST_ADDRESS` | suggested | See [External host address](#external-host-address) |
| `INGRESS_CONTROLLER_SERVICE` | | `namespace/name` of the ingress controller Service, for the local registry's CoreDNS entry |
| `MLRUN_VERSION` | chart's | MLRun API and UI image tag |
| `NUCLIO_VERSION` | chart's | Nuclio controller and dashboard image tag |
| `HELM_REPO_URL` | `https://mlrun.github.io/ce` | Chart repository |
| `HELM_TIMEOUT` | `960s` | helm's wait timeout for install, upgrade and uninstall |
| `MIN_K8S_VERSION` | | Also warn below this Kubernetes version (the 1.34 warning always applies) |
| `PROGRESS_INTERVAL_SEC` | `10` | Refresh interval of `--show-progress` |
| `GENERATED_VALUES_FILE` | `mlrun-ce-values.yaml` | Where the installer writes its values; rewritten every run. An upgrade's `.previous.yaml` and `.changes.yaml` go beside it |
| `INSTALL_LOG_FILE` | `mlrun-ce-install.log` | The [debug log](#the-debug-log); rewritten every run |
