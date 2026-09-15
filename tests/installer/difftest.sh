#!/usr/bin/env bash
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
# Runs scripts/install.sh and scripts/install.py over ONE invocation under identical
# stubbed conditions, and fails if they disagree on either the exit code or the sequence of
# helm/kubectl/docker calls they make.
#
# The argv log is the contract. Whatever the two print, they must ask the cluster for the
# same things in the same order — that is what makes the port a port rather than a rewrite.
# Real binaries are never invoked and no cluster is contacted: stub.py is symlinked onto a
# temporary PATH as helm, kubectl, docker and minikube.
#
# Usage:  tests/installer/difftest.sh [installer flags...]
#         VERBOSE=1 tests/installer/difftest.sh --dry-run    # also print both transcripts
#
# Exit:   0 identical, 1 diverged.

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

mkdir -p "${workdir}/bin"
chmod +x "${repo_root}/tests/installer/stub.py"
for name in helm kubectl docker minikube; do
    ln -s "${repo_root}/tests/installer/stub.py" "${workdir}/bin/${name}"
done
export PATH="${workdir}/bin:${PATH}"

# A complete non-interactive answer set, so neither script stops at a prompt and any
# divergence is about logic rather than about one of them asking a question.
export NON_INTERACTIVE=true
export EXTERNAL_HOST_ADDRESS=localhost
export REGISTRY_URL=index.docker.io/someone
export REGISTRY_USERNAME=someone
export REGISTRY_PASSWORD=secret
export REGISTRY_EMAIL=someone@example.com
export STUB_SC_STABLE=true

run_one() {
    local label="$1" script="$2"
    shift 2
    export STUB_LOG="${workdir}/${label}.log"
    : >"${STUB_LOG}"
    "${script}" "$@" >"${workdir}/${label}.out" 2>&1
    echo "$?" >"${workdir}/${label}.exit"
}

run_one bash "${repo_root}/scripts/install.sh" "$@"
run_one python "${repo_root}/scripts/install.py" "$@"

bash_exit="$(cat "${workdir}/bash.exit")"
python_exit="$(cat "${workdir}/python.exit")"

if [[ -n "${VERBOSE:-}" ]]; then
    for label in bash python; do
        echo "--- ${label} output (exit ${bash_exit})"
        sed 's/^/    /' "${workdir}/${label}.out"
    done
fi

status=0

if [[ "${bash_exit}" != "${python_exit}" ]]; then
    echo "exit code differs: install.sh=${bash_exit} install.py=${python_exit}"
    status=1
fi

if ! diff -u --label "install.sh" --label "install.py" \
    "${workdir}/bash.log" "${workdir}/python.log"; then
    status=1
fi

if [[ "${status}" -eq 0 ]]; then
    echo "identical: exit ${bash_exit}, $(wc -l <"${workdir}/bash.log" | tr -d ' ') calls"
fi
exit "${status}"
