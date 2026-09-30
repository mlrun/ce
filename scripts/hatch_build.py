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
"""Build hooks that carry the chart's version into the installed installer.

`install.py version` reads charts/mlrun-ce/Chart.yaml from the checkout it sits in, which
works from a clone and not otherwise: the wheel contains only the ce_installer package, so
an installed copy has no chart to read and used to report "unknown" — including for
`uvx --from "git+...#subdirectory=scripts"`, the install the README recommends to users
without a clone. A hardcoded version here would fix that by introducing exactly the second
number the chart-reading was meant to avoid.

So the version is read from Chart.yaml at build time instead. uv clones the whole repo
before building the scripts/ subdirectory, so the chart is there to read. Two hooks, one
source:

  metadata hook -> the distribution version (PEP 440, so 0.12.0-rc.12 becomes 0.12.0rc12)
  build hook    -> ce_installer/_chart_version.py, the literal chart string to display

Building without the chart present (an sdist of scripts/ alone) degrades to the old
behaviour rather than failing: the distribution reports 0.0.0 and `version` says unknown.
"""

import pathlib
import re

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface

FALLBACK_VERSION = "0.0.0"
GENERATED = "_chart_version.py"


def read_chart_version(root):
    """The chart's version string verbatim, or "" when there is no chart to read."""
    chart_yaml = pathlib.Path(root).parent / "charts" / "mlrun-ce" / "Chart.yaml"
    if not chart_yaml.is_file():
        return ""
    match = re.search(r"^version:\s*(\S+)", chart_yaml.read_text(), re.MULTILINE)
    return match.group(1) if match else ""


def to_pep440(chart_version):
    """Convert a chart version to one Python packaging accepts.

    Helm uses SemVer, where a pre-release is `0.12.0-rc.12`; PEP 440 spells the same thing
    `0.12.0rc12` and rejects the SemVer form outright. It also has its own names for the
    phases — `a` and `b`, not `alpha` and `beta` — so the word is translated, not just
    reattached. Only the shapes this chart actually uses are handled; anything else falls
    back rather than guessing, since a version that is wrong silently misidentifies what a
    user is running, which is worse than one that is plainly absent.
    """
    if not chart_version:
        return FALLBACK_VERSION
    phases = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}
    normalised = re.sub(
        r"-(alpha|beta|rc|a|b)\.?(\d+)$",
        lambda m: f"{phases[m.group(1)]}{m.group(2)}",
        chart_version,
    )
    if not re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", normalised):
        return FALLBACK_VERSION
    return normalised


class ChartVersionMetadataHook(MetadataHookInterface):
    def update(self, metadata):
        metadata["version"] = to_pep440(read_chart_version(self.root))


class ChartVersionBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        chart_version = read_chart_version(self.root)
        if not chart_version:
            return
        generated = pathlib.Path(self.root) / "ce_installer" / GENERATED
        generated.write_text(
            '"""Generated at build time by hatch_build.py. Not edited by hand."""\n\n'
            f'CHART_VERSION = "{chart_version}"\n'
        )
        # Written into the source tree so the wheel picks it up with the rest of the
        # package, then removed in finalize() so a build never leaves the checkout dirty.
        build_data["artifacts"].append(f"/ce_installer/{GENERATED}")

    def finalize(self, version, build_data, artifact_path):
        generated = pathlib.Path(self.root) / "ce_installer" / GENERATED
        if generated.is_file():
            generated.unlink()
