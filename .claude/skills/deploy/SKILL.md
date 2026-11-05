---
name: deploy
description: >-
  Deploy or uninstall MLRun CE on a real cluster with scripts/install.py on a user's behalf —
  testing a branch's chart, a dry run, a lab or remote cluster, a teardown, or a failed
  install. Covers the safety rules and the agent-specific steps; scripts/README.md covers
  every option. Use when a developer asks to install CE, deploy to a cluster or lab, test a
  branch on a cluster, tear an install down, or work out why an install failed.
disable-model-invocation: false
---

# Deploying MLRun CE

`scripts/README.md` documents every way to install, configure, upgrade, uninstall and
troubleshoot. Read the relevant section there; this skill adds only what an agent must do
differently.

## Rules

- **Name the cluster before anything runs.** The installer targets `KUBE_CONTEXT` if set,
  otherwise kubeconfig's current context, and `KUBE_CONTEXT` is easily left exported from an
  earlier session. Check both and say the target back to the user:

  ```bash
  kubectl config current-context
  echo "${KUBE_CONTEXT:-<unset>}"
  ```

  To target another context, set `KUBE_CONTEXT` for that one command rather than switching
  the user's current context.
- **Dry-run first**: `--dry-run` runs every check and renders the chart, creating nothing.
- **Test the branch, not the release.** Without `--chart-path ./charts/mlrun-ce` the
  installer installs the published chart, and a branch test silently succeeds on the wrong
  chart.
- **Never put the registry password on the command line** (argv, or an inline
  `REGISTRY_PASSWORD=… ./scripts/install.py` in a command you run or print). Ask the user to
  export it themselves, or use `REGISTRY_PASSWORD_FILE`, the interactive prompt, `--skip-secret`
  with an existing secret, or `--local-registry`.
- **Never uninstall, `--hard-clean`, delete PVCs or delete a namespace without the user's
  explicit OK for that cluster.** `--hard-clean` destroys data. Use `uninstall --dry-run` to
  show what would go.
- **Run non-interactively** (`--non-interactive`); an agent cannot answer prompts. Then
  every required value must come from the environment or `--config`.

## Testing a branch

```bash
./scripts/install.py --chart-path ./charts/mlrun-ce --dry-run --non-interactive
./scripts/install.py --chart-path ./charts/mlrun-ce --non-interactive
```

## When a run fails

1. Read `mlrun-ce-install.log` in the directory the installer ran from (`INSTALL_LOG_FILE`):
   every command, exit code and helm's full output. The failure message prints its path.
2. A blocking pre-install check stops before anything is created; the message says which.
3. An interrupted earlier run can leave the release `pending-install`. List it with
   `helm list -n <namespace> --pending --failed --deployed` (Helm 4 has no `--all`).
4. To see the values a run resolved, read `mlrun-ce-values.yaml` (`GENERATED_VALUES_FILE`),
   which is written on dry runs too.
5. Match the symptom against Troubleshooting in `scripts/README.md`.
