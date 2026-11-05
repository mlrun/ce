---
name: contributing
description: >-
  The contribution workflow for MLRun CE and how to change its installer — branching off
  development, the [Scope] commit and PR title format, the Chart.yaml version bump, keeping
  the install-mode values files in sync, the tests to run, the PR checklist, and the
  conventions of scripts/install.py and the scripts/ce_installer package (adding a flag,
  environment variable, ce-config.yaml key, chart value or validator). Use when a developer
  asks how to start a change, name a branch or commit, prepare or open a PR, what is
  required before a change can merge, or how to add or change an installer option.
disable-model-invocation: false
---

# Contributing to MLRun CE

This repo is a Helm umbrella chart plus the `scripts/install.py` installer. The rules below
are enforced by CI or review.

## Branches, commits, PRs

Fork-based; PRs target `upstream/development` (not `main`).

```bash
git remote add upstream https://github.com/mlrun/ce   # once
git fetch upstream
git checkout -b <scope>/<short-description> upstream/development
```

Branch names are `<scope>/<short-description-or-ticket>` (`feature/add-redis-support`,
`fix/CE-111`). Commit and PR titles are `[Scope] Short description`, scope one of `Feature`,
`Fix`, `Docs`, `Improvement`, `Revert`, `Breaking`, `CI` (case-insensitive, enforced by
`.github/workflows/pr-validation.yml`). The squash-merge title becomes the changelog entry.

## Bump `charts/mlrun-ce/Chart.yaml` in every PR

Use the `bump` skill. That version is also the installer's: the release tag
`mlrun-ce-<version>` that chart-releaser cuts on merge carries `scripts/`, and the installer
installs that same chart version by default. Installer CI fails if the version is already
released; merge `development` and bump again.

## What a change obliges you to update

| If you changed | Also update |
|---|---|
| a default that affects a standard install | `values.yaml` and all three install-mode values files (`admin_installation_values.yaml`, `non_admin_installation_values.yaml`, `non_admin_cluster_ip_installation_values.yaml`) |
| `requirements.yaml` | run `make helm-update-dependencies`, commit `requirements.lock` |
| added a component, changed a version, changed install | `charts/mlrun-ce/README.md` |
| added a component | `templates/NOTES.txt`, and a section in `AGENTS.md` |
| an installer flag or environment variable | the Reference tables in `scripts/README.md` (a test fails otherwise) |
| an installer `ce-config.yaml` key | `scripts/ce-config.yaml.example` |
| a message the installer prints with a README anchor | the heading in `scripts/README.md` (a test fails otherwise) |
| the PEP 723 header in `scripts/install.py` | `uv lock --script scripts/install.py`, same commit |

`scripts/README.md` is the only installer doc for users. Write there only what a user needs
to install, configure, upgrade, uninstall or troubleshoot; keep design notes and bug history
out of it (they belong in the commit message).

## Tests before you push

```bash
make helm-lint                                   # chart; run from a feature branch, ct needs a diff
make installer-lint-python                       # if scripts/ or tests/installer changed
KUBECONFIG=/nonexistent make installer-test      # unit + golden suites, Python 3.9
make installer-test-unit INSTALLER_PYTHON=3.13   # CI runs both versions
```

Schema-validate a chart render without a cluster:

```bash
helm template mlrun charts/mlrun-ce -f charts/mlrun-ce/values.yaml \
  | kubectl apply --dry-run=client -f -
```

The `run-tests` skill covers the installer suites. The PR checklist also asks for a run on a
real cluster (the `deploy` skill); nothing in PR CI installs to one.

## Opening the PR

`.github/pull_request_template.md` applies automatically. Use the `pr` skill to fill it from
the diff; it ticks only what the diff proves, so leave the real-cluster box to a human.

## Changing the installer

`scripts/install.py` is only a launcher (PEP 723 metadata, a uv re-exec when dependencies
are missing, `main()`). The code is the `scripts/ce_installer` package; its `__init__.py`
lists the modules in dependency order. Each module imports only from those above it, so put
new code in the lowest layer that fits.

### Invariants

- **Precedence is flag > env var > `ce-config.yaml` > default**, applied in two places
  only: the flag merge in `cli.py`'s `install()` and `config.load_config`. Everything else
  reads `Settings`, never `os.environ`. `config.py` only fills a value that is still unset.
- **Reads before writes.** In `cli.execute()`, everything before `confirm_target` only reads
  the cluster or asks the user; everything after it writes. A step that can refuse goes
  before it, a step that mutates the cluster after it.
- **Values go through a file, not `--set`.** `build_set_flags` composes `--set`-style pairs,
  `set_flags_to_values` folds them into `GENERATED_VALUES_FILE`, passed to helm after the
  user's `-f`. The only `--set` on helm's command line is the dry-run-only
  `kube-prometheus-stack.enabled=false` from `dry_run_exclusions`. The file is dumped in
  insertion order and recorded by the golden suite, so append to `build_set_flags` rather
  than reordering it.
- **The registry password never reaches argv or the logs.** The pull secret goes to
  `kubectl apply` on stdin; `console.add_secret` masks the password in the debug log;
  `shell.run` never logs `input_data`; `secret_data` redacts rendered Secrets.
  `mlrun-ce-values.yaml` holds no secret (`test_the_installer_values_file_carries_no_secret`).
- **Python 3.9 is the floor** (`requires-python` in `scripts/pyproject.toml`). typer reads
  annotations at runtime, so use `typing.Optional`/`List`, never `str | None`, and never
  `from __future__ import annotations`.
- **`docker` is optional.** Only the registry-login validator uses it; the installer must run
  in a pod without a daemon.
- **Dependencies are declared twice**, with identical ranges: the PEP 723 header in
  `install.py` (the clone path, pinned by `install.py.lock`) and `[project.dependencies]` in
  `scripts/pyproject.toml` (the `uvx --from git+…` path and the tests).
  `test_both_dependency_declarations_agree` and
  `test_every_declared_dependency_has_an_upper_bound` enforce it. Prefer the standard library
  over a new dependency.
- **`Chart.yaml` is the only version number.** `installer_version()` reads the chart beside
  the checkout, or the value `scripts/hatch_build.py` bakes into an installed wheel. Moving
  `scripts/` or `charts/` breaks that relative path
  (`test_the_hook_reads_the_real_chart_in_this_repo`). The pinned `mlrun-ce-<version>` tags in
  `README.md`, `scripts/README.md` and `scripts/install.py` must equal it
  (`test_the_documented_install_tag_matches_the_chart_version`).

### A new boolean flag

Say `--disable-jupyter`:

1. `settings.py`: add `disable_jupyter: bool = False` to `Settings`.
2. `cli.py`: add the `typer.Option` to `install()` and merge it with its variable:
   `settings.disable_jupyter = disable_jupyter or env_true("DISABLE_JUPYTER")`.
3. `config.py`: if it has a `ce-config.yaml` key, add it to the `components.*` loop.
   `components.*` keys disable with `"false"`; `otel.*` keys enable with `"true"`.
4. `helm_ops.py`: append the pair in `build_set_flags`:
   `flags += ["--set", "jupyterNotebook.enabled=false"]`.
5. Add a golden case, a unit test, and the README row (see the definition of done).

A flag that takes a value also goes in `VALUE_REQUIRED` in `cli.py`, so a missing value is
an error rather than swallowing the next flag. An optional value (`--enable-ingress [CLASS]`)
also needs `normalize_argv`, because click cannot express one.

### A new validator

Add it to `validators.py` and register it in `run_validators` (warnings) or
`run_blocking_validators` (refusals, which run before anything is created). Block only on
something that guarantees a failed install (no default StorageClass, a Helm that cannot
render the chart); warn on everything else, because `--skip-validators` turns off all of
them at once. `run()` returns a `Result` rather than raising, so check `.ok`.

### Definition of done

- [ ] A test named for the symptom a user would report, in the module's test file or, for a
      bug that reached a user, `tests/installer/test_regressions.py`. Verified by
      reintroducing the bug and watching it fail.
- [ ] A case in `CASES` in `tests/installer/test_golden_argv.py` if the change reaches helm
      or kubectl, recorded with `make installer-test-golden-update`, and every line of
      `git diff tests/installer/golden/` read and explained in the PR.
- [ ] The flag or variable in `scripts/README.md`'s Reference; a `ce-config.yaml` key in
      `scripts/ce-config.yaml.example`.
- [ ] `uv lock --script scripts/install.py` if the PEP 723 header changed.
- [ ] `Chart.yaml` bumped.
- [ ] If the change touches uninstall, PVCs or hooks, say so in the PR so someone runs it on
      a cluster.

## Prerequisites

| Tool | Minimum | For |
|---|---|---|
| helm | 3.17 | rendering, linting, installing (the SeaweedFS subchart needs `fromToml`) |
| kubectl | 1.24 | cluster interaction |
| uv | 0.4 | the installer, its tests, and `uvx ruff` |

First-time chart setup:

```bash
make helm-repo-add                # adds external repos from requirements.yaml; idempotent
make helm-update-dependencies     # downloads sub-chart tarballs; required before lint
make helm-lint
```

## Reference

- `CONTRIBUTING.md`: the workflow this skill summarises
- `AGENTS.md`: chart architecture, adding a component
- `scripts/README.md`: what the installer does, from the user's side
- Related skills: `bump`, `pr`, `run-tests`, `deploy`
