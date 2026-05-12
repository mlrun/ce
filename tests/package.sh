#!/usr/bin/env bash
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

set -o errexit
set -o nounset
set -o pipefail

dirname=$(dirname $0)

# Install chart dependencies
echo "Installing chart dependencies"
cd "$dirname"/../charts/mlrun-ce
helm dependency update

# Patch opentelemetry-operator sub-chart schema: the upstream chart has
# "examples": "" (string) for featureGates, but JSON Schema requires an array.
# Helm v4 enforces metaschema validation strictly and rejects the install otherwise.
echo "Patching opentelemetry-operator schema (featureGates.examples string -> array)..."
python3 - <<'PYEOF'
import json, tarfile, os, shutil, tempfile, yaml, subprocess

# Read version dynamically from requirements.yaml so this doesn't silently break on bumps
with open("requirements.yaml") as f:
    deps = yaml.safe_load(f)["dependencies"]
version = next(d["version"] for d in deps if d["name"] == "opentelemetry-operator")
tgz = f"charts/opentelemetry-operator-{version}.tgz"

if not os.path.exists(tgz):
    print(f"  {tgz} not found, skipping patch")
    exit(0)

with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(tgz, "r:gz") as t:
        t.extractall(tmp)
    schema_path = os.path.join(tmp, "opentelemetry-operator", "values.schema.json")
    with open(schema_path) as f:
        schema = json.load(f)
    fg = schema["properties"]["manager"]["properties"]["featureGates"]
    if isinstance(fg.get("examples"), str):
        fg["examples"] = [fg["examples"]]
        with open(schema_path, "w") as f:
            json.dump(schema, f, indent=2)
        print("  Patched featureGates.examples")
    else:
        print("  Already correct, no patch needed")
    # Repack without macOS metadata
    env = os.environ.copy()
    env["COPYFILE_DISABLE"] = "1"
    subprocess.run(
        ["tar", "czf", os.path.abspath(tgz), "opentelemetry-operator"],
        cwd=tmp, env=env, check=True
    )
PYEOF

# NOTE: CRD slimming step was removed.
# Previously this step replaced conf/crds/ templates with empty stubs (crds.create: false).
# We now use Option B: crds.create: true so the operator sub-chart manages CRD lifecycle.
# The full CRD YAML (~1.6 MB) compresses to ~160 KB gzipped — well within the 3 MB
# Kubernetes API request limit for the Helm release Secret.

# Create MLRun CE tarball
helm package .
exit 0
