# MLRun CE Installer

Interactive installer for the MLRun CE Helm chart on a local or CI Kubernetes cluster.

By default it installs the **published** chart from `https://mlrun.github.io/ce`, so the
`curl | bash` one-liner below works with no repo checked out. To install this repo's own
chart instead — the working tree, on whatever branch you have checked out — pass
`--chart-path ./charts/mlrun-ce`. See [Install this repo's chart](#install-this-repos-chart).

**More docs:** [Parameters reference](docs/parameters.md) (all flags/env vars) ·
[Configuration](docs/configuration.md) (`ce-config.yaml`, ingress, local registry,
OpenTelemetry, precedence) · [FAQ](docs/faq.md) (known gotchas)

---

## Requirements

| Tool      | Notes                                               |
|-----------|-----------------------------------------------------|
| `helm`    | [install](https://helm.sh/docs/intro/install/) |
| `kubectl` | Configured and pointing at your cluster             |
| `docker`  | Daemon running and accessible by your user          |
| `yq`      | Only required when using `--config`/`CONFIG_FILE` — [install](https://github.com/mikefarah/yq/releases) (pin a version, verify its checksum) |

---

## Quick Start

### Run directly (no download)

```bash
curl -sSL https://raw.githubusercontent.com/mlrun/ce/development/scripts/install.sh | bash
```

### Install as a named command

```bash
curl -sSL https://raw.githubusercontent.com/mlrun/ce/development/scripts/install.sh \
  -o /usr/local/bin/mlrun-install && chmod +x /usr/local/bin/mlrun-install
mlrun-install
```

### From a clone of this repo

```bash
./scripts/install.sh
```

Still installs the published chart. Add `--chart-path ./charts/mlrun-ce` to install the
chart from your working tree instead.

---

## Installation flows

### Interactive install

Run the script with no flags. You will be prompted for:

1. Docker registry username, password, server URL, and email
2. External host address (auto-detected — see [FAQ](docs/faq.md) for the fallback chain)
3. Docker registry URL for images

```bash
./scripts/install.sh
```

### Non-interactive / CI install

Set `CI=true` (or pass `--non-interactive`) and export the required values. The script will never prompt — any required value that is missing causes an immediate exit 1 instead of hanging on stdin.

```bash
export CI=true
export REGISTRY_USERNAME=myuser
export REGISTRY_PASSWORD=mypassword
export REGISTRY_SERVER=https://index.docker.io/v1/
export REGISTRY_EMAIL=me@example.com
export REGISTRY_URL=index.docker.io/myuser
export EXTERNAL_HOST_ADDRESS=localhost   # or minikube ip

./scripts/install.sh
```

### Install this repo's chart

The installer ships alongside the chart it installs, but does **not** install it by
default — pass `--chart-path ./charts/mlrun-ce` to install the chart from your working
tree instead of the published release. This is how you test a branch or PR: check it out
here, then point the installer at the chart directory. The installer runs
`helm dependency update` on the path before installing; no git operations happen inside
the script.

```bash
# 1. Check out the branch/PR you want to test
git checkout my-branch

# 2. Dry-run first to validate without deploying
./scripts/install.sh --chart-path ./charts/mlrun-ce --dry-run

# 3. Install for real
./scripts/install.sh --chart-path ./charts/mlrun-ce
```

`--ce-version` is ignored in local-path mode — the chart version comes from
`charts/mlrun-ce/Chart.yaml` as it exists on your branch. Drop `--chart-path` (and
optionally add `--ce-version`) to go back to installing a published release.

### Dry run

Renders the Helm chart and validates it against the cluster API server without creating any resources. Useful to confirm a configuration is valid before a real install.

```bash
./scripts/install.sh --dry-run
```

Pre-install validators still run in `--dry-run` — see [Configuration](docs/configuration.md#pre-install-validators).

### Uninstall

```bash
./scripts/install.sh --uninstall
```

This runs `helm uninstall` with a 960s timeout. The namespace and CRDs are **not** deleted. For deleting persistent data too, see the [FAQ](docs/faq.md#deleting-everything-including-the-namespace).

---

## What's next

- Configuring the registry, chart source, ingress, local registry, disabled components, or OpenTelemetry → [docs/configuration.md](docs/configuration.md)
- Every flag and environment variable → [docs/parameters.md](docs/parameters.md)
- Something looks like a bug but isn't → [docs/faq.md](docs/faq.md)

---

## After Installation

Once complete, the script prints an access table with URLs and credentials for each service:

```
==============================================
  MLRun CE - Access URLs
==============================================
SERVICE            | URL                      | CREDENTIALS
-------------------+--------------------------+------------------
MLRun UI           | http://localhost:80      |
Grafana            | http://localhost:3000     | admin / prom-operator
...
==============================================
```
