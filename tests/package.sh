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
import json, tarfile, os, shutil, tempfile

tgz = "charts/opentelemetry-operator-0.78.1.tgz"
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
    import subprocess
    subprocess.run(
        ["tar", "czf", os.path.abspath(tgz), "opentelemetry-operator"],
        cwd=tmp, env=env, check=True
    )
PYEOF

# Slim down the opentelemetry-operator sub-chart by replacing large conf/crds/ files
# with empty stubs. The CRDs are managed by the parent chart's crds/ directory instead
# (crds.create: false in values.yaml). Keeping the full 542 KB CRD files would push
# the Helm release Secret over the Kubernetes 3 MB API request limit.
echo "Slimming opentelemetry-operator conf/crds/ (replacing with empty stubs)..."
python3 - <<'PYEOF'
import tarfile, os, shutil, tempfile, io

tgz = "charts/opentelemetry-operator-0.78.1.tgz"
if not os.path.exists(tgz):
    print(f"  {tgz} not found, skipping")
    exit(0)

# Stub content: preserves the {{- if .Values.crds.create }} guard so the template
# renders correctly (empty output) whether crds.create is true or false.
STUB = b"{{- if .Values.crds.create }}\n{{- end }}\n"

crd_files = {
    "opentelemetry-operator/conf/crds/crd-opentelemetrycollector.yaml",
    "opentelemetry-operator/conf/crds/crd-opentelemetryinstrumentation.yaml",
    "opentelemetry-operator/conf/crds/crd-opentelemetry.io_opampbridges.yaml",
}

with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(tgz, "r:gz") as t:
        t.extractall(tmp)

    for rel in crd_files:
        path = os.path.join(tmp, rel)
        if os.path.exists(path):
            orig = os.path.getsize(path)
            with open(path, "wb") as f:
                f.write(STUB)
            print(f"  {os.path.basename(rel)}: {orig} -> {len(STUB)} bytes")
        else:
            print(f"  {rel} not found, skipping")

    import subprocess, os as _os
    env = _os.environ.copy()
    env["COPYFILE_DISABLE"] = "1"
    subprocess.run(
        ["tar", "czf", os.path.abspath(tgz), "opentelemetry-operator"],
        cwd=tmp, env=env, check=True
    )
PYEOF

# Create MLRun CE tarball
helm package .
exit 0
