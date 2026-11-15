---
name: run-tests
description: >-
  Run and extend the pytest suites for the CE installer (scripts/install.py and the
  scripts/ce_installer package). Use when a developer asks to run installer tests, check
  coverage, add a new test, or verify a change to the installer didn't break existing
  behaviour.
---

# CE installer tests

Tests live in `tests/installer/` and need only `uv`. None of them contacts a cluster: the
unit suites patch the `helm`/`kubectl` wrappers and the golden suite puts stub binaries on
`PATH`. The chart's own checks (`make helm-lint`, `tests/helm-template-test.sh`,
`tests/kind-test.sh`) are separate.

## Running

```bash
KUBECONFIG=/nonexistent make installer-test              # both suites, Python 3.9
make installer-test-unit INSTALLER_PYTHON=3.13           # CI also runs 3.13
make installer-lint-python                               # ruff check + format check
make installer-format                                    # fix in place
```

`KUBECONFIG=/nonexistent` makes any call that slips past the stubs fail instead of reaching
the cluster your shell points at. 3.9 is the supported floor; typer and click resolve to
different versions on 3.9 and 3.13, so a change can pass on one and fail on the other.

One file or one test:

```bash
uv run --isolated --python 3.9 --with pytest --with hatchling --with-editable ./scripts \
  pytest tests/installer/test_validators.py -q -k otel
```

Take dependencies from `--with-editable ./scripts`, not a hand-written `--with` list, so they
match `scripts/pyproject.toml`. `--isolated` stops an activated virtualenv from supplying
them.

## The two suites

| Target | Runs | A failure means |
|---|---|---|
| `make installer-test-unit` | every `tests/installer/test_*.py` except the golden one | a function computes or prints the wrong thing |
| `make installer-test-golden` | `tests/installer/test_golden_argv.py` | what the installer does to a cluster changed |

| Unit file | Covers |
|---|---|
| `test_cli.py` | commands, flag parsing, the argv pre-parse, precedence, run order, the README Reference |
| `test_config.py` | `ce-config.yaml` parsing, the strict key check, both example files, a key per flag, and required fields |
| `test_validators.py` | each pre-install check, and whether it blocks or warns |
| `test_cluster.py` | chart source, dependencies, namespaces, `KUBE_CONTEXT` |
| `test_registry.py` | the pull secret and password handling |
| `test_regressions.py` | one test per bug that reached a user, and printed output |

### The golden contract

`test_golden_argv.py` runs the whole installer as a subprocess for each entry in `CASES`,
with `tests/installer/stub.py` standing in for `helm`, `kubectl`, `docker` and `minikube`.
Each file in `tests/installer/golden/` records every call the installer makes, its exit
code, and the installer's values file. The recordings are the specification of what the
installer does to a cluster; they do not cover what it prints.

When the suite fails, read the diff first. If the change is intended:

```bash
make installer-test-golden-update
git diff tests/installer/golden/
```

Review every changed line and explain it in the PR. Add a case whenever a flag gains
behaviour that reaches helm or kubectl, including refusals. The stub answers from its
arguments and from `STUB_*` variables; `run_case` drops any `STUB_*` from your shell.

## Adding a unit test

1. Put it in the file for the module you changed.
2. Name it for the symptom a user would report (`test_node_capacity_warns_but_never_blocks`).
3. Use the shared fixtures from `conftest.py`: `settings` (every field pinned), `recorder`
   (records argv, replays `Result`s by argv substring), `capture_logs`, and the autouse
   `clean_env`, which unsets every installer variable. Do not read `os.environ` in a test.
4. Make sure it can fail: reintroduce the bug, see it fail, restore the code.

## Traps

- **Patch the module under test, not `shell`.** Each module does `from .shell import
  kubectl` and keeps its own reference: `monkeypatch.setattr(validators, "kubectl", rec)`.
- **`shell.docker_available()` is `lru_cache`d.** Clear it or patch it on the module under
  test.
- **`die()` returns the exception** (`raise die(...)`). Assert with
  `pytest.raises(InstallerError)` and check `.code`.
- **For a validator, assert the return value** (block or warn), not only the log text.
- **`tests/installer` is outside `scripts/`**, so ruff needs `--config scripts/pyproject.toml`
  there; the make targets pass it.
