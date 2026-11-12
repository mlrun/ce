# Copyright 2025 Iguazio
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
"""MLRun CE installer.

Module layout, in dependency order — each one imports only from the ones above it:

    console      rich consoles, log_info/warn/error, InstallerError, die
    settings     built-in defaults, env_str/env_true, the Settings dataclass, prompting
    shell        run/stream, the KUBE_CONTEXT-aware kubectl/helm wrappers, prerequisites
    config       the ce-config.yaml 'installer:' block
    cluster      namespace, external host address, chart source resolution
    registry     pull secret, the optional in-cluster registry, the CoreDNS patch
    validators   pre-install checks, blocking and advisory
    ui           the live progress table and the access-URL table
    helm_ops     --set composition, install, uninstall, hard clean
    cli          installer version, argv pre-parse, the run order, the typer command

scripts/install.py is only a launcher: it carries the PEP 723 metadata, re-execs under uv
when the dependencies are missing, and calls main() below.
"""

from .cli import main

__all__ = ["main"]
