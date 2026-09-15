#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "typer>=0.12",
#   "click>=8",
#   "rich>=13",
#   "pyyaml>=6",
# ]
# ///
#
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
#
# Interactive installer for MLRun CE on local Kubernetes. Creates a Docker registry secret
# from your credentials, then installs the chart with your local URL and registry URL.
#
# This file is only the entry point; the implementation lives in the ce_installer package
# beside it. See ce_installer/__init__.py for the module layout.
#
# From a clone of this repo (installs the published chart):
#   ./scripts/install.py
#
# From a clone, installing this repo's own chart on the current branch:
#   ./scripts/install.py --chart-path ./charts/mlrun-ce
#
# Without a clone — pin a release tag, see https://github.com/mlrun/ce/releases;
# "development" also works but moves with every merge:
#   uvx --from "git+https://github.com/mlrun/ce@mlrun-ce-0.12.0-rc.12#subdirectory=scripts" \
#     mlrun-ce-installer install
#
# Commands: install (the default), uninstall, version, help. Flags may be passed with no
# command at all, so every invocation above still means the same thing without one.
#
# Non-interactive (CI): set REGISTRY_* and EXTERNAL_HOST_ADDRESS, REGISTRY_URL; see -h.
# Requirements: uv, helm, kubectl (configured with a cluster). docker is optional — it is
# only used for the best-effort registry-auth validator.

import os
import shutil
import sys
from pathlib import Path


def _bootstrap() -> None:
    """Re-exec under uv when the third-party deps are missing, so any entry point works.

    Keeps `python3 install.py` and a bare `./install.py` on a box without the deps working
    identically to `uv run install.py`, without duplicating the CLI in argparse. Stdlib-only
    on purpose: it has to run before the imports it is protecting.
    """
    try:
        import rich  # noqa: F401
        import typer  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        pass
    else:
        return

    if shutil.which("uv") is None:
        sys.exit(
            "mlrun-ce-installer needs uv (or typer, rich and pyyaml on the current "
            "interpreter).\nInstall uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    os.execvp("uv", ["uv", "run", "--script", os.path.abspath(__file__), *sys.argv[1:]])


if __name__ == "__main__":
    _bootstrap()
    # resolve() first: `make installer-link` puts this command on PATH as a symlink into a
    # checkout, and the link's own directory has no ce_installer package next to it.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from ce_installer import main

    sys.exit(main())
