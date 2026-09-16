---
name: run-tests
description: >-
  Run and extend the pytest suites for the CE installer (scripts/install.py and the
  scripts/ce_installer package). Use when a developer asks to run installer tests, check
  coverage, add a new test, or verify a change to the installer didn't break existing
  behaviour.
---

# CE installer test suite

Tests live in `tests/installer/` and run under pytest. Nothing here contacts a cluster:
the unit suites patch the `helm`/`kubectl` wrappers, and the golden suite puts stub
binaries on `PATH`.

These cover the installer only. The chart's own tests are the other scripts in `tests/`
(`helm-template-test.sh`, `kind-test.sh`) plus `make helm-lint` — unrelated to this suite.

## Prerequisites

None beyond `uv`. Every target resolves its own dependencies; there is no virtualenv to
create and nothing to `pip install`.

## Run everything

```bash
make installer-test
```

That is two suites, and they fail for different reasons:

| Target | What it runs | A failure means |
|---|---|---|
| `make installer-test-unit` | `tests/installer/test_*.py` except the golden one | a function computes or prints the wrong thing |
| `make installer-test-golden` | `tests/installer/test_golden_argv.py` | what the installer *does to a cluster* changed |

Run one file, or one test, the usual way:

```bash
uv run --isolated --with pytest --with hatchling --with-editable ./scripts \
  pytest tests/installer/test_validators.py -q
uv run --isolated --with pytest --with hatchling --with-editable ./scripts \
  pytest tests/installer -k "otel" -q
```

Take the dependencies from `--with-editable ./scripts` rather than listing them by hand.
A hand-written list drifts from `scripts/pyproject.toml`: the Makefile's used to, omitting
`click`, which `cli.py` imports directly — it only appeared to work because an older typer
pulled click in transitively.

## The two kinds of test, and which one you want

**Unit suites** import a module, patch its command wrappers, call one function, and assert
on the result or the calls made. Fast (the whole set is well under a second) and precise.
Most new tests belong here.

| File | Covers |
|---|---|
| `test_cli.py` | commands, flag parsing, the argv pre-parse, env precedence, `--enable-otel` folding, run order, log output |
| `test_config.py` | `ce-config.yaml` parsing, required-field checks, never overriding a flag |
| `test_validators.py` | every pre-install check, and crucially which ones block versus warn |
| `test_cluster.py` | chart-source resolution, dependency handling, namespaces, `KUBE_CONTEXT` injection |
| `test_registry.py` | the pull secret, password file handling |
| `test_regressions.py` | one named test per entry in `scripts/AGENTS.md`'s "Fixed bugs" |

**The golden suite** runs the whole installer as a subprocess with stubs on `PATH` and
compares every `helm`/`kubectl`/`docker` call against a recorded file in
`tests/installer/golden/`. It catches what unit tests structurally cannot: a flag that
parses correctly but never reaches helm, a step that runs in the wrong order, an argument
dropped between layers.

These expectations were generated while the bash installer still existed and were confirmed
identical to its behaviour across all 32 cases, so they carry the authority the old
differential harness did.

### When the golden suite fails

Read the diff before doing anything else. If the change is intended:

```bash
make installer-test-golden-update
git diff tests/installer/golden/
```

Every changed line is a change in what the installer does to somebody's cluster — that diff
is the reviewable part of your PR. Re-recording without reading it defeats the whole
mechanism.

Add a case to `CASES` in `test_golden_argv.py` whenever a flag gains behaviour that reaches
helm or kubectl. Refusals belong there too: how the installer declines is as much a contract
as how it succeeds.

## Shared fixtures

Defined in `tests/installer/conftest.py`; do not redefine them locally.

| Fixture | Use |
|---|---|
| `settings` | a `Settings` with every field pinned, so nothing depends on your shell |
| `recorder` | the `Recorder` class — records argv, replays canned `Result`s by argv substring |
| `capture_logs` | `logs = capture_logs(validators)` collects log text without rich formatting |
| `clean_env` | autouse; scrubs every installer env var before each test |

`clean_env` is not optional politeness. A developer who exported `KUBE_CONTEXT` to drive a
real cluster once made a test fail in a way that looked exactly like a code regression, and
the hunt for a nonexistent bug cost more than the test was worth.

## Adding a test

1. Pick the file matching the module you changed.
2. Name it after the **symptom a user would report**, not the function under test:
   `test_node_capacity_warns_but_never_blocks`, not `test_validate_node_capacity_2`. When it
   fails in two years, the name should say what broke.
3. Use the shared fixtures.
4. Comments explain *why* a case matters or what it caught. Never restate the code.
5. **Verify the test can fail.** Reintroduce the bug in the source, confirm red, restore:

   ```bash
   # edit scripts/ce_installer/validators.py to break the behaviour
   uv run --isolated --with pytest --with hatchling --with-editable ./scripts \
     pytest tests/installer/test_validators.py -k your_test -q   # must FAIL
   git checkout scripts/ce_installer/validators.py
   ```

   This is not ceremony. Writing this suite caught a test asserting on `prompt_or_env` that
   passed whether or not the behaviour it named was present, because the value it checked
   was falsy either way.

### Traps specific to this codebase

**Patch the module under test, not `shell`.** Every module does `from .shell import kubectl`
and holds its own reference, so patching `shell.kubectl` changes nothing:

```python
monkeypatch.setattr(validators, "kubectl", recorder)   # right
monkeypatch.setattr(shell, "kubectl", recorder)        # silently does nothing
```

**`docker_available()` is `lru_cache`d.** Call `shell.docker_available.cache_clear()` or
patch it on the module under test, or one test's answer leaks into the next.

**Errors are exceptions, not exit codes.** `die()` *returns* the exception so call sites read
`raise die(...)`. Assert with `pytest.raises(InstallerError)` and check `.code`.

**No `from __future__ import annotations`, no `str | None`.** The installer supports Python
3.9 and typer resolves annotations at runtime; postponed annotations break its option
parsing in ways the error message does not explain.

**Blocking versus warning is the thing to assert.** A validator that starts blocking when it
should warn makes the installer refuse a cluster that works fine. Assert the return value,
not just the log text.

## Linting

```bash
make installer-lint-python   # check
make installer-format        # fix in place
```

`tests/installer` sits outside `scripts/`, so the targets pass
`--config scripts/pyproject.toml` explicitly — at the repo root ruff falls back to its
defaults and disagrees about line length.
