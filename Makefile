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

.PHONY: installer-test
installer-test: ## Run the scripts/install.sh unit tests (requires bats-core)
	@bats tests/install_tests.bats

.PHONY: installer-lint
installer-lint: ## Syntax-check and shellcheck scripts/install.sh
	@bash -n scripts/install.sh
	@shellcheck scripts/install.sh

# Symlink rather than copy, so the command tracks the working tree and can still find the
# chart next to it (a copy has no chart, and reports its version as unknown).
INSTALLER_BIN_DIR ?= $(HOME)/.local/bin

.PHONY: installer-link
installer-link: ## Put mlrun-ce-installer on PATH, pointing at this checkout
	@mkdir -p "$(INSTALLER_BIN_DIR)"
	@ln -sf "$(CURDIR)/scripts/install.sh" "$(INSTALLER_BIN_DIR)/mlrun-ce-installer"
	@echo "linked $(INSTALLER_BIN_DIR)/mlrun-ce-installer -> $(CURDIR)/scripts/install.sh"
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
