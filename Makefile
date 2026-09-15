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

.PHONY: tests
tests: ## Run tests
	@./tests/run.sh

.PHONY: package
package: ## Package the application
	@./tests/package.sh

# --- Installer -----------------------------------------------------------------------
#
# The installer is being ported from bash (scripts/install.sh) to uv + Python
# (scripts/install.py plus the scripts/ce_installer package). Both ship during the
# overlap and both are covered here; see scripts/AGENTS.md for the cutover plan.
#
# The Python targets shell out to uv, which manages its own interpreter and dependencies —
# there is nothing to pip install first.

.PHONY: installer-test
installer-test: installer-test-python installer-test-bash installer-test-diff ## Run every installer test suite

# One named test per entry in scripts/AGENTS.md's "Fixed bugs", plus the output formatting
# the differential suite cannot see — it compares the calls two implementations make, not
# what they print, so the access-URL table shipped broken through 28 green matrix cases.
.PHONY: installer-test-python
installer-test-python: ## Run the ce_installer regression tests
	@uv run --quiet --with pytest --with rich --with typer --with pyyaml \
		pytest tests/installer -q

.PHONY: installer-test-bash
installer-test-bash: ## Run the scripts/install.sh unit tests (requires bats-core)
	@bats tests/install_tests.bats

# Runs both installers over the same invocations with stubs standing in for
# helm/kubectl/docker, and fails if their recorded calls differ. Never touches a cluster.
.PHONY: installer-test-diff
installer-test-diff: ## Diff install.sh against install.py over the flag matrix
	@bash tests/installer/matrix.sh

.PHONY: installer-lint
installer-lint: installer-lint-bash installer-lint-python ## Lint both installers

.PHONY: installer-lint-bash
installer-lint-bash: ## Syntax-check and shellcheck scripts/install.sh
	@bash -n scripts/install.sh
	@shellcheck scripts/install.sh

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

# Which implementation `make installer-link` puts on PATH. Flip to install.sh to go back to
# the bash one without unlinking first.
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
