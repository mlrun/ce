@AGENTS.md
@CONTRIBUTING.md

## Preferred Response Patterns
<!-- These are Claude-specific behavioral instructions on HOW to respond. AGENTS.md covers WHAT the codebase contains. -->

- Values changes: show `--set` flags or a patch values file overlay, not edits to `values.yaml` directly, unless there is a change with the default value that should be reflected in `values.yaml` (e.g. a new component's `enabled` flag)
- New templates: show the complete file including the `{{- if .Values.<component>.enabled }}` guard and `include "mlrun-ce.common.labels"` call
- Service references within templates: use `{{ .Release.Namespace }}`, never hardcode namespace strings
- After any `requirements.yaml` change: remind the user to run `make helm-update-dependencies` and commit `requirements.lock`
- PRs target `upstream/development` — the repo uses a fork-based workflow; always reference `upstream/development` as the base branch, not `origin/development`
- Branch names follow `<scope>/<short-description-or-ticket>` — e.g. `feature/add-redis-support` or `fix/CE-111`
- Please make sure to update all values files in `charts/mlrun-ce/` if your change affects the default installation (admin, non-admin, cluster IP) — e.g. if you add a new component with an `enabled` flag, add it to all three values files with the appropriate default value
- Update README docs if your change adds a new component, changes component version or changes the installation process.
