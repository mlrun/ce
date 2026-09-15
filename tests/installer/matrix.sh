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
# Drives tests/installer/difftest.sh over the invocations worth pinning while install.sh
# and install.py ship side by side. Run via `make installer-test-diff`.
#
# Add a case here whenever a flag gains behaviour that reaches helm or kubectl. Cases that
# are expected to fail (bad values, missing arguments) belong here too: the two scripts
# must agree on how they refuse, not only on how they succeed.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CASES=(
    # Baseline and component toggles
    "--dry-run"
    "--dry-run --disable-spark"
    "--dry-run --disable-mpi"
    "--dry-run --disable-model-monitoring"
    "--dry-run --disable-system-monitoring"
    "--dry-run --disable-spark --disable-mpi --disable-model-monitoring --disable-system-monitoring"

    # otel: the four-state fold, where left-to-right order is the whole point
    "--dry-run --enable-otel"
    "--dry-run --enable-otel off"
    "--dry-run --enable-otel collector"
    "--dry-run --enable-otel full"
    "--dry-run --enable-otel collector --enable-otel-instrumentation"
    "--dry-run --enable-otel-instrumentation --enable-otel collector"
    "--dry-run --enable-otel off --enable-otel-operator"
    "--dry-run --enable-otel-operator --enable-otel-collector"

    # Ingress, with and without an explicit class
    "--dry-run --enable-ingress"
    "--dry-run --enable-ingress traefik"

    # Local registry, which changes the registry URL and the secret's contents
    "--dry-run --local-registry"
    "--dry-run --local-registry --enable-ingress"

    # Secret and validator skips
    "--dry-run --skip-secret"
    "--dry-run --skip-validators"
    "--dry-run --skip-secret --skip-validators"

    # Chart selection
    "--dry-run --ce-version 0.11.0"

    # Uninstall, including the destructive path
    "uninstall"
    "uninstall --hard-clean"

    # Refusals: both must reject these the same way
    "--hard-clean"
    "badverb"
    "--chart-path"
    "--enable-otel bogus"
)

pass=0
fail=0
failed_cases=()

for args in "${CASES[@]}"; do
    # Unquoted on purpose: each case is a pre-split argument string.
    # shellcheck disable=SC2086
    if output="$("${here}/difftest.sh" ${args} 2>&1)"; then
        pass=$((pass + 1))
        printf 'ok    %-70s %s\n' "${args}" "${output##*$'\n'}"
    else
        fail=$((fail + 1))
        failed_cases+=("${args}")
        printf 'FAIL  %s\n' "${args}"
        sed 's/^/          /' <<<"${output}"
    fi
done

printf '\n%d/%d identical\n' "${pass}" "$((pass + fail))"
if ((fail > 0)); then
    printf 'diverged:\n'
    printf '  %s\n' "${failed_cases[@]}"
    exit 1
fi
