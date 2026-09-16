---
name: installer-feature
description: >-
  Add or change a feature in the MLRun CE installer (scripts/install.py and the
  scripts/ce_installer package) — a new flag, environment variable, ce-config.yaml
  key, helm --set, validator, or prompt. Use when a developer asks to add an option
  to the installer, wire a chart value through it, add a pre-install check, or change
  how an existing option behaves.
---

# Adding a feature to the CE installer

The installer is `scripts/install.py` — a launcher carrying
[PEP 723](https://peps.python.org/pep-0723/) metadata — plus the `scripts/ce_installer`
package beside it. It replaced a bash script, `scripts/install.sh`, which was deleted once
its behaviour had been recorded as test expectations — you may still see it referenced in
the bug log as history. See `scripts/AGENTS.md` for the full design notes and
[`run-tests`](../run-tests/SKILL.md) for the test workflow.

## Before you write anything

**Read `scripts/AGENTS.md`.** Its "Fixed bugs" and "Known non-bugs" sections are a list of
mistakes already made once in this code; several are the kind you would otherwise make
again. If your change touches `resolve_external_host`, `do_hard_clean`, `helm_install`, or
the validators, read those entries first.

## Module layout, in dependency order

Each module imports only from the ones above it. Put new code in the lowest layer that
makes sense; if you find yourself wanting to import upward, the code is in the wrong place.

| Module | Holds |
|---|---|
| `console.py` | consoles, `log_info/warn/error`, `InstallerError`, `die` |
| `settings.py` | built-in defaults, `env_str`/`env_true`, the `Settings` dataclass, `prompt_or_env` |
| `shell.py` | `run`/`stream`, the KUBE_CONTEXT-aware `kubectl`/`helm` wrappers, prerequisites |
| `config.py` | the `ce-config.yaml` `installer:` block |
| `cluster.py` | namespace, external host address, chart source resolution |
| `registry.py` | pull secret, the optional in-cluster registry, the CoreDNS patch |
| `validators.py` | pre-install checks |
| `ui.py` | the live progress table, the access-URL table |
| `helm_ops.py` | `--set` composition, install, uninstall, hard clean |
| `cli.py` | version, argv pre-parse, `execute()` run order, the typer command |

## The one rule: precedence

**flag > env var > `ce-config.yaml` > built-in default**, and it is applied in exactly two
places — the flag merge in `cli.py`, then `config.load_config`. Nothing below those two
layers reads `os.environ` for a tunable; everything reads `Settings`. If you reach for
`os.environ` inside `validators.py` or `helm_ops.py`, stop: thread it through `Settings`.

`config.py` may only ever *fill in* a value that is still unset — note the
`if not settings.x and cfg(...)` shape of every assignment there. A config file must never
override a flag.

## Recipe: a new boolean toggle

Say you are adding `--disable-jupyter`.

1. **`settings.py`** — add the field to `Settings`. Booleans that come from a flag default
   to `False` and are merged in `cli.py`; env-only values use a `field(default_factory=...)`.

   ```python
   disable_jupyter: bool = False
   ```

2. **`cli.py`** — add the typer option to `install()`, then OR it with its env var in the
   merge block:

   ```python
   disable_jupyter: bool = typer.Option(
       False, "--disable-jupyter", help="Disable the Jupyter notebook deployment"
   ),
   ...
   settings.disable_jupyter = disable_jupyter or env_true("DISABLE_JUPYTER")
   ```

   Use `typing.Optional[str]` / `typing.List[str]`, **never** `str | None` and never
   `from __future__ import annotations`. typer resolves annotations at runtime; the
   postponed form breaks it, and the installer supports Python 3.9.

3. **`config.py`** — add the key to the `components.*` loop if it belongs there. Mind the
   polarity: `components.*` keys are `"false"` to disable (defaults enabled), `otel.*` keys
   are `"true"` to enable (defaults disabled).

4. **`helm_ops.py`** — emit the `--set` in `build_set_flags`. **Append, do not reorder** —
   the golden expectations pin the order of these flags.

   ```python
   if settings.disable_jupyter:
       flags += ["--set", "jupyterNotebook.enabled=false"]
   ```

5. **Tests and docs** — see "Definition of done" below.

## Recipe: a flag that takes a value

Same as above, but also add it to `VALUE_REQUIRED` in `cli.py` so a missing value is
rejected instead of swallowing the next flag as its argument:

```python
VALUE_REQUIRED = {
    "--chart-path": "Option {opt} requires a directory path (e.g. --chart-path ./charts/mlrun-ce).",
    ...
}
```

If the value is **optional** (`--enable-ingress [CLASS]`), it also needs handling in
`normalize_argv`, which rewrites it to `--flag=value` form. click cannot express an
optional option value; that is the whole reason the pre-parse exists.

## Recipe: a new validator

Add the function to `validators.py` and register it in `run_validators`. Decide
deliberately whether it **blocks or warns**:

- **Blocking** (`return False`) is for something that guarantees a failed install — no
  default StorageClass, a Helm below the chart's floor.
- **Warning** (`log_warn`, still `return True`) is for anything environment-dependent or
  advisory. Node capacity and registry auth warn.

When in doubt, warn. A false blocking check makes the installer unusable on a cluster that
would have worked, and `--skip-validators` is a blunt instrument — it disables all of them.

Validators must tolerate a command that fails or returns nothing: `run()` returns a
`Result` with a non-zero `code` rather than raising, so check `.ok` instead of assuming
output.

## Traps specific to this codebase

- **`os.environ` below `cli.py`/`config.py`.** Breaks precedence. Use `Settings`.
- **Reordering `build_set_flags`.** The golden suite compares argv exactly.
- **`str | None` annotations or `from __future__ import annotations`.** Breaks typer at
  runtime on 3.9.
- **Patching `shell.kubectl` in a test.** Each module does `from .shell import kubectl`, so
  it holds its own reference — patch `cluster.kubectl`, not `shell.kubectl`.
- **Exporting installer env vars in your shell.** Every tunable is an environment variable,
  so a leftover `export KUBE_CONTEXT=<lab>` from live testing changes what the tests
  exercise. The test suite scrubs them via an autouse fixture; your interactive shell does
  not, so a manual run can still mislead you.
- **Assuming `docker` exists.** It is optional and only used by the registry-auth
  validator. The installer must work from inside a pod, where there is no daemon.
- **Adding a third-party import.** Dependencies are declared twice, once per entry path:
  the PEP 723 header in `install.py` (for `uv run --script`, the clone path) and
  `[project.dependencies]` in `pyproject.toml` (for `uvx --from git+…` and the test run).
  Adding to one and not the other works on your machine and fails on the other path. After
  touching the PEP 723 header, regenerate the lock in the same commit:

  ```bash
  uv lock --script scripts/install.py
  ```

  CI fails on a stale lock via `uv lock --script scripts/install.py --check`, but only after
  a push. Prefer a new import from the standard library: every dependency is one more thing
  resolved on a user's machine before the installer can start.

## Definition of done

Run `make installer-lint && make installer-test` — but the checklist is what actually
matters:

- [ ] A regression test in `tests/installer/test_regressions.py`, named for the symptom a
      user would report.
- [ ] **The test was verified by reintroducing the bug and watching it fail.** A test that
      cannot fail is worse than no test: it reads as coverage. This is not optional — two
      tests in this suite originally passed against the reverted fix because two fixes
      overlapped, and only a mutation check found it.
- [ ] A case added to `CASES` in `tests/installer/test_golden_argv.py` if the change reaches
      helm or kubectl, then `make installer-test-golden-update`.
- [ ] `git diff tests/installer/golden/` read line by line and included in the PR
      description. Every changed line is a change in what the installer does to a cluster;
      re-recording without reading turns the suite into a rubber stamp.
- [ ] `scripts/docs/parameters.md` — the flag and its env var.
- [ ] `scripts/docs/configuration.md` — if it has a `ce-config.yaml` key.
- [ ] `scripts/ce-config.yaml.example` — likewise.
- [ ] `scripts/README.md` — only if it changes the common path or the requirements.
- [ ] `uv lock --script scripts/install.py` re-run and committed, if you touched the PEP 723
      header in `install.py`.
- [ ] `scripts/AGENTS.md` — a "Fixed bugs" entry if you fixed one, with what the user saw.
- [ ] `charts/mlrun-ce/Chart.yaml` bumped (see the `bump` skill).

## What the tests do and do not cover

`make installer-test-golden` compares the **calls** the installer makes — argv and exit
codes — against recorded expectations, under stubbed binaries. It says nothing about what
the installer *prints*. The access-URL table shipped with garbage in the URL column through
32 green cases for exactly this reason. Anything the user reads needs a test in
`tests/installer/test_regressions.py`.

Conversely, the unit suites patch the command wrappers, so a flag that parses correctly but
never reaches helm passes all of them. That gap is what the golden suite closes. A feature
that touches both layers needs a test in both.

Nothing in CI touches a real cluster on a PR; the kind job is dispatch-only. If your change
affects uninstall, PVC handling, or hooks, say so in the PR so a human runs it live.
