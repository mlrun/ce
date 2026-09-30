---
name: deploy
description: >-
  Deploy or uninstall MLRun CE with scripts/install.py — the interactive path, the
  config-file path, the non-interactive/CI path, dry runs, installing this repo's chart
  from a working tree, and targeting a remote cluster. Use when a developer asks to
  install CE, deploy to a cluster or lab, test a branch's chart on a real cluster, run a
  dry run, tear an install down, or work out why an install failed.
disable-model-invocation: false
---

# Deploying MLRun CE

The installer wraps `helm install` for the mlrun-ce chart. It resolves configuration,
checks the cluster, creates the registry pull secret, and composes the helm command.

**It installs the _published_ chart by default.** To deploy the chart in the working tree —
which is what you want when testing a branch — you must pass
`--chart-path ./charts/mlrun-ce`. Forgetting this is the single most common mistake: the
install succeeds and silently deploys a released chart instead of the change under test.

## Before you deploy anything, establish where

Ask, or confirm from context, **which cluster** — and say it back before running. An
install is not reversible in the way a code change is, and the installer targets whatever
`kubectl` currently points at, which is rarely what someone means when they say "install
it".

```bash
kubectl config current-context          # the target if KUBE_CONTEXT is unset
kubectl config get-contexts -o name     # what else is available
echo "${KUBE_CONTEXT:-<unset>}"         # overrides current-context when set
```

`KUBE_CONTEXT` wins over the ambient context, and it is easy to leave exported from an
earlier session. Check it explicitly rather than assuming the current context is the target.

Then prefer `--dry-run` for the first run. It renders the chart and validates it against the
API server without creating resources, and the pre-install validators still run.

## The four ways to supply configuration

All four produce the same helm command; they differ only in where values come from.

### Interactive

The default. Prompts for registry username, password, server URL and email, the external
host address (autodetected, press Enter to accept), and the registry URL for images.

```bash
./scripts/install.py
```

### Config file

`--config` reads registry, chart-source and host defaults from a `ce-config.yaml`. It
**works in both interactive and non-interactive mode** — this is the part people miss.
Interactively, you are still prompted for anything the file does not set, but each prompt is
pre-filled from the file, so Enter accepts it and typing overrides just that field for the
one run.

```bash
cp scripts/ce-config.yaml.example ce-config.yaml   # then edit it
./scripts/install.py --config ce-config.yaml
```

The password is **never** read from the config file. Supply `REGISTRY_PASSWORD`,
`REGISTRY_PASSWORD_FILE`, or answer the prompt. A config key for it is warned about and
ignored, so a file that appears to set one is not doing what it looks like.

### Non-interactive / CI

`CI=true` or `--non-interactive` means it will never prompt: a missing required value exits
1 immediately rather than hanging on stdin.

```bash
export CI=true
export REGISTRY_USERNAME=myuser REGISTRY_PASSWORD=… REGISTRY_URL=index.docker.io/myuser
export REGISTRY_SERVER=https://index.docker.io/v1/ REGISTRY_EMAIL=me@example.com
export EXTERNAL_HOST_ADDRESS=localhost
./scripts/install.py
```

Combining this with `--config` is the usual CI shape: the file carries everything stable,
the environment carries the password.

```bash
CI=true REGISTRY_PASSWORD=… ./scripts/install.py --config ce-config.yaml
```

### Values file

`-f` passes a values file straight to helm. Used **alone** it is self-contained: no secret
is created, no prompts, and the installer adds no `--set` flags of its own, so the file must
supply everything including a reference to an existing pull secret.

```bash
./scripts/install.py -f my-values.yaml
```

`-f` and `--config` compose rather than conflict. helm applies `--set` after `--values`, so
config-resolved values win over the same keys in the values file, and the secret *is*
created when both are given.

## Precedence

Highest wins. This is the answer to "why isn't my setting taking effect":

```
flag  >  env var  >  ce-config.yaml (applied as --set)  >  -f/--values (raw)  >  chart defaults
```

A `--disable-*` flag can only ever add a disable — the config file cannot remove one set on
the command line. The `otel` keys run the other way: everything ships off, so each key opts
in.

## Deploying this repo's chart

```bash
git checkout my-branch
./scripts/install.py --chart-path ./charts/mlrun-ce --dry-run   # validate first
./scripts/install.py --chart-path ./charts/mlrun-ce
```

`--ce-version` is ignored in this mode; the version is whatever `Chart.yaml` says on your
branch. Chart dependencies resolve with `helm dependency build`, honouring
`requirements.lock` rather than re-resolving `requirements.yaml`, and the fetch is skipped
entirely when `charts/` already holds every tarball the lock names — so a second run, or an
air-gapped host, needs no upstream Helm repos. `--skip-dependency-update` suppresses it
unconditionally.

## Remote clusters

`install.py` never SSHes anywhere. It runs `kubectl`/`helm` against whatever context it is
given. For a cluster reachable only over SSH, open the tunnel yourself and add a context
pointing at `localhost:<port>`, then set `KUBE_CONTEXT` — the full recipe is in
`scripts/docs/configuration.md`.

One consequence worth knowing: when `KUBE_CONTEXT` names a non-current context, the
`EXTERNAL_HOST_ADDRESS` autodetect skips the minikube and docker-desktop heuristics, since
those describe the *local* environment, and falls back to the target's node IP. Behind a
tunnel that node IP is usually not reachable from where you are running the installer, so
set `EXTERNAL_HOST_ADDRESS` explicitly.

## Uninstalling

```bash
./scripts/install.py uninstall                 # helm uninstall; keeps namespace, CRDs, data
./scripts/install.py uninstall --hard-clean    # also removes persistent data
```

`--hard-clean` can leave orphaned Strimzi resources behind: the Kafka CRs are helm *hook*
resources, so they are not in the release manifest, and deleting the operator while the CRs
and their pods still exist strands a PVC in `Terminating` on the `pvc-protection` finalizer.
`scripts/AGENTS.md` has the manual cleanup order under "Known non-bugs".

## When an install fails

Work outward in this order — each step rules out a whole class of cause:

1. **Re-run with `--dry-run`.** If that also fails, it is configuration or validation, not
   the cluster's reaction to the deploy.
2. **Read which validator blocked.** Blocking checks mean the install would certainly fail;
   advisory ones only warn. `--skip-validators` is for when you know better than the check,
   not a default.
3. **Check the release state.** A prior interrupted run leaves `pending-install`, and the
   next install fails on it: `helm list -n mlrun -a`.
4. **Check precedence before believing a value was ignored.** Print the composed helm
   command with `--dry-run` and look at the actual `--set` flags rather than inferring from
   the config file.

## Reference

- `scripts/README.md` — quick start, commands, requirements
- `scripts/docs/configuration.md` — config file schema, ingress, local registry,
  OpenTelemetry, remote contexts
- `scripts/docs/parameters.md` — every flag and environment variable
- `scripts/docs/faq.md` — known gotchas, including the host-autodetect fallback chain
- `scripts/AGENTS.md` — install flow, known non-bugs, bug log

Use the `run-tests` skill to verify a change before deploying it, and the
`installer-feature` skill to add a flag, env var or config key.
