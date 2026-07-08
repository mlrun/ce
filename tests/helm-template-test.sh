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
# Helm template tests for MLRun CE chart
# Validates that templates render correctly with various configurations

set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/../charts/mlrun-ce"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

TESTS_PASSED=0
TESTS_FAILED=0

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_test() { echo -e "${GREEN}[TEST]${NC} $1"; }
log_pass() { echo -e "${GREEN}[PASS]${NC} $1"; ((TESTS_PASSED++)) || true; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; ((TESTS_FAILED++)) || true; }

# Render a specific template and return the output
render_template() {
    local template="$1"
    shift
    helm template test "${CHART_DIR}" \
        --skip-schema-validation \
        --show-only "${template}" \
        "$@" 2>/dev/null
}

# Render all templates and return the output
render_all() {
    helm template test "${CHART_DIR}" \
        --skip-schema-validation \
        "$@" 2>/dev/null
}

# Check if output contains a string
assert_contains() {
    local output="$1"
    local expected="$2"
    local test_name="$3"

    if echo "$output" | grep -q "$expected"; then
        log_pass "$test_name"
        return 0
    else
        log_fail "$test_name - expected to find: $expected"
        return 1
    fi
}

# Check if output does NOT contain a string
assert_not_contains() {
    local output="$1"
    local not_expected="$2"
    local test_name="$3"

    if echo "$output" | grep -q "$not_expected"; then
        log_fail "$test_name - should not contain: $not_expected"
        return 1
    else
        log_pass "$test_name"
        return 0
    fi
}

# Check if template renders (non-empty output)
assert_renders() {
    local output="$1"
    local test_name="$2"

    if [[ -n "$output" ]]; then
        log_pass "$test_name"
        return 0
    else
        log_fail "$test_name - template produced no output"
        return 1
    fi
}

# Check if template does NOT render (empty output or error)
assert_not_renders() {
    local template="$1"
    local test_name="$2"
    shift 2

    local output
    output=$(render_template "$template" "$@" 2>&1) || true

    if [[ -z "$output" ]] || echo "$output" | grep -q "could not find template"; then
        log_pass "$test_name"
        return 0
    else
        log_fail "$test_name - template should not render"
        return 1
    fi
}

# ============================================================================
# OpenTelemetry Tests
# ============================================================================

test_otel_collector_default() {
    log_test "OpenTelemetry Collector - Enabled (via CRD Readiness Job)"

    local output
    # The collector CR is now created by the otel-cr-installer, not directly
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true)

    assert_renders "$output" "CRD Readiness Job renders"
    assert_contains "$output" "kind: Job" "Has correct kind"
    assert_contains "$output" "kind: OpenTelemetryCollector" "Job contains OpenTelemetryCollector CR"
    assert_contains "$output" "mode: deployment" "Uses deployment mode"
    assert_contains "$output" "otlphttp/prometheus:" "Has OTLP HTTP Prometheus exporter"
    assert_contains "$output" "/api/v1/otlp" "Pushes to Prometheus OTLP endpoint"
    assert_contains "$output" "otlp:" "Has OTLP receiver"
    assert_contains "$output" "helm.sh/hook" "Has Helm hooks"
    assert_contains "$output" "post-install,post-upgrade" "Has correct hook triggers"
    assert_contains "$output" "upgradeStrategy: none" "Has upgradeStrategy"
    assert_contains "$output" "managementState: managed" "Has managementState"
}

test_otel_collector_disabled() {
    log_test "OpenTelemetry Collector - Disabled (default)"

    # When disabled, the otel-cr-installer should not render
    assert_not_renders "templates/opentelemetry/otel-cr-installer.yaml" \
        "CRD Readiness Job does not render when collector disabled (default)"
}

test_otel_collector_upgrade_strategy() {
    log_test "OpenTelemetry Collector - upgradeStrategy override"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.collector.upgradeStrategy=none)

    assert_contains "$output" "upgradeStrategy: none" "upgradeStrategy can be overridden to none"
}

test_otel_collector_resources() {
    log_test "OpenTelemetry Collector - Custom resources"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.collector.resources.requests.cpu=100m \
        --set opentelemetry.collector.resources.requests.memory=128Mi \
        --set opentelemetry.collector.resources.limits.cpu=500m \
        --set opentelemetry.collector.resources.limits.memory=512Mi)

    assert_contains "$output" "cpu: 100m" "Custom CPU request"
    assert_contains "$output" "memory: 128Mi" "Custom memory request"
    assert_contains "$output" "cpu: 500m" "Custom CPU limit"
    assert_contains "$output" "memory: 512Mi" "Custom memory limit"
}

test_otel_instrumentation_default() {
    log_test "OpenTelemetry Instrumentation - Enabled (via CRD Readiness Job)"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.instrumentation.enabled=true)

    assert_renders "$output" "CRD Readiness Job renders for Instrumentation"
    assert_contains "$output" "kind: Instrumentation" "Job contains Instrumentation CR"
    assert_contains "$output" "tracecontext" "Has tracecontext propagator"
    assert_contains "$output" "baggage" "Has baggage propagator"
    assert_contains "$output" "parentbased_traceidratio" "Has sampler type"
    assert_contains "$output" "python:" "Has Python instrumentation"
    assert_contains "$output" "autoinstrumentation-python" "Uses Python auto-instrumentation image"
}

test_otel_instrumentation_disabled() {
    log_test "OpenTelemetry Instrumentation - Disabled (default)"

    # When both collector and instrumentation are disabled, the job should not render
    assert_not_renders "templates/opentelemetry/otel-cr-installer.yaml" \
        "CRD Readiness Job does not render when instrumentation disabled (default)"
}

test_otel_instrumentation_java_enabled() {
    log_test "OpenTelemetry Instrumentation - Java enabled"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.instrumentation.enabled=true \
        --set opentelemetry.instrumentation.java.enabled=true)

    assert_contains "$output" "java:" "Has Java instrumentation section"
    assert_contains "$output" "autoinstrumentation-java" "Uses Java auto-instrumentation image"
}

test_otel_rbac_default() {
    log_test "OpenTelemetry RBAC - Enabled"

    local output
    output=$(render_template "templates/opentelemetry/rbac.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true)

    assert_renders "$output" "RBAC renders"
    assert_contains "$output" "kind: ServiceAccount" "Has ServiceAccount"
    assert_contains "$output" "kind: Role" "Has Role"
    assert_contains "$output" "kind: RoleBinding" "Has RoleBinding"
    assert_contains "$output" "name: otel-collector" "Has correct name"
    assert_contains "$output" "kind: ClusterRole" "Has ClusterRole for CRD access"
    assert_contains "$output" "otel-cr-creator" "Has CR creator ServiceAccount"
}

test_otel_rbac_disabled() {
    log_test "OpenTelemetry RBAC - Disabled (default)"

    assert_not_renders "templates/opentelemetry/rbac.yaml" \
        "RBAC does not render when OTEL disabled (default)"
}

test_jupyter_otel_labels() {
    log_test "Jupyter Deployment - No OTel injection even when OTel enabled (per design)"

    local output
    output=$(render_template "templates/jupyter-notebook/deployment.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.instrumentation.enabled=true)

    assert_not_contains "$output" 'mlrun.io/otel' "No OTel pod label (Jupyter not auto-instrumented)"
    assert_not_contains "$output" "sidecar.opentelemetry.io/inject:" "No sidecar annotation (deployment mode)"
    assert_not_contains "$output" "prometheus.io/scrape:" "No per-pod Prometheus annotation (collector scrapes)"
}

test_jupyter_no_otel_label_when_disabled() {
    log_test "Jupyter Deployment - No OTel label when disabled (default)"

    local output
    output=$(render_template "templates/jupyter-notebook/deployment.yaml" \
        --set global.registry.url=test.io)

    assert_not_contains "$output" 'mlrun.io/otel' "No OTel label when disabled (default)"
}

# ============================================================================
# Admin/Non-Admin Installation Tests
# ============================================================================

test_admin_values_otel() {
    log_test "Admin installation - OTEL operator enabled, CRs disabled"

    # CRD readiness job should not render when CRs are disabled
    assert_not_renders "templates/opentelemetry/otel-cr-installer.yaml" \
        "CRD Readiness Job not rendered with admin values" \
        -f "${CHART_DIR}/admin_installation_values.yaml"
}

test_non_admin_values_otel() {
    log_test "Non-admin installation - OTEL CRs enabled"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        -f "${CHART_DIR}/non_admin_installation_values.yaml")

    assert_renders "$output" "CRD Readiness Job renders with non-admin values"
    assert_contains "$output" "kind: OpenTelemetryCollector" "Has Collector CR"
    assert_contains "$output" "kind: Instrumentation" "Has Instrumentation CR"
}

test_namespace_label_enabled() {
    log_test "Namespace Label - Enabled"

    local output
    output=$(render_template "templates/opentelemetry/namespace-label.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.namespaceLabel.enabled=true \
        --set opentelemetry.collector.enabled=true)

    assert_renders "$output" "Namespace label job renders"
    assert_contains "$output" "kind: Job" "Has correct kind (Job)"
    assert_contains "$output" "helm.sh/hook" "Has post-install hook annotation"
    assert_contains "$output" "kubectl label namespace" "Has kubectl label command"
    assert_contains "$output" "opentelemetry.io/inject" "Has OTEL inject label key"
}

test_namespace_label_disabled() {
    log_test "Namespace Label - Disabled (default)"

    assert_not_renders "templates/opentelemetry/namespace-label.yaml" \
        "Namespace label not rendered when disabled (default)"
}

test_admin_namespace_label_disabled() {
    log_test "Admin installation - Namespace label disabled"

    assert_not_renders "templates/opentelemetry/namespace-label.yaml" \
        "Namespace label not rendered with admin values" \
        -f "${CHART_DIR}/admin_installation_values.yaml"
}

test_non_admin_namespace_label_enabled() {
    log_test "Non-admin installation - Namespace label enabled"

    local output
    output=$(render_template "templates/opentelemetry/namespace-label.yaml" \
        --set global.registry.url=test.io \
        -f "${CHART_DIR}/non_admin_installation_values.yaml")

    assert_renders "$output" "Namespace label job renders with non-admin values"
    assert_contains "$output" "opentelemetry.io/inject" "Has OTEL inject label"
}

test_otel_operator_namespace_selector() {
    log_test "OTEL Operator - Namespace selector configured"

    local output
    output=$(render_all \
        --set global.registry.url=test.io \
        --set opentelemetry-operator.enabled=true)

    # Check if the operator webhook has namespace selector configured
    # The selector should be in the MutatingWebhookConfiguration
    if echo "$output" | grep -A5 "namespaceSelector:" | grep -q "opentelemetry.io/inject"; then
        log_pass "Has namespace selector in webhook configuration"
    else
        log_fail "Namespace selector not found in webhook configuration"
    fi
}

# ============================================================================
# RBAC Lifecycle Tests (Issue 6 — resources must be regular, not hooks)
# ============================================================================

test_rbac_no_hook_annotations() {
    log_test "RBAC - ClusterRole and ClusterRoleBinding are regular resources (no hook annotations)"

    local output
    output=$(render_template "templates/opentelemetry/rbac.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true)

    # Extract ClusterRole and ClusterRoleBinding sections; neither should have helm.sh/hook
    local cluster_section
    cluster_section=$(echo "$output" | awk '/kind: ClusterRole/{found=1} found{print} /^---/{if(found && NR>1) found=0}')

    assert_not_contains "$cluster_section" "helm.sh/hook" \
        "ClusterRole has no helm.sh/hook annotation (deleted on uninstall)"
    assert_not_contains "$output" "before-hook-creation" \
        "No before-hook-creation delete policy (resources are regular Helm-managed)"
}

test_rbac_cr_creator_no_hooks() {
    log_test "RBAC - ServiceAccount/Role/RoleBinding for otel-cr-creator are regular resources"

    local output
    output=$(render_template "templates/opentelemetry/rbac.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true)

    # The entire file should have no hook annotations at all
    assert_not_contains "$output" "helm.sh/hook" \
        "rbac.yaml has no helm.sh/hook annotations (all resources are regular)"
}

# ============================================================================
# Namespace-label hook timing (Issue 6 — must be post-install, not pre-install)
# ============================================================================

test_namespace_label_post_install_hook() {
    log_test "Namespace Label - Uses post-install hook (not pre-install)"

    local output
    output=$(render_template "templates/opentelemetry/namespace-label.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.namespaceLabel.enabled=true \
        --set opentelemetry.collector.enabled=true)

    assert_contains "$output" "post-install,post-upgrade" \
        "Namespace label job uses post-install,post-upgrade hook"
    assert_not_contains "$output" "pre-install" \
        "Namespace label job does NOT use pre-install hook"
}

# ============================================================================
# CR Installer resilience (Issue 2 — retry counter, Issue 3 — restart guard)
# ============================================================================

test_otel_cr_installer_retry_counter() {
    log_test "OTel CR Installer - Has bounded retry counter with exit on failure"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true \
        --set opentelemetry.instrumentation.enabled=true)

    assert_contains "$output" "max_retries" \
        "Has max_retries variable (bounded retry loop)"
    assert_contains "$output" "exit 1" \
        "Has exit 1 on retry exhaustion (no infinite loop)"
    assert_contains "$output" "retries=0" \
        "Initializes retry counter"
    assert_contains "$output" "instrumentation-cr.yaml" \
        "Instrumentation CR uses temp file (not heredoc-in-until)"
    assert_contains "$output" "collector-cr.yaml" \
        "Collector CR uses temp file"
}

test_otel_cr_installer_restart_guard() {
    log_test "OTel CR Installer - Rollout restart guarded by init container check"

    local output
    output=$(render_template "templates/opentelemetry/otel-cr-installer.yaml" \
        --set global.registry.url=test.io \
        --set opentelemetry.collector.enabled=true)

    assert_contains "$output" "initContainers" \
        "Checks for existing OTel init container before restart"
    assert_contains "$output" "skipping rollout restart" \
        "Has skip message when OTel already injected"
    assert_contains "$output" "opentelemetry" \
        "Checks for opentelemetry init container name"
}


# ============================================================================
# Telemetry Env Var Tests (CEML-708)
# ============================================================================

# Defaults: telemetry.enabled is "" and collector is off → ENABLED=false,
# no other MLRUN_TELEMETRY__* keys emitted.
test_telemetry_default_inherits_collector_disabled() {
    log_test "Telemetry - defaults inherit collector=disabled"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml")

    assert_contains "$output" 'MLRUN_TELEMETRY__ENABLED: "false"' "Telemetry disabled by default"
    assert_not_contains "$output" "MLRUN_TELEMETRY__OTLP_ENDPOINT" "No endpoint emitted when disabled"
    assert_not_contains "$output" "MLRUN_TELEMETRY__INSECURE" "No insecure key when disabled"
    assert_not_contains "$output" "MLRUN_TELEMETRY__HEADERS_SECRET_NAME" "No headers secret key when disabled"
}

# Empty telemetry.enabled inherits from opentelemetry.collector.enabled; with
# collector on, ENABLED resolves to true and endpoint derives from the release
# namespace + configured grpc port. INSECURE is NOT emitted by default —
# mlrun-api falls back to its own default (true, plaintext gRPC, correct for
# the in-cluster collector).
test_telemetry_inherits_collector_enabled() {
    log_test "Telemetry - inherits collector=enabled"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml" \
        --set opentelemetry.collector.enabled=true)

    assert_contains "$output" 'MLRUN_TELEMETRY__ENABLED: "true"' "Telemetry inherits enabled=true"
    assert_contains "$output" 'MLRUN_TELEMETRY__OTLP_ENDPOINT: "test-otel-collector.default.svc.cluster.local:4317"' "Endpoint derived from in-cluster collector (uses fullname helper)"
    assert_not_contains "$output" "MLRUN_TELEMETRY__INSECURE" "Insecure not emitted by default (mlrun-api default = true)"
}

# User-supplied otlpEndpoint always wins, even with the in-cluster collector
# off — supports pointing mlrun-api at an external SaaS endpoint. INSECURE is
# not auto-flipped here; mlrun-api falls back to its own default (true), and
# users targeting a TLS endpoint must explicitly set telemetry.insecure=false.
test_telemetry_external_endpoint() {
    log_test "Telemetry - user external endpoint honored"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml" \
        --set telemetry.enabled=true \
        --set telemetry.otlpEndpoint=external.com:4317 \
        --set opentelemetry.collector.enabled=false)

    assert_contains "$output" 'MLRUN_TELEMETRY__ENABLED: "true"' "User opt-in honored despite collector off"
    assert_contains "$output" 'MLRUN_TELEMETRY__OTLP_ENDPOINT: "external.com:4317"' "User endpoint passed through verbatim"
    assert_not_contains "$output" "MLRUN_TELEMETRY__INSECURE" "Insecure not auto-emitted for user endpoint (mlrun-api default applies)"
}

# When the user explicitly sets telemetry.insecure (e.g. =false for a TLS
# endpoint), the chart MUST emit it — otherwise the mlrun-api default of
# true would silently break TLS.
test_telemetry_insecure_emitted_when_set() {
    log_test "Telemetry - insecure emitted when user overrides"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml" \
        --set telemetry.enabled=true \
        --set telemetry.otlpEndpoint=external.com:4317 \
        --set telemetry.insecure=false)

    assert_contains "$output" 'MLRUN_TELEMETRY__INSECURE: "false"' "User-supplied insecure=false passed through"
}

# Safety override: enabled=true with no in-cluster collector AND no user
# otlpEndpoint must force ENABLED=false to avoid silently dropping spans.
test_telemetry_safety_force_disable() {
    log_test "Telemetry - safety forces disable when no listener"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml" \
        --set telemetry.enabled=true \
        --set opentelemetry.collector.enabled=false)

    assert_contains "$output" 'MLRUN_TELEMETRY__ENABLED: "false"' "Safety override forces false"
    assert_not_contains "$output" "MLRUN_TELEMETRY__OTLP_ENDPOINT" "No endpoint emitted when force-disabled"
}

# headersSecretName must not be rendered as an env var when telemetry is off —
# downstream consumers shouldn't see a stale auth-headers reference.
test_telemetry_headers_secret_emitted_only_when_enabled() {
    log_test "Telemetry - headers secret skipped when disabled"

    local output
    output=$(render_template "templates/config/mlrun-env-configmap.yaml" \
        --set telemetry.headersSecretName=my-secret \
        --set telemetry.enabled=false)

    assert_contains "$output" 'MLRUN_TELEMETRY__ENABLED: "false"' "Explicit disable honored"
    assert_not_contains "$output" "MLRUN_TELEMETRY__HEADERS_SECRET_NAME" "Headers secret skipped when disabled"
}

# ============================================================================
# Full Chart Render Test
# ============================================================================

test_full_chart_renders() {
    log_test "Full chart renders without errors"

    local output
    output=$(render_all --set global.registry.url=test.io 2>&1)

    if [[ $? -eq 0 ]] && [[ -n "$output" ]]; then
        log_pass "Full chart renders successfully"
    else
        log_fail "Full chart failed to render"
    fi
}

# ============================================================================
# Main
# ============================================================================

main() {
    log_info "Running Helm template tests for MLRun CE"
    log_info "Chart directory: ${CHART_DIR}"
    echo ""

    # Ensure dependencies are up to date
    log_info "Updating Helm dependencies..."
    helm dependency update "${CHART_DIR}" > /dev/null 2>&1

    echo ""
    echo "========================================"
    echo "OpenTelemetry Collector Tests"
    echo "========================================"
    test_otel_collector_default
    test_otel_collector_disabled
    test_otel_collector_upgrade_strategy
    test_otel_collector_resources

    echo ""
    echo "========================================"
    echo "OpenTelemetry Instrumentation Tests"
    echo "========================================"
    test_otel_instrumentation_default
    test_otel_instrumentation_disabled
    test_otel_instrumentation_java_enabled

    echo ""
    echo "========================================"
    echo "OpenTelemetry RBAC Tests"
    echo "========================================"
    test_otel_rbac_default
    test_otel_rbac_disabled

    echo ""
    echo "========================================"
    echo "Jupyter OTEL Integration Tests"
    echo "========================================"
    test_jupyter_otel_labels
    test_jupyter_no_otel_label_when_disabled

    echo ""
    echo "========================================"
    echo "Admin/Non-Admin Installation Tests"
    echo "========================================"
    test_admin_values_otel
    test_non_admin_values_otel

    echo ""
    echo "========================================"
    echo "Namespace Label Tests"
    echo "========================================"
    test_namespace_label_enabled
    test_namespace_label_disabled
    test_admin_namespace_label_disabled
    test_non_admin_namespace_label_enabled
    test_otel_operator_namespace_selector


    echo ""
    echo "========================================"
    echo "RBAC Lifecycle Tests"
    echo "========================================"
    test_rbac_no_hook_annotations
    test_rbac_cr_creator_no_hooks

    echo ""
    echo "========================================"
    echo "Namespace Label Hook Timing Tests"
    echo "========================================"
    test_namespace_label_post_install_hook

    echo ""
    echo "========================================"
    echo "CR Installer Resilience Tests"
    echo "========================================"
    test_otel_cr_installer_retry_counter
    test_otel_cr_installer_restart_guard

    echo ""
    echo "========================================"
    echo "Telemetry Env Var Tests"
    echo "========================================"
    test_telemetry_default_inherits_collector_disabled
    test_telemetry_inherits_collector_enabled
    test_telemetry_external_endpoint
    test_telemetry_insecure_emitted_when_set
    test_telemetry_safety_force_disable
    test_telemetry_headers_secret_emitted_only_when_enabled

    echo ""
    echo "========================================"
    echo "Full Chart Tests"
    echo "========================================"
    test_full_chart_renders

    echo ""
    echo "========================================"
    echo "Test Summary"
    echo "========================================"
    echo -e "Passed: ${GREEN}${TESTS_PASSED}${NC}"
    echo -e "Failed: ${RED}${TESTS_FAILED}${NC}"

    if [[ ${TESTS_FAILED} -gt 0 ]]; then
        log_error "Some tests failed!"
        exit 1
    else
        log_info "All tests passed!"
        exit 0
    fi
}

main "$@"




