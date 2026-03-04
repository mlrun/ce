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
# Local Kind cluster test for MLRun CE Helm chart
# Requirements: docker, kind, kubectl, helm

set -o errexit
set -o nounset
set -o pipefail

CLUSTER_NAME="${CLUSTER_NAME:-mlrun-ce-test}"
NAMESPACE="${NAMESPACE:-mlrun}"
RELEASE_NAME="${RELEASE_NAME:-mlrun}"
ADMIN_NAMESPACE="${ADMIN_NAMESPACE:-mlrun-admin}"
USER_NAMESPACE_1="${USER_NAMESPACE_1:-mlrun-user1}"
USER_NAMESPACE_2="${USER_NAMESPACE_2:-mlrun-user2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/../charts/mlrun-ce"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
fail() { log_error "$1"; exit 1; }

# Verify webhook mutation on a driver pod. Expects SparkApplication "spark-test" to exist.
# Args: $1 = namespace
# Returns: number of errors found
verify_spark_webhook_mutation() {
    local ns="$1"
    local errors=0

    # Wait for driver pod to be created (up to 60s)
    log_info "Waiting for driver pod spark-test-driver in ${ns}..."
    local attempt
    for attempt in $(seq 1 12); do
        if kubectl get pod spark-test-driver -n "${ns}" &>/dev/null; then
            log_info "Driver pod created"
            break
        fi
        sleep 5
    done
    if ! kubectl get pod spark-test-driver -n "${ns}" &>/dev/null; then
        log_error "Driver pod spark-test-driver not created within 60s — skipping mutation checks"
        return 1
    fi

    # Check 1: Owner reference points to SparkApplication
    local owner_kind owner_name
    owner_kind=$(kubectl get pod spark-test-driver -n "${ns}" \
        -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || echo "")
    owner_name=$(kubectl get pod spark-test-driver -n "${ns}" \
        -o jsonpath='{.metadata.ownerReferences[0].name}' 2>/dev/null || echo "")
    if [[ "${owner_kind}" == "SparkApplication" && "${owner_name}" == "spark-test" ]]; then
        log_info "Driver pod owner reference: ${owner_kind}/${owner_name} (correct)"
    else
        log_error "Driver pod owner reference: ${owner_kind}/${owner_name}, expected SparkApplication/spark-test"
        errors=$((errors + 1))
    fi

    # Check 2: Webhook-injected labels exist
    local app_name spark_role
    app_name=$(kubectl get pod spark-test-driver -n "${ns}" \
        -o jsonpath='{.metadata.labels.sparkoperator\.k8s\.io/app-name}' 2>/dev/null || echo "")
    spark_role=$(kubectl get pod spark-test-driver -n "${ns}" \
        -o jsonpath='{.metadata.labels.spark-role}' 2>/dev/null || echo "")
    if [[ "${app_name}" == "spark-test" ]]; then
        log_info "Webhook label sparkoperator.k8s.io/app-name=${app_name} (correct)"
    else
        log_error "Webhook label sparkoperator.k8s.io/app-name missing or wrong: '${app_name}'"
        errors=$((errors + 1))
    fi
    if [[ "${spark_role}" == "driver" ]]; then
        log_info "Webhook label spark-role=${spark_role} (correct)"
    else
        log_error "Webhook label spark-role missing or wrong: '${spark_role}'"
        errors=$((errors + 1))
    fi

    # Check 3: Correct service account
    local pod_sa
    pod_sa=$(kubectl get pod spark-test-driver -n "${ns}" \
        -o jsonpath='{.spec.serviceAccountName}' 2>/dev/null || echo "")
    if [[ "${pod_sa}" == "sparkapp" ]]; then
        log_info "Driver pod service account: ${pod_sa} (correct)"
    else
        log_error "Driver pod service account: '${pod_sa}', expected 'sparkapp'"
        errors=$((errors + 1))
    fi

    return "${errors}"
}

cleanup() {
    if [[ "${CLEANUP_ON_EXIT:-false}" == "true" ]]; then
        log_info "Cleaning up Kind cluster..."
        kind delete cluster --name "${CLUSTER_NAME}" 2>/dev/null || true
    fi
}

trap cleanup EXIT

check_requirements() {
    local missing=()
    local tool
    for tool in docker kind kubectl helm; do
        if ! command -v "$tool" &> /dev/null; then
            missing+=("$tool")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required tools: ${missing[*]}"
        exit 1
    fi
}

create_kind_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log_warn "Cluster '${CLUSTER_NAME}' already exists"
        return 0
    fi

    log_info "Creating Kind cluster '${CLUSTER_NAME}'..."
    cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 30080
    hostPort: 30080
    protocol: TCP
  - containerPort: 30443
    hostPort: 30443
    protocol: TCP
  - containerPort: 30110
    hostPort: 30110
    protocol: TCP
EOF
}

setup_helm_repos() {
    log_info "Adding Helm repositories..."
    helm repo add nuclio https://nuclio.github.io/nuclio/charts 2>/dev/null || true
    helm repo add mlrun https://v3io.github.io/helm-charts/stable 2>/dev/null || true
    helm repo add mpi-operator https://v3io.github.io/helm-charts/stable 2>/dev/null || true
    helm repo add spark-operator https://kubeflow.github.io/spark-operator 2>/dev/null || true
    helm repo add kube-prometheus-stack https://prometheus-community.github.io/helm-charts 2>/dev/null || true
    helm repo add kafka https://charts.bitnami.com/bitnami 2>/dev/null || true
    helm repo update
}

build_dependencies() {
    log_info "Building chart dependencies..."
    helm dependency update "${CHART_DIR}"
}

install_chart() {
    log_info "Installing MLRun CE chart..."

    # Create minimal values for local testing
    local values_file
    values_file=$(mktemp)
    # Ensure temp file cleanup on function exit (success or failure)
    trap "rm -f '${values_file}'" RETURN
    cat > "${values_file}" <<EOF
# Minimal values for local Kind testing
mlrun:
  api:
    resources:
      requests:
        memory: "256Mi"
        cpu: "100m"
      limits:
        memory: "512Mi"
        cpu: "500m"
  ui:
    resources:
      requests:
        memory: "128Mi"
        cpu: "50m"
      limits:
        memory: "256Mi"
        cpu: "200m"

nuclio:
  dashboard:
    resources:
      requests:
        memory: "128Mi"
        cpu: "50m"
      limits:
        memory: "256Mi"
        cpu: "200m"

jupyter:
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"

pipelines:
  enabled: false

kube-prometheus-stack:
  enabled: false

spark-operator:
  enabled: true
  controller:
    resources:
      requests:
        memory: "128Mi"
        cpu: "50m"
      limits:
        memory: "256Mi"
        cpu: "200m"

mpi-operator:
  enabled: false

kafka:
  enabled: false

timescaledb:
  enabled: true
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"
EOF

    helm upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" \
        --create-namespace \
        --namespace "${NAMESPACE}" \
        --values "${values_file}" \
        --timeout 20m \
        --wait \
        --debug
}

verify_installation() {
    log_info "Verifying installation..."

    # Wait for pods
    kubectl wait --for=condition=ready pod \
        --all \
        --namespace "${NAMESPACE}" \
        --timeout=300s || true

    echo ""
    log_info "Pod status:"
    kubectl get pods -n "${NAMESPACE}"

    # Verify TimescaleDB specifically
    echo ""
    log_info "Verifying TimescaleDB..."
    local tsdb_pod
    tsdb_pod=$(kubectl get pods -n "${NAMESPACE}" -l app.kubernetes.io/component=timescaledb -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

    if [[ -n "${tsdb_pod}" ]]; then
        log_info "TimescaleDB pod: ${tsdb_pod}"
        kubectl exec -n "${NAMESPACE}" "${tsdb_pod}" -- psql -U postgres -c "SELECT version();" 2>/dev/null || log_warn "Could not query PostgreSQL version"
        kubectl exec -n "${NAMESPACE}" "${tsdb_pod}" -- psql -U postgres -c "SELECT extversion FROM pg_extension WHERE extname='timescaledb';" 2>/dev/null || log_warn "Could not query TimescaleDB version"
    else
        log_warn "TimescaleDB pod not found"
    fi

    # Verify spark-operator
    echo ""
    log_info "Verifying spark-operator..."

    # Controller pod should be running
    kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/name=spark-operator \
        -n "${NAMESPACE}" --timeout=120s \
        && log_info "spark-operator controller pod is Ready" \
        || log_warn "spark-operator controller pod not ready"

    # Webhook pod should be running
    kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=webhook \
        -n "${NAMESPACE}" --timeout=120s \
        && log_info "spark-operator webhook pod is Ready" \
        || log_warn "spark-operator webhook pod not ready"

    # CRDs should exist
    kubectl get crd sparkapplications.sparkoperator.k8s.io > /dev/null 2>&1 \
        && log_info "SparkApplication CRD exists" \
        || log_warn "SparkApplication CRD not found"

    # sparkapp ServiceAccount should exist
    kubectl get sa sparkapp -n "${NAMESPACE}" > /dev/null 2>&1 \
        && log_info "sparkapp ServiceAccount exists" \
        || log_warn "sparkapp ServiceAccount not found"

    # mlrun-spark-config ConfigMap should exist
    kubectl get configmap mlrun-spark-config -n "${NAMESPACE}" > /dev/null 2>&1 \
        && log_info "mlrun-spark-config ConfigMap exists" \
        || log_warn "mlrun-spark-config ConfigMap not found"

    # Functional check: submit a SparkApplication and verify controller processes it
    log_info "Submitting test SparkApplication..."
    kubectl apply -n "${NAMESPACE}" -f - <<'SPARK_EOF'
apiVersion: sparkoperator.k8s.io/v1beta2
kind: SparkApplication
metadata:
  name: spark-test
spec:
  type: Scala
  mode: cluster
  image: spark:3.5.0
  mainClass: org.apache.spark.examples.SparkPi
  mainApplicationFile: local:///opt/spark/examples/jars/spark-examples_2.12-3.5.0.jar
  sparkVersion: "3.5.0"
  driver:
    serviceAccount: sparkapp
    cores: 1
    memory: "512m"
  executor:
    cores: 1
    instances: 1
    memory: "512m"
SPARK_EOF

    local attempt status driver_pod
    for attempt in $(seq 1 12); do
        status=$(kubectl get sparkapplication spark-test -n "${NAMESPACE}" \
            -o jsonpath='{.status.applicationState.state}' 2>/dev/null || echo "")
        driver_pod=$(kubectl get pod spark-test-driver -n "${NAMESPACE}" \
            -o jsonpath='{.metadata.name}' 2>/dev/null || echo "")
        if [[ -n "${status}" || -n "${driver_pod}" ]]; then
            log_info "SparkApplication picked up by controller (status=${status:-pending}, driver=${driver_pod:-not yet})"
            break
        fi
        log_info "Waiting for controller to process SparkApplication... (attempt ${attempt}/12)"
        sleep 5
    done

    if [[ -z "${status}" && -z "${driver_pod}" ]]; then
        log_warn "SparkApplication not processed within 60s — controller may not be working"
    fi

    # Verify webhook mutation on the driver pod
    local mutation_errors=0
    verify_spark_webhook_mutation "${NAMESPACE}" || mutation_errors=$?
    if [[ "${mutation_errors}" -gt 0 ]]; then
        log_warn "Webhook mutation checks: ${mutation_errors} issue(s)"
    fi

    kubectl delete sparkapplication spark-test -n "${NAMESPACE}" --ignore-not-found > /dev/null 2>&1
}

# --- Multi-NS test functions ---

install_admin_chart() {
    log_info "Installing admin release in namespace '${ADMIN_NAMESPACE}'..."

    local values_file
    values_file=$(mktemp)
    trap "rm -f '${values_file}'" RETURN

    cat > "${values_file}" <<EOF
# Admin namespace: CRDs + ClusterRole + Webhook, no controller pods
global:
  registry:
    url: false

nuclio:
  controller:
    enabled: false
  dashboard:
    enabled: false
  rbac:
    create: false
  platform: null

mlrun:
  enabled: false

jupyterNotebook:
  enabled: false
  serviceAccount:
    create: false
  persistence:
    enabled: false

mpi-operator:
  rbac:
    namespaced:
      create: false
  deployment:
    create: false

seaweedfs:
  enabled: false

spark-operator:
  enabled: true
  fullnameOverride: spark-operator
  controller:
    replicas: 0
    rbac:
      create: true
    serviceAccount:
      create: true
  webhook:
    enable: true
    replicas: 1
  spark:
    jobNamespaces:
      - ""
    serviceAccount:
      create: false
    rbac:
      create: false

spark:
  enabled: false

pipelines:
  enabled: false
  priority_class:
    enabled: false

kube-prometheus-stack:
  enabled: false

timescaledb:
  enabled: false

strimzi-kafka-operator:
  enabled: false

kafka:
  enabled: false
EOF

    helm upgrade --install mlrun-admin "${CHART_DIR}" \
        --create-namespace \
        --namespace "${ADMIN_NAMESPACE}" \
        --values "${values_file}" \
        --timeout 20m \
        --wait \
        --debug
}

install_user_chart() {
    local user_ns="$1"
    local release_name="$2"
    log_info "Installing user release '${release_name}' in namespace '${user_ns}'..."

    local values_file
    values_file=$(mktemp)
    trap "rm -f '${values_file}'" RETURN

    cat > "${values_file}" <<EOF
# Minimal user namespace: only spark-operator + CE spark templates
# All other components disabled to keep the test fast

spark-operator:
  enabled: true
  fullnameOverride: spark-operator
  controller:
    replicas: 1
    rbac:
      create: false
    serviceAccount:
      create: true
    leaderElection:
      enable: true
    resources:
      requests: { memory: "128Mi", cpu: "50m" }
      limits:   { memory: "256Mi", cpu: "200m" }
  webhook:
    enable: false
  spark:
    jobNamespaces:
      - ${user_ns}
    serviceAccount:
      create: true
      name: sparkapp
    rbac:
      create: true

spark:
  enabled: true

# Disable everything else
global:
  registry:
    url: false
  nuclio:
    dashboard:
      nodePort: ""
mlrun:
  enabled: false
nuclio:
  controller:
    enabled: false
  dashboard:
    enabled: false
  rbac:
    create: false
  crd:
    create: false
  platform: null
jupyterNotebook:
  enabled: false
  serviceAccount:
    create: false
  persistence:
    enabled: false
seaweedfs:
  enabled: false
pipelines:
  enabled: false
  priority_class:
    enabled: false
  crd:
    enabled: false
kube-prometheus-stack:
  enabled: false
mpi-operator:
  enabled: false
  crd:
    create: false
  rbac:
    clusterResources:
      create: false
  deployment:
    create: false
kafka:
  enabled: false
strimzi-kafka-operator:
  enabled: false
timescaledb:
  enabled: false
EOF

    helm upgrade --install "${release_name}" "${CHART_DIR}" \
        --create-namespace \
        --namespace "${user_ns}" \
        --values "${values_file}" \
        --skip-crds \
        --timeout 5m \
        --wait \
        --debug
}

verify_admin_ns() {
    log_info "=== Admin namespace (${ADMIN_NAMESPACE}) ==="
    local errors=0

    # CRDs exist
    if kubectl get crd sparkapplications.sparkoperator.k8s.io &>/dev/null; then
        log_info "SparkApplication CRD exists"
    else
        log_error "SparkApplication CRD not found"
        errors=$((errors + 1))
    fi

    if kubectl get crd scheduledsparkapplications.sparkoperator.k8s.io &>/dev/null; then
        log_info "ScheduledSparkApplication CRD exists"
    else
        log_error "ScheduledSparkApplication CRD not found"
        errors=$((errors + 1))
    fi

    # ClusterRole exists
    if kubectl get clusterrole spark-operator-controller &>/dev/null; then
        log_info "ClusterRole spark-operator-controller exists"
    else
        log_error "ClusterRole spark-operator-controller not found"
        errors=$((errors + 1))
    fi

    # No controller pods in admin (controller.replicas=0)
    local controller_pod_count
    controller_pod_count=$(kubectl get pods -n "${ADMIN_NAMESPACE}" -l app.kubernetes.io/component=controller -o name 2>/dev/null | wc -l)
    if [[ "${controller_pod_count}" -eq 0 ]]; then
        log_info "No spark-operator controller pods in admin namespace (expected)"
    else
        log_error "Found ${controller_pod_count} controller pods in admin namespace (expected 0)"
        errors=$((errors + 1))
    fi

    # Webhook pod running in admin (webhook.replicas=1)
    if kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=webhook \
        -n "${ADMIN_NAMESPACE}" --timeout=120s &>/dev/null; then
        log_info "Webhook pod is Ready in admin namespace"
    else
        log_error "Webhook pod not ready in admin namespace"
        errors=$((errors + 1))
    fi

    # MutatingWebhookConfiguration exists
    if kubectl get mutatingwebhookconfiguration spark-operator-webhook &>/dev/null; then
        log_info "MutatingWebhookConfiguration exists"
    else
        log_error "MutatingWebhookConfiguration not found"
        errors=$((errors + 1))
    fi

    # No sparkapp SA in admin
    if ! kubectl get sa sparkapp -n "${ADMIN_NAMESPACE}" &>/dev/null; then
        log_info "No sparkapp SA in admin namespace (expected)"
    else
        log_error "sparkapp SA should not exist in admin namespace"
        errors=$((errors + 1))
    fi

    # mlrun-spark-config NOT in admin
    if ! kubectl get configmap mlrun-spark-config -n "${ADMIN_NAMESPACE}" &>/dev/null; then
        log_info "mlrun-spark-config ConfigMap absent from admin namespace (expected)"
    else
        log_error "mlrun-spark-config ConfigMap should not exist in admin namespace"
        errors=$((errors + 1))
    fi

    return "${errors}"
}

# Verify a single user namespace. Args: $1 = namespace name
verify_user_ns() {
    local user_ns="$1"
    log_info "=== User namespace (${user_ns}) ==="
    local errors=0

    # spark-operator-controller pod is Running
    if kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/name=spark-operator \
        -n "${user_ns}" --timeout=120s &>/dev/null; then
        log_info "spark-operator-controller pod is Ready"
    else
        log_error "spark-operator-controller pod not ready"
        kubectl get pods -n "${user_ns}" -l app.kubernetes.io/name=spark-operator
        errors=$((errors + 1))
    fi

    # sparkapp SA exists
    if kubectl get sa sparkapp -n "${user_ns}" &>/dev/null; then
        log_info "sparkapp ServiceAccount exists"
    else
        log_error "sparkapp ServiceAccount not found"
        errors=$((errors + 1))
    fi

    # CE-created RoleBinding → ClusterRole exists and is correct
    if kubectl get rolebinding spark-operator-controller -n "${user_ns}" &>/dev/null; then
        local rb_kind rb_name
        rb_kind=$(kubectl get rolebinding spark-operator-controller -n "${user_ns}" -o jsonpath='{.roleRef.kind}')
        rb_name=$(kubectl get rolebinding spark-operator-controller -n "${user_ns}" -o jsonpath='{.roleRef.name}')
        if [[ "${rb_kind}" == "ClusterRole" && "${rb_name}" == "spark-operator-controller" ]]; then
            log_info "RoleBinding spark-operator-controller -> ClusterRole spark-operator-controller (correct)"
        else
            log_error "RoleBinding references ${rb_kind}/${rb_name}, expected ClusterRole/spark-operator-controller"
            errors=$((errors + 1))
        fi
    else
        log_error "RoleBinding spark-operator-controller not found"
        errors=$((errors + 1))
    fi

    # Leader election Role + RoleBinding exist
    if kubectl get role spark-operator-controller-leases -n "${user_ns}" &>/dev/null; then
        log_info "Leader election Role exists"
    else
        log_error "Leader election Role spark-operator-controller-leases not found"
        errors=$((errors + 1))
    fi

    if kubectl get rolebinding spark-operator-controller-leases -n "${user_ns}" &>/dev/null; then
        log_info "Leader election RoleBinding exists"
    else
        log_error "Leader election RoleBinding spark-operator-controller-leases not found"
        errors=$((errors + 1))
    fi

    # mlrun-spark-config ConfigMap exists
    if kubectl get configmap mlrun-spark-config -n "${user_ns}" &>/dev/null; then
        log_info "mlrun-spark-config ConfigMap exists"
    else
        log_error "mlrun-spark-config ConfigMap not found"
        errors=$((errors + 1))
    fi

    # Functional check: submit a SparkApplication
    log_info "Submitting SparkApplication in ${user_ns}..."
    kubectl apply -n "${user_ns}" -f - <<'SPARK_EOF'
apiVersion: sparkoperator.k8s.io/v1beta2
kind: SparkApplication
metadata:
  name: spark-test
spec:
  type: Scala
  mode: cluster
  image: spark:3.5.0
  mainClass: org.apache.spark.examples.SparkPi
  mainApplicationFile: local:///opt/spark/examples/jars/spark-examples_2.12-3.5.0.jar
  sparkVersion: "3.5.0"
  driver:
    serviceAccount: sparkapp
    cores: 1
    memory: "512m"
  executor:
    cores: 1
    instances: 1
    memory: "512m"
SPARK_EOF

    log_info "Waiting for controller to process SparkApplication..."
    local status=""
    local driver_pod=""
    local attempt
    for attempt in $(seq 1 12); do
        sleep 5
        status=$(kubectl get sparkapplication spark-test -n "${user_ns}" -o jsonpath='{.status.applicationState.state}' 2>/dev/null || echo "")
        driver_pod=$(kubectl get pod spark-test-driver -n "${user_ns}" -o name 2>/dev/null || echo "")
        if [[ -n "${status}" ]]; then
            log_info "SparkApplication status: ${status} (controller is processing)"
            break
        elif [[ -n "${driver_pod}" ]]; then
            log_info "Driver pod created (controller is processing, status not yet set)"
            break
        fi
        log_info "Attempt ${attempt}/12: waiting for controller to set status or create driver pod..."
    done
    if [[ -z "${status}" && -z "${driver_pod}" ]]; then
        log_error "SparkApplication not processed after 60s — controller may not be working"
        errors=$((errors + 1))
    fi

    # Verify webhook mutation on the driver pod
    local mutation_errors=0
    verify_spark_webhook_mutation "${user_ns}" || mutation_errors=$?
    errors=$((errors + mutation_errors))

    # Cleanup
    kubectl delete sparkapplication spark-test -n "${user_ns}" --ignore-not-found &>/dev/null

    return "${errors}"
}

verify_multi_ns() {
    log_info "Verifying multi-NS spark-operator split (1 admin + 2 user namespaces)..."
    local total_errors=0
    local ns_errors=0

    echo ""
    ns_errors=0
    verify_admin_ns || ns_errors=$?
    total_errors=$((total_errors + ns_errors))

    echo ""
    ns_errors=0
    verify_user_ns "${USER_NAMESPACE_1}" || ns_errors=$?
    total_errors=$((total_errors + ns_errors))

    echo ""
    ns_errors=0
    verify_user_ns "${USER_NAMESPACE_2}" || ns_errors=$?
    total_errors=$((total_errors + ns_errors))

    # --- Summary ---
    echo ""
    if [[ "${total_errors}" -eq 0 ]]; then
        log_info "All multi-NS checks passed! (admin + 2 user namespaces, no conflicts)"
    else
        log_error "${total_errors} check(s) failed"
        exit 1
    fi
}

delete_cluster() {
    log_info "Deleting Kind cluster '${CLUSTER_NAME}'..."
    kind delete cluster --name "${CLUSTER_NAME}"
}

usage() {
    cat <<EOF
Usage: $0 [command]

Commands:
  create    Create Kind cluster only
  install   Install MLRun CE chart (assumes cluster exists)
  full      Create cluster and install chart (default)
  multi-ns  Multi-NS test: admin + 2 user namespaces with spark-operator split
  verify    Verify installation
  delete    Delete Kind cluster
  help      Show this help message

Environment variables:
  CLUSTER_NAME        Kind cluster name (default: mlrun-ce-test)
  NAMESPACE           Kubernetes namespace (default: mlrun)
  RELEASE_NAME        Helm release name (default: mlrun)
  ADMIN_NAMESPACE     Admin namespace for multi-ns (default: mlrun-admin)
  USER_NAMESPACE_1    First user namespace for multi-ns (default: mlrun-user1)
  USER_NAMESPACE_2    Second user namespace for multi-ns (default: mlrun-user2)
  CLEANUP_ON_EXIT     Delete cluster on script exit (default: false)

Examples:
  $0 full                    # Full test: create cluster + install
  $0 create                  # Just create the cluster
  $0 install                 # Just install the chart
  CLEANUP_ON_EXIT=true $0    # Auto-cleanup after test
EOF
}

main() {
    local cmd="${1:-full}"

    check_requirements

    case "$cmd" in
        create)
            create_kind_cluster
            ;;
        install)
            setup_helm_repos
            build_dependencies
            install_chart
            verify_installation
            ;;
        full)
            create_kind_cluster
            setup_helm_repos
            build_dependencies
            install_chart
            verify_installation
            ;;
        multi-ns)
            create_kind_cluster
            setup_helm_repos
            build_dependencies
            install_admin_chart
            install_user_chart "${USER_NAMESPACE_1}" "mlrun-user1"
            install_user_chart "${USER_NAMESPACE_2}" "mlrun-user2"
            verify_multi_ns
            ;;
        verify)
            verify_installation
            ;;
        delete)
            delete_cluster
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            log_error "Unknown command: $cmd"
            usage
            exit 1
            ;;
    esac
}

main "$@"
