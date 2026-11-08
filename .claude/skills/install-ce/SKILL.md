---
name: install-ce
description: >-
  Guide a user step by step through installing MLRun CE with scripts/install.py — local
  (Docker Desktop, kind, minikube, including Apple Silicon) or a remote cluster (existing
  context, kubeconfig file, or reachable only over SSH) — then choose the registry, secret,
  configuration and clean-up, dry-run, install, and debug a failure. Use when a user asks
  to install, deploy, set up, reinstall or upgrade MLRun CE, or asks why an install failed.
---

# Guided MLRun CE install

Walk the user down the steps below. Detect what you can, ask only what is left, and link
the README section instead of explaining it: [`scripts/README.md`](../../../scripts/README.md)
is the source of truth for every option.

## Rules

1. Say the target context, namespace and release back to the user before the first write.
2. Never ask for the registry password in chat or put it on a command line. The user writes
   it to a file (`REGISTRY_PASSWORD_FILE`), or runs the interactive installer themselves.
3. Never uninstall, `--hard-clean`, or delete PVCs or namespaces without an explicit OK for
   that cluster.
4. Run with `--dry-run` first. You run the installer with `--non-interactive`.

## 1. Detect

```bash
kubectl config current-context; echo "KUBE_CONTEXT=${KUBE_CONTEXT:-<unset>}"
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.status.nodeInfo.architecture} {.status.nodeInfo.kubeletVersion}{"\n"}{end}'
helm list -n mlrun --deployed --failed --pending --superseded --uninstalling   # Helm 4 has no --all
kubectl get pvc -n mlrun
```

Pass `--context`/`--kube-context` once the user names another cluster. A node named
`desktop-control-plane` is Docker Desktop in kind mode, `docker-desktop` is its kubeadm
mode, `kind-*` is plain kind.

## 2. Chart

The published chart is the default. To test this checkout, add `--chart-path
./charts/mlrun-ce` ([Installing this repo's chart](../../../scripts/README.md#installing-this-repos-chart)).
Without a clone, use the pinned `uvx` command ([Getting the installer](../../../scripts/README.md#getting-the-installer)).

## 3. Cluster

**Local.** Docker Desktop: Kubernetes on, at least 10 GB of memory, and
`EXTERNAL_HOST_ADDRESS=localhost` ([Local install on Docker Desktop](../../../scripts/README.md#local-install-on-docker-desktop)).
minikube and kind: the suggested address is usually right ([External host address](../../../scripts/README.md#external-host-address)).
An `arm64` node goes through [Apple Silicon](#apple-silicon) before installing.

**Remote.** Ask how the API is reached:
- a context that already works: `KUBE_CONTEXT=<name>`;
- a kubeconfig file from an admin: `KUBECONFIG=<file>`, then `KUBE_CONTEXT`;
- only over SSH: ask for the jump host, the API server's IP and port, and the CA, client
  certificate and key (or a kubeconfig to take them from), then run the tunnel recipe in
  [Targeting another cluster](../../../scripts/README.md#targeting-another-cluster). Stop it
  afterwards with `pkill -f "ssh -f -N -L 16443"`.

Check with `kubectl --context <ctx> get nodes -o wide`. Then ask how users will reach the UIs:
- NodePorts: `EXTERNAL_HOST_ADDRESS=<a node IP they can reach>` from that output. Firewalls
  often block 30000-32767.
- an ingress controller already in the cluster: `--enable-ingress <class>`, with a nip.io name
  for a bare IP ([Ingress](../../../scripts/README.md#ingress));
- neither: port-forward after the install, through the SSH host if needed.

Cluster checks:
- `kubectl get storageclass` must show a `(default)`. The installer blocks without one;
  suggest local-path, as in `charts/mlrun-ce/README.md`.
- `kubectl auth can-i create customresourcedefinitions`. Without it, an admin first installs
  the cluster-scoped parts with `charts/mlrun-ce/admin_installation_values.yaml`, then the user
  installs with `-f charts/mlrun-ce/non_admin_installation_values.yaml` (or
  `non_admin_cluster_ip_installation_values.yaml`) plus `--config`, so the secret is still
  created ([Precedence](../../../scripts/README.md#precedence)). Not tested with the installer;
  say so.
- One MLRun CE release per cluster ([Known issues](../../../scripts/README.md#known-issues)).
  On a shared cluster, check `helm list -A` for another one.

## 4. Registry and secret

Ask which registry MLRun pushes function images to ([Choosing a registry](../../../scripts/README.md#choosing-a-registry)):

| Answer | Set |
|---|---|
| Docker Hub | `REGISTRY_URL=index.docker.io/<user>`, `REGISTRY_USERNAME=<user>`, token in the password file |
| Another registry | `REGISTRY_SERVER=<host>`, `REGISTRY_URL=<host>/<prefix>`, `REGISTRY_USERNAME` |
| None | `--local-registry`; read [Local registry](../../../scripts/README.md#local-registry) with the user, it needs ingress and a CoreDNS entry for builds to pull |
| A secret already in the namespace | `--skip-secret` (`REGISTRY_SECRET_NAME` if not `registry-credentials`) |

For the password, have the user run `read -s P && printf '%s' "$P" > ~/.ce-registry-pw &&
chmod 600 ~/.ce-registry-pw` in their own terminal, and set `REGISTRY_PASSWORD_FILE` to it.
A re-run reuses the secret the first run created.

## 5. Configuration

- One-off: environment variables and flags.
- Repeatable: copy `scripts/ce-config.yaml.example` to `ce-config.yaml` and fill it in, with no
  password ([From a config file](../../../scripts/README.md#from-a-config-file)).
- Chart values the installer does not manage (resources, replicas): `-f my-values.yaml`
  ([From a values file](../../../scripts/README.md#from-a-values-file)).
- Fewer components on a small cluster: [Optional components](../../../scripts/README.md#optional-components).

## 6. Existing state

From the detect step:
- no release: continue.
- `deployed`: the same command upgrades in place ([Upgrading](../../../scripts/README.md#upgrading)).
- `failed` or `pending-*`: rerunning usually recovers it. If it does not, ask, then
  `./scripts/install.py uninstall`.
- PVCs the user wants gone: `uninstall --hard-clean`, after an explicit OK. With no release in
  the namespace it refuses `--non-interactive`, so the user runs it
  ([Uninstalling](../../../scripts/README.md#uninstalling)).

## 7. Install

```bash
KUBE_CONTEXT=<ctx> REGISTRY_PASSWORD_FILE=~/.ce-registry-pw <other variables> \
  ./scripts/install.py [--chart-path ./charts/mlrun-ce] [--config ce-config.yaml] --non-interactive --dry-run
```

Read the warnings with the user, then run it again without `--dry-run`. A cold install takes
5-15 minutes; raise `HELM_TIMEOUT` on a slow network. Afterwards:

```bash
kubectl -n mlrun port-forward svc/mlrun-api 30070:8080 &
curl -s localhost:30070/api/v1/healthz   # {"version":"…","status":"ok"}
```

Show the access table. If its addresses do not open, port-forward each UI
([After installation](../../../scripts/README.md#after-installation)).

## 8. Debug

On any failure, read before guessing:
- `mlrun-ce-install.log` in the directory the installer ran from (`INSTALL_LOG_FILE`): every
  command, exit code and helm's output. Before quoting it, check it for the user's secrets:
  only rendered Secrets are redacted ([The debug log](../../../scripts/README.md#the-debug-log)).
- `mlrun-ce-values.yaml` (`GENERATED_VALUES_FILE`): what the run resolved.
- `helm status mlrun-ce -n mlrun`, `helm history mlrun-ce -n mlrun`.
- `kubectl get pods -n mlrun`, `kubectl get events -n mlrun --sort-by=.lastTimestamp`, then
  `kubectl describe pod` and `kubectl logs --previous` for the failing pod only.

Then match the symptom:

| Symptom | Next step |
|---|---|
| A pre-install check blocked | The message names it: StorageClass or Helm version |
| `not published` / `No such option` | [Troubleshooting](../../../scripts/README.md#troubleshooting) |
| Dry run: `no matches for kind` | [Why](../../../scripts/README.md#--dry-run-fails-with-no-matches-for-kind); a real install is fine |
| `no match for platform in manifest` | [Apple Silicon](#apple-silicon) |
| `ErrImagePull` with 401/403 | The pull secret: credentials, `REGISTRY_SERVER` |
| Timeout or `ProgressDeadlineExceeded` | Usually a slow pull: rerun, it upgrades in place ([An install failed or timed out](../../../scripts/README.md#an-install-failed-or-timed-out)) |
| `mlrun-api-chief` restarts with `Can't connect to MySQL` | Normal on a first start; wait |
| Hard clean ends with leftovers | [A hard clean left a Kafka pod and its PVC](../../../scripts/README.md#a-hard-clean-left-a-kafka-pod-and-its-pvc) |

Fix the cause, then go back to the dry run. Rule 3 still applies.

## Apple Silicon

MLRun's own images (`mlrun-api`, `mlrun-ui`, `jupyter`, `log-collector`) are published for
amd64 only, so an `arm64` node refuses them with `no match for platform in manifest`. Load
them before installing; they then run under emulation, slowly. A host `docker pull` alone
does not help on a kind node: the node has its own image store, and the tag points at an
index with no arm64 entry. Pull the amd64 image by digest inside the node and tag it, which
the chart's `imagePullPolicy: IfNotPresent` then uses:

```bash
NODE=desktop-control-plane                 # the kind node: its container has the node's name
CHART=./charts/mlrun-ce                    # or: mlrun-ce --repo https://mlrun.github.io/ce --version <v>
for img in $(helm template mlrun-ce $CHART 2>/dev/null | grep -o 'quay.io/mlrun/[^"'"'"' ]*' | sort -u); do
  idx=$(docker manifest inspect "$img" 2>/dev/null) || continue
  echo "$idx" | jq -e '.manifests[]? | select(.platform.architecture=="arm64")' >/dev/null && continue
  d=$(echo "$idx" | jq -r '.manifests[]? | select(.platform.architecture=="amd64") | .digest')
  [ -n "$d" ] || continue
  docker exec "$NODE" crictl pull "${img%:*}@$d"
  docker exec "$NODE" ctr -n k8s.io images tag --force "${img%:*}@$d" "$img"
done
```

In Docker Desktop's kubeadm mode (node `docker-desktop`) the Docker engine is the node's
runtime, so `docker pull --platform linux/amd64 <image>` for each image should be enough.
Not verified yet. If `docker` answers "Sign in to continue using Docker Desktop", the user
has to sign in first. `MLRUN_VERSION` changes the tags, so rerun the loop after changing it.
