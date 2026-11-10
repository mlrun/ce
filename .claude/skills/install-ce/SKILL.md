---
name: install-ce
description: >-
  Guide a user step by step through installing MLRun CE with scripts/install.py — local
  (Docker Desktop, kind, minikube) or a remote cluster (existing
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
test -f scripts/install.py && echo clone || echo "no clone"
```

Once the user names another cluster, pass it to every tool: `--context <ctx>` to kubectl,
`--kube-context <ctx>` to helm and to the installer (or `KUBE_CONTEXT=<ctx>` for the
installer alone). A node named
`desktop-control-plane` is Docker Desktop in kind mode, `docker-desktop` is its kubeadm
mode, `kind-*` is plain kind.

## 2. Installer and chart

Below, `ce` is the installer. Both forms take the same arguments; define it in the same
command you run it in if your shell keeps no state between calls:

```bash
ce() { ./scripts/install.py "$@"; }                    # clone
ce() { uvx --from "git+https://github.com/mlrun/ce@${CE_TAG}#subdirectory=scripts" mlrun-ce-installer "$@"; }   # no clone
```

`CE_TAG` is a tag from the releases page ([Getting the installer](../../../scripts/README.md#getting-the-installer)).
The published chart is the default. In a clone, `--chart-path ./charts/mlrun-ce` tests this
checkout instead ([Installing this repo's chart](../../../scripts/README.md#installing-this-repos-chart));
add `--update-dependencies` unless `charts/mlrun-ce/charts/` is already filled.

## 3. Cluster

**Local.** Docker Desktop: Kubernetes on, at least 10 GB of memory, and
`EXTERNAL_HOST_ADDRESS=localhost` with a registry account, or `host.docker.internal` with
`--local-registry --enable-ingress` ([Local install on Docker Desktop](../../../scripts/README.md#local-install-on-docker-desktop)).
minikube and kind: the suggested address is usually right ([External host address](../../../scripts/README.md#external-host-address)).

**Remote.** Ask how the API is reached:
- a context that already works: `--kube-context <name>`;
- a kubeconfig file from an admin: `KUBECONFIG=<file>`, then `--kube-context`;
- only over SSH: ask for the jump host, the API server's IP and port, and the CA, client
  certificate and key (or a kubeconfig to take them from), then run the tunnel recipe in
  [Targeting another cluster](../../../scripts/README.md#targeting-another-cluster). Stop it
  afterwards with `pkill -f -- "-L 16443:"`.

Check with `kubectl --context <ctx> get nodes -o wide`. Then ask how users will reach the UIs:
- NodePorts: `EXTERNAL_HOST_ADDRESS=<a node IP they can reach>` from that output. Firewalls
  often block 30000-32767.
- an ingress controller already in the cluster: `--enable-ingress <class>`, with a nip.io name
  for a bare IP ([Ingress](../../../scripts/README.md#ingress));
- neither (common for lab clusters behind a jump host): `kubectl port-forward` after the
  install. It goes through the API server, so it works over the SSH tunnel too.

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
| None | `--local-registry --enable-ingress`; see the prerequisites below |
| A secret already in the namespace | `--skip-secret` (`REGISTRY_SECRET_NAME` if not `registry-credentials`) |

`--local-registry` needs, before function builds work ([Local registry](../../../scripts/README.md#local-registry)):
- an ingress controller in the cluster ([Ingress](../../../scripts/README.md#ingress));
- a CoreDNS entry for `registry.<address>`, so the builder can push (not for a `nip.io` or
  `sslip.io` address, which public DNS resolves);
- an `/etc/hosts` line for it on the user's machine, which Docker Desktop's nodes pull through;
- the host in Docker's `insecure-registries`, because the registry serves plain HTTP;
- on Docker Desktop, `EXTERNAL_HOST_ADDRESS=host.docker.internal`, not `localhost`.

The installer prints each entry and edits none of them. CoreDNS is shared by the whole
cluster and `/etc/hosts` needs sudo: show the user the command and ask before running either.

For the password, have the user run `read -s P && printf '%s' "$P" > ~/.ce-registry-pw &&
chmod 600 ~/.ce-registry-pw` in their own terminal, and set `REGISTRY_PASSWORD_FILE` to it.
A re-run reuses the secret the first run created.

## 5. Configuration

- One-off: environment variables and flags.
- Repeatable: copy `scripts/ce-config.local.yaml.example` (a local cluster) or
  `scripts/ce-config.remote.yaml.example` (a remote one), in the clone or from the same tag on
  GitHub, to `ce-config.yaml` and fill it in, with no password. A key the installer does not
  know stops the run and names where it belongs ([From a config file](../../../scripts/README.md#from-a-config-file)).
- Chart values the installer does not manage (resources, replicas): `-f my-values.yaml`
  ([From a values file](../../../scripts/README.md#from-a-values-file)).
- Fewer components on a small cluster: [Optional components](../../../scripts/README.md#optional-components).

## 6. Existing state

From the detect step:
- no release: continue.
- `deployed`: the same command upgrades in place ([Upgrading](../../../scripts/README.md#upgrading)).
- `failed` or `pending-*`: rerunning usually recovers it. If it does not, ask, then
  `ce uninstall`.
- PVCs the user wants gone: `uninstall --hard-clean`, after an explicit OK. With no release in
  the namespace it refuses `--non-interactive`, so the user runs it
  ([Uninstalling](../../../scripts/README.md#uninstalling)).

## 7. Install

```bash
REGISTRY_PASSWORD_FILE=~/.ce-registry-pw <other variables> \
  ce --kube-context <ctx> [--chart-path ./charts/mlrun-ce] [--config ce-config.yaml] --non-interactive --dry-run
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
| `not published` | `--ce-version` names a chart the repo lacks; the message names the newest stable one |
| `No such option` | [Troubleshooting](../../../scripts/README.md#no-such-option-exit-2) |
| `chart dependencies ... are missing` | Add `--update-dependencies`, or run `make helm-update-dependencies` |
| Dry run: `no matches for kind` | [Why](../../../scripts/README.md#--dry-run-fails-with-no-matches-for-kind); a real install is fine |
| `ErrImagePull` with 401/403 | The pull secret: credentials, `REGISTRY_SERVER` |
| `ImagePullBackOff` with `no match for platform in manifest` (arm64 node, e.g. Apple Silicon) | MLRun's images are amd64-only: [load them first](../../../scripts/README.md#apple-silicon-arm64) |
| Function pod `ImagePullBackOff` with `--local-registry` | The event names the registry and the cause: [the step it maps to](../../../scripts/README.md#function-pod-imagepullbackoff-with---local-registry) |
| A function still builds or pulls from the old registry after a registry change | It keeps the registry it was created with; delete it and create it again |
| Timeout or `ProgressDeadlineExceeded` | Usually a slow pull: rerun, it upgrades in place ([An install failed or timed out](../../../scripts/README.md#an-install-failed-or-timed-out)) |
| `mlrun-api-chief` restarts with `Can't connect to MySQL` | Normal on a first start; wait |
| Hard clean ends with leftovers | [A hard clean left a Kafka pod and its PVC](../../../scripts/README.md#a-hard-clean-left-a-kafka-pod-and-its-pvc) |

Fix the cause, then go back to the dry run. Rule 3 still applies.
