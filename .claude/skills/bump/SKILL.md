---
name: bump
description: Bump the chart version in charts/mlrun-ce/Chart.yaml (patch, minor, or rc)
allowed-tools: Read(charts/mlrun-ce/Chart.yaml) Edit(charts/mlrun-ce/Chart.yaml) Read(charts/mlrun-ce/README.md) Edit(charts/mlrun-ce/README.md)
disable-model-invocation: false
---

Bump the version in `charts/mlrun-ce/Chart.yaml`.

Usage: /bump <patch|minor|rc>

- `patch` — increment the patch digit: `0.11.0` → `0.11.1`
- `minor` — increment the minor digit and reset patch: `0.11.3` → `0.12.0`
- `rc` — increment the RC counter on the current version: `0.11.0-rc.34` → `0.11.0-rc.35`
  - If the current version has no RC suffix, add `-rc.1`: `0.11.0` → `0.11.0-rc.1`

Steps:
1. Read the current version from `charts/mlrun-ce/Chart.yaml` (the `version:` field).
2. Compute the new version according to the argument above.
3. Show the user: "Bumping `<old>` → `<new>`" and ask for confirmation before writing.
4. On confirmation, update the `version:` field in `charts/mlrun-ce/Chart.yaml` in-place.
5. Remind the user: version bumps must be committed before opening a PR, and the PR title must follow `[Scope] description` format.
6. Update the MLRun CE version under Version Matrix in `charts/mlrun-ce/README.md`.

The installer needs no edit. `Chart.yaml` is the only version number in the repo — `scripts/install.py` resolves to it, reading the chart directly from a checkout and otherwise using a value baked in at build time by `scripts/hatch_build.py`. Bumping the chart bumps the installer, and there is nothing in `scripts/` to keep in step.

If no argument is given, show the current version and list the three options with the resulting version for each, then ask which to apply.
