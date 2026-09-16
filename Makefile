# Copyright 2022 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Set the default shell to bash instead of sh
SHELL := bash

HELM_LINT_DEFAULT_BRANCH ?= development

# Set the default target to help
.DEFAULT_GOAL := help

.PHONY: help
help: ## Display available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

# Not a test suite despite the name, and not hermetic: tests/run.sh runs a real
# `helm install --wait --timeout 960s` into whatever cluster kubectl currently points at,
# with no assertions and no teardown. It was written as the body of ci.yaml's `test:` job,
# which ran it inside a throwaway kind cluster — that job is commented out ("takes too long
# to pull images"), so the only thing left that can reach this is a developer's laptop.
# ROADMAP items A1/A2 reclaim the name and split the deploy into `make integration-test`.
# Use `make installer-test` for the installer suites; they touch no cluster.
.PHONY: tests
tests: ## Deploy CE to the current cluster (real install, not a test suite — see comment)
	@./tests/run.sh

.PHONY: package
package: ## Package the application
	@./tests/package.sh

# --- Installer -----------------------------------------------------------------------
#
# scripts/install.py plus the scripts/ce_installer package beside it. Every target here
# shells out to uv, which manages its own interpreter and dependencies — there is nothing
# to pip install first, and no virtualenv to activate.
#
# --isolated is load-bearing. Without it, an activated conda base or virtualenv in the
# developer's shell satisfies the --with requirements and uv uses it as-is, so the suite
# silently runs on that interpreter and those package versions instead of a resolved set.
# That is invisible locally and does not match CI. The pinned version is the floor declared
# in scripts/pyproject.toml, so 3.9 support is exercised rather than merely asserted by
# ruff's target-version; override to check another:
#
#   make installer-test INSTALLER_PYTHON=3.13
#
# The dependencies come from scripts/pyproject.toml via --with-editable rather than a list
# of --with flags, so there is one declared set instead of two that drift. They did drift:
# the flags omitted click, which cli.py imports directly, and that went unnoticed only
# because older typer pulled click in transitively — on a newer typer the suite could not
# import the module under test.
INSTALLER_PYTHON ?= 3.9
# hatchling is the build backend, pulled in because the suite covers scripts/hatch_build.py
# — the hook that carries the chart version into an installed copy.
INSTALLER_PYTEST = uv run --isolated --python $(INSTALLER_PYTHON) --quiet \
	--with pytest --with hatchling --with-editable ./scripts pytest

.PHONY: installer-test
installer-test: installer-test-unit installer-test-golden ## Run every installer test suite

# Patch the helm/kubectl wrappers and check one function at a time: flag parsing, config
# precedence, validators, and one named test per entry in scripts/AGENTS.md's "Fixed bugs".
.PHONY: installer-test-unit
installer-test-unit: ## Run the ce_installer unit and regression tests
	@$(INSTALLER_PYTEST) tests/installer -q --ignore=tests/installer/test_golden_argv.py

# Runs the whole installer as a subprocess against stub binaries and compares the
# helm/kubectl calls it makes to tests/installer/golden/. Catches what the unit tests
# cannot: a flag that parses but never reaches helm, or a step that runs out of order.
# Never touches a cluster.
.PHONY: installer-test-golden
installer-test-golden: ## Check the installer still makes the recorded helm/kubectl calls
	@$(INSTALLER_PYTEST) tests/installer/test_golden_argv.py -q

.PHONY: installer-test-golden-update
installer-test-golden-update: ## Re-record the expectations after an intended change
	@UPDATE_GOLDEN=1 $(INSTALLER_PYTEST) tests/installer/test_golden_argv.py -q
	@echo "re-recorded — review 'git diff tests/installer/golden/' before committing:"
	@echo "every changed line is a change in what the installer does to a cluster."

.PHONY: installer-lint
installer-lint: installer-lint-python ## Lint the installer

# tests/installer lives outside scripts/, so it needs the package's ruff config passed
# explicitly — at the repo root ruff would fall back to its defaults and disagree.
.PHONY: installer-lint-python
installer-lint-python: ## Lint and format-check the Python installer and its tests
	@cd scripts && uvx ruff check .
	@cd scripts && uvx ruff format --check .
	@uvx ruff check --config scripts/pyproject.toml tests/installer
	@uvx ruff format --config scripts/pyproject.toml --check tests/installer

.PHONY: installer-format
installer-format: ## Reformat the Python installer and its tests in place
	@cd scripts && uvx ruff check --fix .
	@cd scripts && uvx ruff format .
	@uvx ruff check --config scripts/pyproject.toml --fix tests/installer
	@uvx ruff format --config scripts/pyproject.toml tests/installer

# Symlink rather than copy, so the command tracks the working tree and can still find the
# chart next to it (a copy has no chart, and reports its version as unknown).
INSTALLER_BIN_DIR ?= $(HOME)/.local/bin
INSTALLER_ENTRYPOINT ?= scripts/install.py

.PHONY: installer-link
installer-link: ## Put mlrun-ce-installer on PATH, pointing at this checkout
	@mkdir -p "$(INSTALLER_BIN_DIR)"
	@ln -sf "$(CURDIR)/$(INSTALLER_ENTRYPOINT)" "$(INSTALLER_BIN_DIR)/mlrun-ce-installer"
	@echo "linked $(INSTALLER_BIN_DIR)/mlrun-ce-installer -> $(CURDIR)/$(INSTALLER_ENTRYPOINT)"
	@case ":$$PATH:" in \
		*":$(INSTALLER_BIN_DIR):"*) ;; \
		*) echo "note: $(INSTALLER_BIN_DIR) is not on PATH — add it, or set INSTALLER_BIN_DIR" ;; \
	esac

.PHONY: installer-unlink
installer-unlink: ## Remove the mlrun-ce-installer symlink
	@rm -f "$(INSTALLER_BIN_DIR)/mlrun-ce-installer"
	@echo "removed $(INSTALLER_BIN_DIR)/mlrun-ce-installer"

.PHONY: helm-lint
helm-lint: helm-repo-add ## Lint Helm Chart
	@helm lint charts/mlrun-ce
	@ct lint --target-branch $(HELM_LINT_DEFAULT_BRANCH) --validate-maintainers=false --helm-extra-args "--timeout 600s"

.PHONY: helm-update-dependencies
helm-update-dependencies:  ## Update Helm Chart dependencies
	@helm dependency update charts/mlrun-ce


.PHONY: helm-repo-add
helm-repo-add: ## Add Chart helm dependency repositories
	@helm dependency list charts/mlrun-ce 2> /dev/null |\
    	tail +2 |\
     	awk 'NR>1{print l}{l=$$0}' |\
      	awk '{ print "helm repo add " $$1 " " $$3 }' |\
       	while read cmd; do $$cmd; done
