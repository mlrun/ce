---
name: contributing
description: >-
  The contribution workflow for MLRun CE — branching off development, the [Scope] commit
  and PR title format, the Chart.yaml version bump, keeping the install-mode values files
  in sync, which tests to run before pushing, and the PR checklist. Use when a developer
  asks how to start a change, name a branch or commit, prepare or open a PR, or what is
  required before a change can merge.
disable-model-invocation: false
---

# Contributing to MLRun CE

This repo is a Helm umbrella chart plus the `scripts/install.py` installer. The rules below
are enforced by CI, so skipping one means a red PR rather than a style debate.

## Fork-based, and the default branch is `development`

Not `main`, not `master`. PRs target `upstream/development`.

```bash
git remote add upstream https://github.com/mlrun/ce   # once
git fetch upstream
git checkout -b <scope>/<short-description> upstream/development
```

Branch names are `<scope>/<short-description-or-ticket>` — `feature/add-redis-support`,
`fix/CE-111`.

## Commit and PR title format

```
[Scope] Short description
```

Scopes, case-insensitive: `Feature`, `Fix`, `Docs`, `Improvement`, `Revert`, `Breaking`,
`CI`. Enforced by `.github/workflows/pr-validation.yml`. The squash-merge title becomes the
changelog entry via `git-cliff`, so write it for someone reading release notes, not for
yourself.

## Always bump `charts/mlrun-ce/Chart.yaml`

Every PR. Format `major.minor.patch` or `major.minor.patch-rc.N`. Use the `bump` skill.

This is also the installer's version — there is no second number. The chart and installer
are released by the same tag because the installer encodes chart internals (fixed
NodePorts, `--set` value paths), so a mismatched pair fails silently: a renamed value path
becomes a `--set` that quietly does nothing.

## What a change obliges you to update

Work through whichever of these the change touches. Most review comments on this repo are
one of these being missed:

| If you changed | Also update |
|---|---|
| a default that affects a standard install | `values.yaml` **and** all three install-mode values files |
| `requirements.yaml` | run `make helm-update-dependencies`, commit `requirements.lock` |
| added a component, changed a version, changed install | `charts/mlrun-ce/README.md` |
| added a component | `templates/NOTES.txt`, and a section in `AGENTS.md` |
| an installer flag or env var | `scripts/docs/parameters.md` |
| an installer config key | `scripts/docs/configuration.md`, `scripts/ce-config.yaml.example` |
| the installer's PEP 723 dependencies | re-run `uv lock --script scripts/install.py` |

The three install-mode files are `admin_installation_values.yaml`,
`non_admin_installation_values.yaml` and `non_admin_cluster_ip_installation_values.yaml`.
They drift silently — nothing fails if one is missed, the install just behaves differently
in that mode.

## Testing before you push

Run the levels that apply; none of them needs a cluster except the last.

```bash
make helm-lint                    # chart lint; run from a feature branch, ct needs a diff
make installer-test               # installer unit + golden suites, if scripts/ changed
```

Schema-validate a render without a cluster:

```bash
helm template mlrun charts/mlrun-ce -f charts/mlrun-ce/values.yaml \
  | kubectl apply --dry-run=client -f -
```

Swap in an install-mode overlay with a second `-f` to check a particular mode.

For a real cluster, use the `deploy` skill — and note the PR checklist requires testing
against a real cluster, not just lint.

If `make installer-test`'s golden suite fails, **read the diff before re-recording**. Every
changed line there is a change in what the installer does to somebody's cluster; re-recording
without reading turns the suite into a rubber stamp. The `run-tests` skill covers the
workflow.

## Opening the PR

`.github/pull_request_template.md` is applied automatically and every item must be
addressed. The ones that actually block:

- [ ] Tested against a real cluster, not only lint
- [ ] `Chart.yaml` bumped
- [ ] Install-mode values files in sync
- [ ] Documentation updated
- [ ] Breaking changes disclosed

Use the `pr` skill to generate the filled description from the branch diff. It checks the
boxes it can confirm from the diff and leaves the rest, which is the honest split — do not
tick the real-cluster box on its behalf.

## Prerequisites

| Tool | Minimum | For |
|---|---|---|
| helm | 3.6 | rendering, linting, installing |
| kubectl | 1.24 | cluster interaction |
| uv | 0.4 | `scripts/install.py`, and `uvx ruff` for installer lint/format |

First-time chart setup:

```bash
make helm-repo-add                # adds external repos from requirements.yaml; idempotent
make helm-update-dependencies     # downloads sub-chart tarballs; required before lint
make helm-lint
```

## Reference

- `CONTRIBUTING.md` — the authoritative workflow this skill summarises
- `AGENTS.md` — chart architecture, how to add a component
- `scripts/AGENTS.md` — installer design notes and bug log
- Related skills: `bump` (version), `pr` (description), `run-tests`, `deploy`,
  `installer-feature`
