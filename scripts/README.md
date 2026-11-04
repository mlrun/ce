# MLRun CE Installer

Interactive installer for the MLRun CE Helm chart on a local or CI Kubernetes cluster.

By default it installs the **published** chart from `https://mlrun.github.io/ce`, so the
one-liner below works with no repo checked out. To install this repo's own chart instead —
the working tree, on whatever branch you have checked out — pass
`--chart-path ./charts/mlrun-ce`. See [Install this repo's chart](#install-this-repos-chart).

**More docs:** [Parameters reference](docs/parameters.md) (all flags/env vars) ·
[Configuration](docs/configuration.md) (`ce-config.yaml`, ingress, local registry,
OpenTelemetry, precedence) · [FAQ](docs/faq.md) (known gotchas)

---

## Requirements

Three tools, and `uv` supplies the Python:

| Tool | Notes |
|---|---|
| `uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` — brings its own Python interpreter and resolves the installer's dependencies itself, so there is nothing to `pip install` and no virtualenv to manage |
| `helm` | [install](https://helm.sh/docs/intro/install/) |
| `kubectl` | Configured and pointing at your cluster |

`docker` is **not** required. The image pull secret is a Kubernetes Secret the installer
renders itself and applies with kubectl, never anything Docker builds; the only thing a
Docker daemon adds is a best-effort `docker login` in the pre-install validators, which
reports `skipped` without it. That is also what lets the installer run from inside a pod,
where there is no daemon at all.

The secret is piped to `kubectl apply` on standard input rather than passed as
`kubectl create secret docker-registry --docker-password …`, so your registry password is
never a command-line argument: not in the process table, and not in the command text the
installer prints if a kubectl call fails. Worth knowing if you run this in CI, where that
output is usually captured and kept.

---

## Quick Start

### Run directly (no download)

Pin to a release tag. Pick one from [the releases page](https://github.com/mlrun/ce/releases)
— the installer from tag `mlrun-ce-X` is the one tested against chart `X`:

```bash
CE_TAG=mlrun-ce-0.12.1-rc.8
uvx --from "git+https://github.com/mlrun/ce@${CE_TAG}#subdirectory=scripts" \
  mlrun-ce-installer install
```

`uvx` fetches the installer, resolves its dependencies into a throwaway environment and
runs it, leaving nothing behind on your system Python.

The tag above is the release this README ships with. If it is not on the releases page yet,
use the newest tag listed there. Releases older than the installer have no `scripts/`
directory, and `uvx` fails on them.

Substituting `development` for the tag always gets the newest installer, but it moves with
every merge, so two runs a day apart can differ. Prefer a tag anywhere reproducibility
matters, CI especially.

A tag pins the installer, not its dependencies down to the patch: this path resolves typer,
click, rich and pyyaml fresh, within the ranges `scripts/pyproject.toml` declares. The clone
path below is the exactly-pinned one, via the committed `scripts/install.py.lock`. If you
need that guarantee without a clone, install into an environment you control and lock it
there.

### Install as a named command

```bash
CE_TAG=mlrun-ce-0.12.1-rc.8
uv tool install "git+https://github.com/mlrun/ce@${CE_TAG}#subdirectory=scripts"
mlrun-ce-installer version
```

`uv tool upgrade mlrun-ce-installer` moves it forward; `uv tool uninstall
mlrun-ce-installer` removes it.

### From a clone of this repo

```bash
./scripts/install.py
```

Still installs the published chart. Add `--chart-path ./charts/mlrun-ce` to install the
chart from your working tree instead.

The `#!/usr/bin/env -S uv run --script` shebang means `./scripts/install.py` is enough —
you do not need to say `uv run`. Running it as `python3 scripts/install.py` also works: if
the dependencies are missing it re-executes itself under uv rather than failing on an
import.

To get the `mlrun-ce-installer` command while working on a clone, symlink it onto your
PATH:

```bash
make installer-link                                    # links into ~/.local/bin
make installer-link INSTALLER_BIN_DIR=/usr/local/bin   # or somewhere else on PATH
```

The link points back at your working tree, so edits take effect immediately and
`mlrun-ce-installer version` reports the chart version rather than `unknown`. `make
installer-unlink` removes it.

---

## Commands

```bash
mlrun-ce-installer install [options]     # the default when no command is given
mlrun-ce-installer uninstall [--hard-clean]
mlrun-ce-installer version
mlrun-ce-installer help
```

The command is optional. Flags on their own mean `install`, so `--dry-run` and
`install --dry-run` are the same thing and anything written before commands existed still
works. `uninstall` is equivalent to the older `--uninstall` flag. A word that isn't one of
the four is rejected rather than treated as an install, so a typo like `unistall` can't
deploy a cluster by accident.

Colored output is suppressed automatically when stdout isn't a terminal, and when
`NO_COLOR` is set, so piped and redirected runs stay readable.

---

## Versioning and releases

The installer has no version of its own. It ships with the chart and is released by the
same tag, so `mlrun-ce-installer version` reports the version from
`charts/mlrun-ce/Chart.yaml` — bumping the chart bumps the installer, with no second copy
to keep in step.

That works however you run it. From a clone the version is read from the chart directly,
and symlinks are resolved first, so `make installer-link` still finds the chart in the
checkout it points at. Installed via `uvx` or `uv tool install` there is no chart on disk,
so the version is baked into the package when it is built from the tag. Only a copy that
is neither — the file moved to a bin directory by hand, say — reports `unknown`, because
nothing then records where it came from; that's what pinning to a release tag answers.

They're coupled on purpose. The installer encodes chart internals — the chart's fixed
NodePorts, and the value paths it writes — so an installer and a chart from the same tag
are the only pairing guaranteed to agree, and that pairing is what it installs unless
`--ce-version` says otherwise. Independent versions would invite a mismatch whose failure
mode is silent: a renamed value path becomes a value that quietly does nothing.

Releasing follows from that. A push to `development` or a `X.Y.x` branch runs
chart-releaser, which tags `mlrun-ce-<chart version>` and cuts a GitHub Release; that
tag's tree contains `scripts/`, which is what the pinned `uvx --from "git+…@<tag>"`
invocations above resolve against. So shipping an installer change is just merging it with a
chart version bump — there's no separate installer release to cut. Note the chart
tarball published to the Helm repo packages `charts/mlrun-ce` only, so the installer is
available from the git tag rather than from inside the `.tgz`.

---

## Installation flows

### Interactive install

Run the script with no flags. You will be prompted for:

1. Docker registry username, password, server URL, and email (see
   [Choosing a registry](#choosing-a-registry))
2. External host address (suggested from your kubectl context — see
   [FAQ](docs/faq.md#what-address-does-the-installer-suggest-for-external_host_address-and-why))
3. Docker registry URL for images

```bash
./scripts/install.py
```

Nothing is created on the cluster while you answer. Once the questions are done the
installer prints the target and asks before its first write:

```
[INFO] About to install:
[INFO]   Cluster:   docker-desktop (https://127.0.0.1:6443)
[INFO]   Namespace: mlrun
[INFO]   Release:   mlrun-ce
[INFO]   Chart:     mlrun-ce/mlrun-ce 0.12.1
[INFO]   Registry:  index.docker.io/myuser (pull secret 'registry-credentials')
[INFO]   Address:   host.docker.internal
Proceed? [Y/n]:
```

Check the cluster line: it is whatever your kubeconfig's current context (or `KUBE_CONTEXT`)
points at. Answering `n`, or pressing Ctrl-C at any prompt, leaves the cluster untouched.
The summary is printed but not asked under `--non-interactive` and `--dry-run`.

### Choosing a registry

MLRun builds a container image for each function you deploy, pushes it to a registry, and
the cluster then pulls it from there. So the installer needs a registry you can push to, and
it stores the credentials as a pull secret (`registry-credentials`) in the namespace.

- **Docker Hub** is the simplest: your username, an
  [access token](https://docs.docker.com/security/for-developers/access-tokens/) as the
  password, and `index.docker.io/<username>` as the registry URL.
- **Any other registry** (GHCR, ECR, Harbor, …) works the same way: set the server URL to
  the registry's host and the registry URL to the repository prefix images go under.
- **`--local-registry`** runs a `registry:2` inside the cluster instead, with no account
  at all. Good for a laptop, see [Configuration](docs/configuration.md) for its caveats.
- **A secret you already have**: create it yourself and pass `--skip-secret`. A secret of
  that name the installer did not create is never modified, and no credentials are asked
  for it.

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

./scripts/install.py
```

### Install this repo's chart

The installer ships alongside the chart it installs, but does **not** install it by
default — pass `--chart-path ./charts/mlrun-ce` to install the chart from your working
tree instead of the published release. This is how you test a branch or PR: check it out
here, then point the installer at the chart directory. No git operations happen inside
the installer.

Chart dependencies are resolved first. `install.py` prefers `helm dependency build`, which
honours `requirements.lock` rather than re-resolving `requirements.yaml`, and skips the
fetch entirely when `charts/` already holds every tarball the lock names — so a second run,
or a run on an air-gapped host, does not need the upstream Helm repos. Pass
`--skip-dependency-update` to suppress the fetch unconditionally.

```bash
# 1. Check out the branch/PR you want to test
git checkout my-branch

# 2. Dry-run first to validate without deploying
./scripts/install.py --chart-path ./charts/mlrun-ce --dry-run

# 3. Install for real
./scripts/install.py --chart-path ./charts/mlrun-ce
```

`--ce-version` is ignored in local-path mode — the chart version comes from
`charts/mlrun-ce/Chart.yaml` as it exists on your branch. Drop `--chart-path` (and
optionally add `--ce-version`) to go back to installing a published release.

### Dry run

Runs every check and prompt, prints the plan, and renders the chart with
`helm upgrade --install --dry-run=server`, without creating anything.

```bash
./scripts/install.py --dry-run
```

`--dry-run=server` lets helm *read* the cluster: which API versions and CRDs exist, what
`lookup()` returns, which resources would collide. It sends nothing to the API server, so
admission webhooks and the server's own validation are not exercised. On a cluster that does
not have the chart's CRDs yet, the default install's dry run fails with `no matches for kind
"PrometheusRule"` — see the [FAQ](docs/faq.md#--dry-run-fails-with-no-matches-for-kind-prometheusrule).
Helm older than 3.13 has no server mode and falls back to a client-side render.

Pre-install validators still run in `--dry-run` — see [Configuration](docs/configuration.md#pre-install-validators).

### Upgrading or re-running

Run the same command again. The installer always runs `helm upgrade --install`, so a second
run upgrades the existing release in place: change a flag, pick up a newer chart with
`--ce-version`, or retry an install that timed out.

- An interactive re-run offers to reuse the pull secret the first run created, so you are
  not asked for the password again. Credentials passed through the environment replace it.
- The installer's resolved values are rewritten to `mlrun-ce-values.yaml` (see
  `GENERATED_VALUES_FILE`) on every run. If you upgrade with plain helm instead, pass that
  file with `-f`, or helm resets those values to the chart defaults.
- A release with the same chart under a different name (`my-mlrun` from the chart README,
  say) is refused rather than installed next to it: set `RELEASE_NAME` to that name to
  upgrade it.

### When an install fails

Every run writes a debug log, `mlrun-ce-install.log` in the current directory (see
`INSTALL_LOG_FILE`), with each command the installer ran, its exit code, timing and output,
plus helm's full output. A failed run prints its path:

```
[ERROR] Helm installation failed (exit code 1).
[INFO] To see what did not come up:
[INFO]   kubectl get pods -n mlrun
[INFO]   kubectl get events -n mlrun --sort-by=.lastTimestamp
[INFO] A timeout (HELM_TIMEOUT=960s) is often a slow first image pull; running the same command again upgrades the release in place.
[INFO] Common failures and their fixes: https://github.com/mlrun/ce/blob/development/scripts/docs/faq.md
Full debug log: /home/me/mlrun-ce-install.log
```

The log is readable by you only and never contains the registry password; attach it to a
bug report as is.

### Uninstall

```bash
./scripts/install.py uninstall
```

Removes the helm release, then what the installer created outside the chart: the in-cluster
registry's Deployment, Service and Ingress (`--local-registry`), and the pull secret. Those
are selected by the installer's `app.kubernetes.io/managed-by` label, so a pull secret you
created yourself stays. `helm uninstall` runs with the `HELM_TIMEOUT` timeout (default
`960s`).

Kept: the namespace, the chart's CRDs, and every PersistentVolumeClaim, the registry's
included, so a reinstall finds its data again. `--hard-clean` also deletes the PVCs in the
namespace and the released PVs bound to them (data loss). Add `--dry-run` to either to see
what would be removed. For removing the namespace too, see the
[FAQ](docs/faq.md#deleting-everything-including-the-namespace).

---

## What's next

- Configuring the registry, chart source, ingress, local registry, disabled components, or OpenTelemetry → [docs/configuration.md](docs/configuration.md)
- Every flag and environment variable → [docs/parameters.md](docs/parameters.md)
- Something looks like a bug but isn't → [docs/faq.md](docs/faq.md)

---

## After Installation

Once complete, the script prints a table of what the release serves, read from the chart's
NOTES. With the default NodePort install and `localhost` as the external address:

```
                     MLRun CE - Access URLs
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ SERVICE            ┃ URL             ┃ CREDENTIALS          ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ Jupyter UI         │ localhost:30040 │                      │
│ Nuclio UI          │ localhost:30050 │                      │
│ MLRun UI           │ localhost:30060 │                      │
│ MLRun API          │ localhost:30070 │                      │
│ SeaweedFS S3 API   │ localhost:30094 │                      │
│ SeaweedFS Admin UI │ localhost:30093 │ seaweed / seaweed123 │
│ Grafana UI         │ localhost:30010 │                      │
│ Prometheus UI      │ localhost:30020 │                      │
│ TimescaleDB        │ localhost:30110 │ postgres             │
└────────────────────┴─────────────────┴──────────────────────┘
```

The addresses carry no scheme; open them as `http://`. Disabled components drop out of the
table, and with `--enable-ingress` each UI shows its ingress host instead of a port.
