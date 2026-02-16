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
    helm repo add minio https://charts.min.io/ 2>/dev/null || true
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

minio:
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"
  replicas: 1
  mode: standalone

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
  enabled: false

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
        --timeout 10m \
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
  verify    Verify installation
  delete    Delete Kind cluster
  help      Show this help message

Environment variables:
  CLUSTER_NAME      Kind cluster name (default: mlrun-ce-test)
  NAMESPACE         Kubernetes namespace (default: mlrun)
  RELEASE_NAME      Helm release name (default: mlrun)
  CLEANUP_ON_EXIT   Delete cluster on script exit (default: false)

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
