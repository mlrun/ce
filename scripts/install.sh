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
# Interactive installer for MLRun CE Helm chart on local Kubernetes.
# Creates a Docker registry secret from your credentials, then installs the
# chart with your local URL and registry URL.
#
# Run as a command (no download, no ./ or sh). Pin to a release tag — see
# https://github.com/mlrun/ce/releases; "development" also works but moves with every merge:
#   curl -sSL https://raw.githubusercontent.com/mlrun/ce/mlrun-ce-0.12.0-rc.12/scripts/install.sh | bash
#
# Or install as a named command, then run from any directory:
#   curl -sSL https://raw.githubusercontent.com/mlrun/ce/mlrun-ce-0.12.0-rc.12/scripts/install.sh -o /usr/local/bin/mlrun-ce-installer && chmod +x /usr/local/bin/mlrun-ce-installer
#   mlrun-ce-installer install
#
# Commands: install (the default), uninstall, version, help. Flags may be passed with no
# command at all, so every pre-command invocation below still means the same thing.
#
# From a clone of this repo (installs the published chart):
#   ./scripts/install.sh
#
# From a clone, installing this repo's own chart on the current branch:
#   ./scripts/install.sh --chart-path ./charts/mlrun-ce
#
# Non-interactive (CI): set REGISTRY_* and EXTERNAL_HOST_ADDRESS, REGISTRY_URL; see -h.
# Requirements: helm, kubectl (configured with a cluster), docker (installed and configured)

set -o errexit
set -o nounset
set -o pipefail

SUBCOMMAND="install"
COMMAND_ARGS=()

NAMESPACE="${NAMESPACE:-mlrun}"
RELEASE_NAME="${RELEASE_NAME:-mlrun-ce}"
REGISTRY_SECRET_NAME="${REGISTRY_SECRET_NAME:-registry-credentials}"
HELM_REPO_URL="${HELM_REPO_URL:-https://mlrun.github.io/ce}"
# --wait without --timeout inherits helm's 5m default, which a single image pull can
# outrun: the 4.2Gi jupyter image alone takes ~5m40s on a cold node, failing the release
# even though the rollout goes on to succeed. Matches `helm uninstall --timeout 960s`.
HELM_TIMEOUT="${HELM_TIMEOUT:-960s}"
SKIP_REGISTRY_SECRET="${SKIP_REGISTRY_SECRET:-false}"
SKIP_VALIDATORS="${SKIP_VALIDATORS:-false}"
REGISTRY_PASSWORD_FILE="${REGISTRY_PASSWORD_FILE:-}"
VALUES_FILE=""
SHOW_PROGRESS="${SHOW_PROGRESS:-false}"
UNINSTALL="${UNINSTALL:-false}"
HARD_CLEAN="${HARD_CLEAN:-false}"
DISABLE_SYSTEM_MONITORING="${DISABLE_SYSTEM_MONITORING:-false}"
DISABLE_SPARK="${DISABLE_SPARK:-false}"
DISABLE_MPI="${DISABLE_MPI:-false}"
DISABLE_MODEL_MONITORING="${DISABLE_MODEL_MONITORING:-false}"
ENABLE_INGRESS="${ENABLE_INGRESS:-false}"
INGRESS_CLASS="${INGRESS_CLASS:-nginx}"
ENABLE_OTEL_OPERATOR="${ENABLE_OTEL_OPERATOR:-false}"
ENABLE_OTEL_COLLECTOR="${ENABLE_OTEL_COLLECTOR:-false}"
ENABLE_OTEL_NAMESPACE_LABEL="${ENABLE_OTEL_NAMESPACE_LABEL:-false}"
ENABLE_OTEL_INSTRUMENTATION="${ENABLE_OTEL_INSTRUMENTATION:-false}"
LOCAL_REGISTRY="${LOCAL_REGISTRY:-false}"
LOCAL_REGISTRY_URL=""
EXTERNAL_HOST_ADDRESS="${EXTERNAL_HOST_ADDRESS:-}"
CE_VERSION="${CE_VERSION:-}"
CHART_PATH="${CHART_PATH:-}"
DRY_RUN="${DRY_RUN:-false}"
NON_INTERACTIVE="${NON_INTERACTIVE:-false}"
CONFIG_FILE="${CONFIG_FILE:-}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
MLRUN_VERSION="${MLRUN_VERSION:-}"
NUCLIO_VERSION="${NUCLIO_VERSION:-}"

# Colors for output (use $'...' so escape sequences are actual bytes, not literal \033).
# Suppressed when stdout isn't a terminal or NO_COLOR is set (https://no-color.org), so a
# piped or redirected run — CI logs, `| tee install.log` — reads as text instead of escapes.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    RED=$'\033[0;31m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    NC=$'\033[0m'
else
    RED=""
    GREEN=""
    YELLOW=""
    NC=""
fi

log_info() { printf '%s\n' "${GREEN}[INFO]${NC} $1"; }
log_warn() { printf '%s\n' "${YELLOW}[WARN]${NC} $1"; }
log_error() { printf '%s\n' "${RED}[ERROR]${NC} $1" >&2; }

# Wrap kubectl/helm so every call in this script honors KUBE_CONTEXT (installer.kubeContext)
# without threading --context/--kube-context through every call site individually.
kubectl() { command kubectl ${KUBE_CONTEXT:+--context "${KUBE_CONTEXT}"} "$@"; }
helm() { command helm ${KUBE_CONTEXT:+--kube-context "${KUBE_CONTEXT}"} "$@"; }

# The installer has no version of its own — it ships with the chart and is released by the
# same tag — so read the version off the chart beside it rather than keeping a copy here
# that has to be carried forward by hand. Served via `curl | bash` or copied to a bin
# directory there is no chart to read, and the version is genuinely unknown: nothing in a
# standalone script records which commit it came from. Pin by release tag to know.
installer_version() {
    local script_path target script_dir chart_yaml
    script_path="${BASH_SOURCE[0]:-$0}"

    # Follow symlinks by hand: `readlink -f` is GNU-only, and the common way to put this
    # on PATH during development is a symlink into a checkout, where the link's own
    # directory has no chart in it. Loop rather than resolve once, since a link can chain.
    while [[ -L "${script_path}" ]]; do
        target="$(readlink "${script_path}")"
        if [[ "${target}" == /* ]]; then
            script_path="${target}"
        else
            script_path="$(dirname "${script_path}")/${target}"
        fi
    done

    script_dir="$(cd "$(dirname "${script_path}")" 2>/dev/null && pwd)" || script_dir=""
    chart_yaml="${script_dir}/../charts/mlrun-ce/Chart.yaml"

    if [[ -n "${script_dir}" && -f "${chart_yaml}" ]]; then
        awk '/^version:/ {print $2; exit}' "${chart_yaml}"
    else
        printf 'unknown (standalone script — pin by release tag to identify it)'
    fi
}

usage() {
    cat <<EOF
Usage: mlrun-ce-installer <command> [options]

Commands:
  install              Install MLRun CE (default when no command is given)
  uninstall            Uninstall the MLRun CE Helm release
  version              Print the installer version
  help                 Show this help message

Interactive installer for MLRun CE on your local Kubernetes cluster.
Creates a Docker registry secret (unless skipped) and installs the Helm chart
with your local URL and registry URL, or from a values file.

Options:
  -h, --help           Show this help message
  -v, --version        Print the installer version (read from the chart it ships with)
  --uninstall          Uninstall the MLRun CE Helm release (uses RELEASE_NAME and NAMESPACE)
  --hard-clean         Use with --uninstall: also delete all PVCs and PVs in the namespace (data loss!)
  --skip-secret        Do not create or replace the registry secret (use existing one)
  --skip-validators    Skip the pre-install validators (K8s/Helm version, StorageClass,
                       registry auth, NodePort conflicts, node capacity)
  -f, --values FILE    Use this YAML values file as the base for installation.
                       Alone (no --config): fully self-contained (global.registry.*,
                       versions, everything) — skips secret creation and prompts.
                       Combined with --config: --config still creates the registry
                       secret and resolves its curated fields as --set overrides,
                       which win over the same keys in this file (helm applies --set
                       after --values). See docs/configuration.md's "Precedence" section.
  --show-progress      Show a live-updating UI with each deployment/statefulset progress during install
  --disable-system-monitoring  Disable Grafana/Prometheus stack
  --disable-spark      Disable Spark operator
  --disable-mpi        Disable MPI operator resources
  --disable-model-monitoring   Disable Kafka + TimescaleDB components
  --enable-ingress [CLASS]     Enable the chart's Ingress resources for UI/API/Jupyter/Nuclio (class
                               defaults to "nginx"). Requires an ingress controller already present
                               in the cluster — this installer does not install one for you.
  --enable-otel [MODE]          Convenience for the 4 granular toggles below. MODE is one of:
                                 off, collector (operator+collector only — metrics pipeline,
                                 no auto-instrumentation), or full (all 4). Bare --enable-otel
                                 with no MODE means full (all 4), same as before this flag took
                                 a MODE. The granular flags below still work individually and
                                 combine with this.
  --enable-otel-operator        --set opentelemetry-operator.enabled=true (installs CRDs/webhook/manager)
  --enable-otel-collector       --set opentelemetry.collector.enabled=true (deploys the Collector -> Prometheus)
  --enable-otel-namespace-label --set opentelemetry.namespaceLabel.enabled=true (labels NAMESPACE; auto-instruments
                               every Python pod in it once the operator+instrumentation are also on)
  --enable-otel-instrumentation --set opentelemetry.instrumentation.enabled=true (creates the Instrumentation CR)
  --local-registry             Deploy a local registry:2 registry inside the cluster
  --ce-version      [VERSION]  Pin a specific mlrun-ce chart version (default: latest)
  --chart-path  DIR            Install from a local chart directory instead of the published repo —
                               use ./charts/mlrun-ce for this repo's own chart, on whatever branch is
                               checked out. Runs helm dependency update on the path first.
  --dry-run                    Render the helm chart without deploying (passes --dry-run=server to helm)
  --non-interactive            Never prompt; fail with exit 1 if a required value is missing (auto-set when CI=true)
  --config FILE                Read defaults from a ce-config.yaml file's 'installer:' block (requires yq).
                               Flag/env values always win over the file; see docs/configuration.md for the schema.
                               Can be combined with -f/--values — see docs/configuration.md's "Precedence" section.
Environment variables (for non-interactive / CI):
  SKIP_REGISTRY_SECRET  Set to 'true' to skip creating the registry secret
  SKIP_VALIDATORS       Set to 'true' to skip the pre-install validators
  HELM_TIMEOUT          Timeout for helm's --wait on install/upgrade (default: 960s)
  MIN_K8S_VERSION       Kubernetes version to warn below (unset by default; never blocks)
  MIN_HELM_VERSION      Helm CLI version floor the validators enforce (default: 3.6)
  DISABLE_SYSTEM_MONITORING  Set to 'true' to disable the Grafana/Prometheus stack
  DISABLE_SPARK              Set to 'true' to disable the Spark operator
  DISABLE_MPI                Set to 'true' to disable MPI operator resources
  DISABLE_MODEL_MONITORING   Set to 'true' to disable Kafka + TimescaleDB components
  REGISTRY_USERNAME     Docker registry username
  REGISTRY_PASSWORD     Docker registry password (never read from ce-config.yaml)
  REGISTRY_PASSWORD_FILE Path to a file containing just the password (never read from ce-config.yaml;
                        REGISTRY_PASSWORD wins if both are set)
  REGISTRY_SERVER       Docker server URL (e.g. https://index.docker.io/v1/)
  REGISTRY_EMAIL        Docker registry email
  REGISTRY_URL          Registry URL for chart (e.g. index.docker.io/<username>)
  REGISTRY_SECRET_NAME  Secret name (default: registry-credentials)
  EXTERNAL_HOST_ADDRESS Local URL to reach the cluster (e.g. localhost or minikube ip)
  RELEASE_NAME          Helm release name (default: mlrun-ce)
  NAMESPACE             Kubernetes namespace (default: mlrun)
  SHOW_PROGRESS         Set to 'true' to show pod progress during install
  ENABLE_INGRESS        Set to 'true' to enable the chart's Ingress resources for
                        UI/API/Jupyter/Nuclio (requires an ingress controller already in the
                        cluster — this installer does not install one for you)
  INGRESS_CLASS         Ingress class name (default: nginx)
  ENABLE_OTEL_OPERATOR         Set to 'true' for --set opentelemetry-operator.enabled=true
  ENABLE_OTEL_COLLECTOR        Set to 'true' for --set opentelemetry.collector.enabled=true
  ENABLE_OTEL_NAMESPACE_LABEL  Set to 'true' for --set opentelemetry.namespaceLabel.enabled=true
  ENABLE_OTEL_INSTRUMENTATION  Set to 'true' for --set opentelemetry.instrumentation.enabled=true
  LOCAL_REGISTRY        Set to 'true' to deploy a local registry:2 registry
  CE_VERSION            Helm chart version (default: latest; ignored when CHART_PATH is set)
  CHART_PATH              Path to a local chart directory, e.g. ./charts/mlrun-ce (disables
                          published-repo mode)
  DRY_RUN                 Set to 'true' to render the chart without deploying
  NON_INTERACTIVE         Set to 'true' to suppress all prompts (auto-set when CI=true)
  CI                      Set to 'true' to auto-enable non-interactive mode
  CONFIG_FILE             Path to a ce-config.yaml file (same as --config)
  KUBE_CONTEXT            kubectl/helm context to use (default: current kubeconfig context)
  MLRUN_VERSION           Pin mlrun api/ui image tag (--set mlrun.{api,ui}.image.tag=...)
  NUCLIO_VERSION          Pin nuclio controller/dashboard image tag (--set nuclio.{controller,dashboard}.image.tag=...)
EOF
}

check_requirements() {
    log_info "Checking prerequisites (helm, kubectl, docker)..."

    # Helm installed (type -P bypasses the kubectl/helm wrapper functions defined above,
    # so this checks for the real binary rather than always finding our own wrapper)
    if ! type -P helm &> /dev/null; then
        log_error "Helm is not installed or not in PATH."
        log_info "Install Helm: https://helm.sh/docs/intro/install/"
        exit 1
    fi
    log_info "  Helm: $(helm version --short 2>/dev/null || helm version 2>/dev/null | head -1)"

    # kubectl installed
    if ! type -P kubectl &> /dev/null; then
        log_error "kubectl is not installed or not in PATH."
        log_info "Install kubectl: https://kubernetes.io/docs/tasks/tools/"
        exit 1
    fi

    # kubectl configured and connected to a cluster
    if ! kubectl cluster-info &> /dev/null; then
        log_error "kubectl is not configured or cannot reach a Kubernetes cluster."
        log_info "Configure kubeconfig (e.g. set KUBECONFIG or run your cluster's setup)."
        log_info "See: https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/"
        exit 1
    fi
    log_info "  kubectl: connected to cluster"

    # Docker installed
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed or not in PATH."
        log_info "Install Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi

    # Docker daemon running and configured
    if ! docker info &> /dev/null; then
        log_error "Docker daemon is not running or not accessible (e.g. permission or not in docker group)."
        log_info "Start Docker and ensure your user can run 'docker info'."
        log_info "See: https://docs.docker.com/config/daemon/"
        exit 1
    fi
    log_info "  Docker: installed and configured"

    log_info "All prerequisites met."
}

ensure_namespace() {
    if kubectl get namespace "${NAMESPACE}" &> /dev/null; then
        log_info "Namespace '${NAMESPACE}' already exists"
    elif [[ "${DRY_RUN}" == "true" ]]; then
        log_info "Dry-run: would create namespace '${NAMESPACE}'"
    else
        log_info "Creating namespace '${NAMESPACE}'..."
        kubectl create namespace "${NAMESPACE}"
    fi
}

patch_coredns_for_registry() {
    local registry_host="$1"
    local ingress_clusterip
    ingress_clusterip="$(kubectl get svc ingress-nginx-controller \
        --namespace "${NAMESPACE}" \
        -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"

    if [[ -z "${ingress_clusterip}" ]]; then
        log_warn "Could not get ingress controller ClusterIP; skipping CoreDNS patch."
        log_warn "Pods may not resolve ${registry_host} — add a hosts entry manually if needed."
        return
    fi

    local corefile
    corefile="$(kubectl get configmap coredns -n kube-system -o jsonpath='{.data.Corefile}' 2>/dev/null || true)"

    if grep -qF "${registry_host}" <<< "${corefile}"; then
        log_info "CoreDNS already has an entry for ${registry_host}; skipping patch."
        return
    fi

    # CoreDNS only allows one hosts{} block per server. If one already exists (e.g. Docker Desktop
    # adds host.docker.internal), insert our entry inside it before 'fallthrough'. Otherwise
    # create a new hosts{} block before the first 'forward' line.
    local patched_corefile
    if grep -q 'hosts {' <<< "${corefile}"; then
        patched_corefile="$(awk -v ip="${ingress_clusterip}" -v host="${registry_host}" '
            /hosts \{/               { in_hosts=1 }
            in_hosts && /^[[:space:]]*}/ {
                if (!inserted) { print "        " ip " " host; inserted=1 }
                in_hosts=0
            }
            !inserted && in_hosts && /fallthrough/ {
                print "        " ip " " host
                inserted=1
            }
            { print }
        ' <<< "${corefile}")"
    else
        patched_corefile="$(awk -v ip="${ingress_clusterip}" -v host="${registry_host}" '
            !inserted && /forward / {
                print "    hosts {"
                print "        " ip " " host
                print "        fallthrough"
                print "    }"
                inserted=1
            }
            { print }
        ' <<< "${corefile}")"
    fi

    kubectl create configmap coredns \
        --from-literal="Corefile=${patched_corefile}" \
        --namespace kube-system \
        --dry-run=client -o yaml | kubectl apply -f -

    kubectl rollout restart deployment/coredns --namespace kube-system
    kubectl rollout status deployment/coredns --namespace kube-system --timeout=60s

    log_info "CoreDNS patched: ${registry_host} → ${ingress_clusterip}"
}

deploy_local_registry() {
    # Resolve URL early so create_registry_secret (called before gather_install_params) has it
    if [[ "${ENABLE_INGRESS}" == "true" ]]; then
        LOCAL_REGISTRY_URL="registry.${EXTERNAL_HOST_ADDRESS}"
    else
        LOCAL_REGISTRY_URL="local-registry.${NAMESPACE}.svc.cluster.local:5000"
    fi

    log_info "Deploying local Docker registry..."
    kubectl apply -f - --namespace "${NAMESPACE}" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: local-registry
  namespace: ${NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: local-registry
  template:
    metadata:
      labels:
        app: local-registry
    spec:
      containers:
        - name: registry
          image: registry:2
          ports:
            - containerPort: 5000
---
apiVersion: v1
kind: Service
metadata:
  name: local-registry
  namespace: ${NAMESPACE}
spec:
  selector:
    app: local-registry
  type: ClusterIP
  ports:
    - port: 5000
      targetPort: 5000
EOF

    # Create Ingress if --local-registry and --enable-ingress are both set
    if [[ "${ENABLE_INGRESS}" == "true" ]]; then
        kubectl apply -f - --namespace "${NAMESPACE}" <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: local-registry
  namespace: ${NAMESPACE}
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
spec:
  ingressClassName: ${INGRESS_CLASS}
  rules:
    - host: registry.${EXTERNAL_HOST_ADDRESS}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: local-registry
                port:
                  number: 5000
EOF
        LOCAL_REGISTRY_URL="registry.${EXTERNAL_HOST_ADDRESS}"
        log_info "Local registry ingress created: ${LOCAL_REGISTRY_URL}"
        patch_coredns_for_registry "${LOCAL_REGISTRY_URL}"
        # /etc/hosts needs a real IP; host.docker.internal is already localhost on Docker Desktop
        local hosts_ip="${EXTERNAL_HOST_ADDRESS}"
        [[ "${EXTERNAL_HOST_ADDRESS}" == "host.docker.internal" ]] && hosts_ip="127.0.0.1"
        log_warn "To push images from this machine, add to /etc/hosts:"
        log_warn "  ${hosts_ip}  ${LOCAL_REGISTRY_URL}"
        log_warn "In Docker Desktop: Settings → Docker Engine → add:"
        log_warn '  "insecure-registries": ["'"${LOCAL_REGISTRY_URL}"'"]'
    fi
}

prompt_or_env() {
    local env_var="$1"
    local prompt_msg="$2"
    local default="${3:-}"
    local secret="${4:-}"

    if [[ -n "${!env_var:-}" ]]; then
        echo "${!env_var}"
        return
    fi
    if [[ "${NON_INTERACTIVE}" == "true" ]]; then
        if [[ -n "$default" ]]; then
            echo "$default"
        else
            log_error "Required value '${env_var}' is not set and no default is available (non-interactive mode)."
            exit 1
        fi
        return
    fi
    local default_prompt=""
    [[ -n "$default" ]] && default_prompt=" [$default]"
    if [[ "$secret" == "secret" ]]; then
        read -r -s -p "${prompt_msg}${default_prompt}: " value
        echo "" >&2
    else
        read -r -p "${prompt_msg}${default_prompt}: " value
    fi
    echo "${value:-$default}"
}

# Reads the reserved 'installer:' block from a ce-config.yaml (--config/CONFIG_FILE) and
# fills in vars that flag/env didn't already set. Never touches helm values directly —
# every key here just becomes a default for existing flags/env/prompts, so precedence stays
# flag > env > config file > built-in default everywhere.
load_config() {
    CONFIG_REGISTRY_URL=""
    CONFIG_REGISTRY_USERNAME=""
    CONFIG_REGISTRY_SERVER=""
    CONFIG_REGISTRY_EMAIL=""
    CONFIG_EXTERNAL_HOST_ADDRESS=""

    [[ -z "${CONFIG_FILE}" ]] && return 0

    if [[ ! -f "${CONFIG_FILE}" ]]; then
        log_error "Config file not found: ${CONFIG_FILE}"
        exit 1
    fi

    if ! command -v yq &> /dev/null; then
        log_error "yq is required to use --config/CONFIG_FILE but is not installed or not in PATH."
        log_info "Install yq (pin a version, verify its checksum): https://github.com/mikefarah/yq/releases"
        exit 1
    fi

    yq_get() {
        local val
        val="$(yq eval "$1" "${CONFIG_FILE}" 2>/dev/null || true)"
        [[ "$val" == "null" ]] && val=""
        printf '%s' "$val"
    }

    if [[ -n "$(yq_get '.installer.registry.secret.password')" ]]; then
        log_warn "ce-config.yaml has 'installer.registry.secret.password' set — ignored."
        log_warn "Passwords are never read from the config file; set REGISTRY_PASSWORD or use the prompt."
    fi

    CONFIG_REGISTRY_URL="$(yq_get '.installer.registry.url')"
    CONFIG_REGISTRY_USERNAME="$(yq_get '.installer.registry.secret.username')"
    CONFIG_REGISTRY_SERVER="$(yq_get '.installer.registry.secret.server')"
    CONFIG_REGISTRY_EMAIL="$(yq_get '.installer.registry.secret.email')"

    CONFIG_EXTERNAL_HOST_ADDRESS="$(yq_get '.installer.externalHostAddress')"
    [[ "${CONFIG_EXTERNAL_HOST_ADDRESS}" == "auto" ]] && CONFIG_EXTERNAL_HOST_ADDRESS=""

    local cfg_kube_context
    cfg_kube_context="$(yq_get '.installer.kubeContext')"
    [[ -z "${KUBE_CONTEXT}" && -n "${cfg_kube_context}" ]] && KUBE_CONTEXT="${cfg_kube_context}"

    local cfg_chart_kind cfg_chart_version cfg_chart_path
    cfg_chart_kind="$(yq_get '.installer.chartSource.kind')"
    cfg_chart_version="$(yq_get '.installer.chartSource.chartVersion')"
    cfg_chart_path="$(yq_get '.installer.chartSource.chartPath')"
    [[ -z "${CE_VERSION}" && -n "${cfg_chart_version}" ]] && CE_VERSION="${cfg_chart_version}"
    [[ -z "${CHART_PATH}" && "${cfg_chart_kind}" == "path" && -n "${cfg_chart_path}" ]] && CHART_PATH="${cfg_chart_path}"

    local cfg_mlrun_version cfg_nuclio_version
    cfg_mlrun_version="$(yq_get '.installer.versions.mlrun')"
    cfg_nuclio_version="$(yq_get '.installer.versions.nuclio')"
    [[ -z "${MLRUN_VERSION}" && -n "${cfg_mlrun_version}" ]] && MLRUN_VERSION="${cfg_mlrun_version}"
    [[ -z "${NUCLIO_VERSION}" && -n "${cfg_nuclio_version}" ]] && NUCLIO_VERSION="${cfg_nuclio_version}"

    # components.* mirrors the --disable-* flags exactly: "false" disables, same as passing
    # the flag; a flag/env-set DISABLE_* is never un-set by the file (there's no "explicitly
    # re-enable" flag to begin with, so config can only ever add a disable, never remove one).
    [[ "${DISABLE_SYSTEM_MONITORING}" != "true" && "$(yq_get '.installer.components.monitoring')" == "false" ]] && DISABLE_SYSTEM_MONITORING="true"
    [[ "${DISABLE_SPARK}" != "true" && "$(yq_get '.installer.components.spark')" == "false" ]] && DISABLE_SPARK="true"
    [[ "${DISABLE_MPI}" != "true" && "$(yq_get '.installer.components.mpi')" == "false" ]] && DISABLE_MPI="true"
    [[ "${DISABLE_MODEL_MONITORING}" != "true" && "$(yq_get '.installer.components.modelMonitoring')" == "false" ]] && DISABLE_MODEL_MONITORING="true"

    # otel is its own block (not under components.*): the chart ships all 4 of these
    # opentelemetry-operator/opentelemetry.* values disabled by default (unlike the
    # components.* above, which default enabled), so each key here opts IN, mirroring
    # the --enable-otel-* flags rather than the --disable-* pattern. Independently
    # settable so a user can enable e.g. just the operator+collector without the
    # namespace-wide auto-instrumentation of namespaceLabel/instrumentation. A
    # flag/env-set ENABLE_OTEL_* still always wins — the file can only turn one on,
    # same "add, never override" rule as the --disable-* mirrors above.
    [[ "${ENABLE_OTEL_OPERATOR}" != "true" && "$(yq_get '.installer.otel.operator')" == "true" ]] && ENABLE_OTEL_OPERATOR="true"
    [[ "${ENABLE_OTEL_COLLECTOR}" != "true" && "$(yq_get '.installer.otel.collector')" == "true" ]] && ENABLE_OTEL_COLLECTOR="true"
    [[ "${ENABLE_OTEL_NAMESPACE_LABEL}" != "true" && "$(yq_get '.installer.otel.namespaceLabel')" == "true" ]] && ENABLE_OTEL_NAMESPACE_LABEL="true"
    [[ "${ENABLE_OTEL_INSTRUMENTATION}" != "true" && "$(yq_get '.installer.otel.instrumentation')" == "true" ]] && ENABLE_OTEL_INSTRUMENTATION="true"

    # chartPath is a pure config-authoring error with no flag/env/prompt fallback, so it's
    # checked unconditionally (not just in --non-interactive mode).
    if [[ "${cfg_chart_kind}" == "path" && -z "${CHART_PATH}" ]]; then
        log_error "ce-config.yaml sets installer.chartSource.kind: path but installer.chartSource.chartPath is empty."
        exit 1
    fi

    # Everything else (registry.url/username, REGISTRY_PASSWORD) already has a flag/env/prompt
    # fallback — only fail fast here in --non-interactive mode, where there is no prompt left
    # to catch it, so every missing field is reported together instead of one exit-1 at a time.
    # --skip-secret bypasses the username/password checks below (main() still runs
    # gather_install_params for the registry URL, but skips create_registry_secret). Pure
    # -f-only mode (no --config) bypasses this whole block — main() skips
    # gather_install_params/create_registry_secret entirely in that case; -f combined with
    # --config does not bypass it, since --config still drives secret creation there.
    if [[ "${NON_INTERACTIVE}" == "true" && ( -z "${VALUES_FILE}" || -n "${CONFIG_FILE}" ) ]]; then
        local -a missing=()
        if [[ "${LOCAL_REGISTRY}" != "true" && -z "${REGISTRY_URL:-}" && -z "${CONFIG_REGISTRY_URL}" ]]; then
            missing+=("installer.registry.url (or REGISTRY_URL)")
        fi
        if [[ "${SKIP_REGISTRY_SECRET}" != "true" && "${LOCAL_REGISTRY}" != "true" ]]; then
            if [[ -z "${REGISTRY_USERNAME:-}" && -z "${CONFIG_REGISTRY_USERNAME}" ]]; then
                missing+=("installer.registry.secret.username (or REGISTRY_USERNAME)")
            fi
            if [[ -z "${REGISTRY_PASSWORD:-}" && ! -f "${REGISTRY_PASSWORD_FILE:-}" ]]; then
                missing+=("REGISTRY_PASSWORD or REGISTRY_PASSWORD_FILE (env/file only — never set via ce-config.yaml)")
            fi
        fi
        if (( ${#missing[@]} > 0 )); then
            log_error "Missing required configuration (non-interactive mode, no prompt to fall back on):"
            local m
            for m in "${missing[@]}"; do
                log_error "  - ${m}"
            done
            exit 1
        fi
    fi
}

verify_existing_registry_secret() {
    if ! kubectl get secret "${REGISTRY_SECRET_NAME}" --namespace "${NAMESPACE}" &> /dev/null; then
        log_error "--skip-secret was used but secret '${REGISTRY_SECRET_NAME}' does not exist in namespace '${NAMESPACE}'."
        log_error "Create it first (kubectl create secret docker-registry ...), or drop --skip-secret to let install.sh create it."
        exit 1
    fi
}

create_registry_secret() {
    if [[ "${LOCAL_REGISTRY}" == "true" ]]; then
        if [[ "${DRY_RUN}" == "true" ]]; then
            log_info "Dry-run: would create local registry secret '${REGISTRY_SECRET_NAME}'"
            return 0
        fi
        if kubectl get secret "${REGISTRY_SECRET_NAME}" --namespace "${NAMESPACE}" &> /dev/null; then
            log_info "Secret '${REGISTRY_SECRET_NAME}' already exists; replacing..."
            kubectl delete secret "${REGISTRY_SECRET_NAME}" --namespace "${NAMESPACE}"
        fi
        log_info "Creating local registry secret '${REGISTRY_SECRET_NAME}'..."
        kubectl create secret docker-registry "${REGISTRY_SECRET_NAME}" \
            --namespace "${NAMESPACE}" \
            --docker-server "${LOCAL_REGISTRY_URL}" \
            --docker-username "local" \
            --docker-password "local" \
            --docker-email "local@local"
        return 0
    fi

    local username password server email
    username="$(prompt_or_env "REGISTRY_USERNAME" "Docker registry username" "${CONFIG_REGISTRY_USERNAME}")"

    # REGISTRY_PASSWORD (env) > REGISTRY_PASSWORD_FILE > interactive masked prompt. Never
    # settable via ce-config.yaml, same as REGISTRY_PASSWORD — env/file/prompt only.
    if [[ -z "${REGISTRY_PASSWORD:-}" && -n "${REGISTRY_PASSWORD_FILE:-}" ]]; then
        if [[ ! -f "${REGISTRY_PASSWORD_FILE}" ]]; then
            log_error "REGISTRY_PASSWORD_FILE is set but the file does not exist: ${REGISTRY_PASSWORD_FILE}"
            exit 1
        fi
        REGISTRY_PASSWORD="$(<"${REGISTRY_PASSWORD_FILE}")"
    fi
    password="$(prompt_or_env "REGISTRY_PASSWORD" "Docker registry password" "" "secret")"
    server="$(prompt_or_env "REGISTRY_SERVER" "Docker server URL" "${CONFIG_REGISTRY_SERVER:-https://index.docker.io/v1/}")"
    email="$(prompt_or_env "REGISTRY_EMAIL" "Docker registry email" "${CONFIG_REGISTRY_EMAIL}")"

    if [[ -z "$username" || -z "$password" ]]; then
        log_error "Registry username and password are required."
        exit 1
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "Dry-run: would create registry secret '${REGISTRY_SECRET_NAME}'"
        REGISTRY_USERNAME_VALUE="${username}"
        REGISTRY_PASSWORD_VALUE="${password}"
        REGISTRY_SERVER_VALUE="${server}"
        return 0
    fi

    if kubectl get secret "${REGISTRY_SECRET_NAME}" --namespace "${NAMESPACE}" &> /dev/null; then
        log_info "Secret '${REGISTRY_SECRET_NAME}' already exists; replacing..."
        kubectl delete secret "${REGISTRY_SECRET_NAME}" --namespace "${NAMESPACE}"
    fi

    log_info "Creating Docker registry secret '${REGISTRY_SECRET_NAME}'..."
    kubectl create secret docker-registry "${REGISTRY_SECRET_NAME}" \
        --namespace "${NAMESPACE}" \
        --docker-username "${username}" \
        --docker-password "${password}" \
        --docker-server "${server}" \
        --docker-email "${email}"
    REGISTRY_USERNAME_VALUE="${username}"
    REGISTRY_PASSWORD_VALUE="${password}"
    REGISTRY_SERVER_VALUE="${server}"
}

resolve_external_host() {
    [[ -n "${EXTERNAL_HOST_ADDRESS}" ]] && return

    local suggested_host="localhost"
    # minikube/docker-desktop are heuristics about the ambient local environment — meaningless
    # once KUBE_CONTEXT explicitly selects a different (possibly remote) cluster, and
    # "kubectl config current-context" always reports the kubeconfig's ambient current-context
    # regardless of --context, so it can't be made KUBE_CONTEXT-aware. Skip straight to the
    # node-IP fallback below, which already goes through the KUBE_CONTEXT-aware kubectl wrapper.
    if [[ -n "${KUBE_CONTEXT}" ]]; then
        local node_ip
        node_ip="$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
        [[ -n "$node_ip" ]] && suggested_host="$node_ip"
    elif command -v minikube &> /dev/null && minikube ip &> /dev/null 2>&1; then
        suggested_host="$(minikube ip)"
    elif kubectl config current-context 2>/dev/null | grep -q "docker-desktop"; then
        # host.docker.internal resolves to the host from both pods and the host terminal on Docker Desktop
        suggested_host="host.docker.internal"
    fi
    # No heuristic matched (e.g. kind/k3d, or a local cluster type we don't special-case):
    # suggested_host keeps its "localhost" default from above. Those tools typically
    # NodePort-map to localhost rather than an internal Docker-network IP, so this is a
    # better generic guess than a node-IP lookup that may not be reachable from here.

    EXTERNAL_HOST_ADDRESS="$(prompt_or_env "EXTERNAL_HOST_ADDRESS" "Local URL / address to reach the cluster (e.g. localhost or minikube ip)" "${CONFIG_EXTERNAL_HOST_ADDRESS:-$suggested_host}")"

    if [[ -z "${EXTERNAL_HOST_ADDRESS}" ]]; then
        log_error "External host address is required."
        exit 1
    fi
}

gather_install_params() {
    local registry_url

    resolve_external_host

    if [[ "${LOCAL_REGISTRY}" == "true" ]]; then
        if [[ "${ENABLE_INGRESS}" == "true" ]]; then
            LOCAL_REGISTRY_URL="registry.${EXTERNAL_HOST_ADDRESS}"
        else
            LOCAL_REGISTRY_URL="local-registry.${NAMESPACE}.svc.cluster.local:5000"
        fi
        log_info "Local registry URL: ${LOCAL_REGISTRY_URL}"
        REGISTRY_URL="${LOCAL_REGISTRY_URL}"
    else
        local suggested_registry_url=""
        [[ -n "${REGISTRY_USERNAME_VALUE:-}" ]] && suggested_registry_url="index.docker.io/${REGISTRY_USERNAME_VALUE}"
        registry_url="$(prompt_or_env "REGISTRY_URL" "Docker registry URL for images (e.g. index.docker.io/<username>)" "${CONFIG_REGISTRY_URL:-$suggested_registry_url}")"
        if [[ -z "$registry_url" ]]; then
            log_error "Registry URL is required (e.g. index.docker.io/<username>)."
            exit 1
        fi
        REGISTRY_URL="$registry_url"
    fi
}

# Live-updating UI: show deployments and statefulsets progress until helm_pid exits.
# Redraws a table in place every PROGRESS_INTERVAL_SEC seconds. Only runs when stdout is a TTY.
run_deployment_progress_ui() {
    local helm_pid=$1
    local interval="${PROGRESS_INTERVAL_SEC:-10}"
    local lines=0
    local box_inner_width=62
    local green=$'\033[0;32m'
    local yellow=$'\033[1;33m'
    local cyan=$'\033[0;36m'
    local nc=$'\033[0m'

    [[ ! -t 1 ]] && return 0
    command -v tput &>/dev/null || return 0
    kill -0 "$helm_pid" 2>/dev/null || return 0

    tput civis 2>/dev/null || true

    _render_workload_section() {
        local data="$1"
        while IFS= read -r line; do
            if [[ "$line" =~ ^NAME ]]; then
                printf '    %s\n' "$line"
            elif [[ "$line" =~ ([0-9]+)/([0-9]+) ]]; then
                local ready="${BASH_REMATCH[1]}" desired="${BASH_REMATCH[2]}"
                if [[ "$ready" == "$desired" ]]; then
                    printf '    %s  %sReady%s\n' "$line" "$green" "$nc"
                else
                    printf '    %s  %sDeploying...%s\n' "$line" "$yellow" "$nc"
                fi
            else
                printf '    %s\n' "$line"
            fi
        done <<< "$data"
    }

    while kill -0 "$helm_pid" 2>/dev/null; do
        local deploy_output ss_output deploy_count ss_count new_lines progress_title
        deploy_output="$(kubectl get deployments -n "${NAMESPACE}" 2>/dev/null || true)"
        ss_output="$(kubectl get statefulsets -n "${NAMESPACE}" 2>/dev/null || true)"

        # Count output lines (0 if empty)
        deploy_count=0; [[ -n "$deploy_output" ]] && deploy_count=$(printf '%s\n' "$deploy_output" | wc -l)
        ss_count=0;     [[ -n "$ss_output"     ]] && ss_count=$(printf '%s\n' "$ss_output"     | wc -l)

        # Frame height: box(4) + "Deployments:\n\n"(2) + rows + "\n\nStatefulSets:\n\n"(3) + rows + "\n⏳..."(2)
        new_lines=$(( 4 + 2 + deploy_count + 3 + ss_count + 2 ))

        # Extend reservation if content grew since last frame
        if (( new_lines > lines )); then
            for ((i=lines; i<new_lines; i++)); do echo; done
        fi

        progress_title="$(printf '  MLRun CE - Deployment progress (refreshing every %ss)' "$interval")"
        printf '\033[%dA\033[J' "$new_lines"
        lines=$new_lines

        printf '%s\n' "${cyan}╔══════════════════════════════════════════════════════════════╗${nc}"
        printf '%s%-*.*s%s\n' "${cyan}║" "$box_inner_width" "$box_inner_width" "$progress_title" "${cyan}║${nc}"
        printf '%s╚══════════════════════════════════════════════════════════════╝%s\n\n' "${cyan}" "${nc}"

        printf '%s  Deployments:%s\n\n' "${cyan}" "${nc}"
        _render_workload_section "$deploy_output"

        printf '\n%s  StatefulSets:%s\n\n' "${cyan}" "${nc}"
        _render_workload_section "$ss_output"

        printf '\n%s  ⏳ Waiting for Helm to finish...%s\n' "${cyan}" "${nc}"
        sleep "$interval"
    done

    # Clear the progress area; cursor restore is handled by the INT trap in helm_install
    printf '\033[%dA\033[J' "$lines"
    tput cnorm 2>/dev/null || true
}

# Parse Helm NOTES and print a table: Service | URL | Credentials
print_notes_table() {
    local notes
    notes="$(helm get notes "${RELEASE_NAME}" --namespace "${NAMESPACE}" 2>/dev/null)" || return 0
    [[ -z "$notes" ]] && return 0

    local service="" url="" user="" pass=""
    local line
    printf '\n%s\n' "=============================================="
    printf '%s\n' "  MLRun CE - Access URLs"
    printf '%s\n' "=============================================="
    printf '%-18s | %-24s | %s\n' "SERVICE" "URL" "CREDENTIALS"
    printf '%-18s-+-%-24s-+-%s\n' "------------------" "------------------------" "------------------"

    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"
        if [[ "$line" =~ ^(.+)\ is\ available\ at:\ *$ ]]; then
            if [[ -n "$service" ]]; then
                local c=""
                [[ -n "$user" || -n "$pass" ]] && c="${user} / ${pass}"
                printf '%-18s | %-24s | %s\n' "$service" "$url" "$c"
            fi
            service="${BASH_REMATCH[1]}"
            url=""
            user=""
            pass=""
        elif [[ "$line" =~ ^-\ \ *username:\ *(.*)$ ]]; then
            user="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^-\ \ *password:\ *(.*)$ ]]; then
            pass="${BASH_REMATCH[1]}"
        elif [[ -n "$service" && -n "$line" && "$line" != "You're up and running!" && "$line" != "Happy MLOPSing!!! :]" ]]; then
            url="$line"
        fi
    done <<< "$notes"
    if [[ -n "$service" ]]; then
        local c=""
        [[ -n "$user" || -n "$pass" ]] && c="${user} / ${pass}"
        printf '%-18s | %-24s | %s\n' "$service" "$url" "$c"
    fi
    printf '%s\n' "=============================================="
    printf '\n'
}

resolve_chart_source() {
    if [[ -n "${CHART_PATH}" ]]; then
        if [[ ! -d "${CHART_PATH}" ]]; then
            log_error "Chart path not found: ${CHART_PATH}"
            exit 1
        fi
        if [[ ! -f "${CHART_PATH}/Chart.yaml" ]]; then
            log_error "No Chart.yaml found in ${CHART_PATH} — is this a valid Helm chart directory?"
            exit 1
        fi
        if [[ -n "${CE_VERSION}" ]]; then
            log_warn "--ce-version is ignored in local-path mode (chart version comes from ${CHART_PATH}/Chart.yaml)."
        fi
        log_info "Using local chart: ${CHART_PATH}"
        log_info "Running helm dependency update..."
        helm dependency update "${CHART_PATH}"
        CHART_REF="${CHART_PATH}"
    else
        log_info "Adding Helm repository..."
        helm repo add mlrun-ce "${HELM_REPO_URL}" 2>/dev/null || true
        helm repo update > /dev/null 2>&1
        CHART_REF="mlrun-ce/mlrun-ce"
    fi
}

helm_install() {
    resolve_chart_source

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "Dry-run mode: rendering chart without deploying..."
    else
        log_info "Installing MLRun CE (release: ${RELEASE_NAME})..."
    fi

    local helm_exit=0
    local -a extra_set_flags=()
    local -a version_flag=()
    local -a dry_run_flag=()
    local -a values_flag=()
    [[ -n "${CE_VERSION}" && -z "${CHART_PATH}" ]] && version_flag=(--version "${CE_VERSION}")
    [[ "${DRY_RUN}" == "true" ]] && dry_run_flag=(--dry-run=server)
    [[ -n "${VALUES_FILE}" ]] && values_flag=(--values "${VALUES_FILE}")
    # Pure -f-only mode (no --config) stays fully self-contained (main() never resolved
    # REGISTRY_URL/EXTERNAL_HOST_ADDRESS in that case) — every other mode (--config alone,
    # or -f + --config together) sets these as --set, which helm applies after --values,
    # so a config-resolved value always wins over the same key in a -f file.
    if [[ -z "${VALUES_FILE}" || -n "${CONFIG_FILE}" ]]; then
        extra_set_flags+=(--set "global.registry.url=${REGISTRY_URL}")
        extra_set_flags+=(--set "global.registry.secretName=${REGISTRY_SECRET_NAME}")
        extra_set_flags+=(--set "global.externalHostAddress=${EXTERNAL_HOST_ADDRESS}")
    fi
    if [[ -n "${MLRUN_VERSION}" ]]; then
        extra_set_flags+=(--set "mlrun.api.image.tag=${MLRUN_VERSION}")
        extra_set_flags+=(--set "mlrun.ui.image.tag=${MLRUN_VERSION}")
        extra_set_flags+=(--set "mlrun.api.sidecars.logCollector.image.tag=${MLRUN_VERSION}")
    fi
    if [[ -n "${NUCLIO_VERSION}" ]]; then
        extra_set_flags+=(--set "nuclio.controller.image.tag=${NUCLIO_VERSION}")
        extra_set_flags+=(--set "nuclio.dashboard.image.tag=${NUCLIO_VERSION}")
    fi
    if [[ "${DISABLE_SYSTEM_MONITORING}" == "true" ]]; then
        extra_set_flags+=(--set "kube-prometheus-stack.enabled=false")
    fi
    if [[ "${DISABLE_SPARK}" == "true" ]]; then
        extra_set_flags+=(--set "spark-operator.enabled=false")
    fi
    if [[ "${DISABLE_MPI}" == "true" ]]; then
        extra_set_flags+=(--set "mpi-operator.deployment.create=false")
        extra_set_flags+=(--set "mpi-operator.crd.create=false")
        extra_set_flags+=(--set "mpi-operator.rbac.create=false")
    fi
    if [[ "${DISABLE_MODEL_MONITORING}" == "true" ]]; then
        extra_set_flags+=(--set "strimzi-kafka-operator.enabled=false")
        extra_set_flags+=(--set "kafka.enabled=false")
        extra_set_flags+=(--set "timescaledb.enabled=false")
    fi
    if [[ "${ENABLE_INGRESS}" == "true" ]]; then
        extra_set_flags+=(--set "jupyterNotebook.ingress.enabled=true")
        extra_set_flags+=(--set "jupyterNotebook.ingress.ingressClassName=${INGRESS_CLASS}")
        extra_set_flags+=(--set "nuclio.dashboard.ingress.enabled=true")
        extra_set_flags+=(--set "mlrun.api.ingress.enabled=true")
        extra_set_flags+=(--set "mlrun.ui.ingress.enabled=true")
    fi
    if [[ "${ENABLE_OTEL_OPERATOR}" == "true" ]]; then
        extra_set_flags+=(--set "opentelemetry-operator.enabled=true")
    fi
    if [[ "${ENABLE_OTEL_COLLECTOR}" == "true" ]]; then
        extra_set_flags+=(--set "opentelemetry.collector.enabled=true")
    fi
    if [[ "${ENABLE_OTEL_NAMESPACE_LABEL}" == "true" ]]; then
        extra_set_flags+=(--set "opentelemetry.namespaceLabel.enabled=true")
    fi
    if [[ "${ENABLE_OTEL_INSTRUMENTATION}" == "true" ]]; then
        extra_set_flags+=(--set "opentelemetry.instrumentation.enabled=true")
    fi
    if [[ "${LOCAL_REGISTRY}" == "true" && "${ENABLE_INGRESS}" == "true" ]]; then
        # Local registry serves HTTP via nginx; tell kaniko to push/pull without TLS
        extra_set_flags+=(--set "mlrun.api.kaniko.insecureRegistry=true")
    fi

    if [[ "${SHOW_PROGRESS}" == "true" && "${DRY_RUN}" != "true" ]]; then
        local helm_output helm_pid
        helm_output="$(mktemp)"
        trap 'tput cnorm 2>/dev/null || true; rm -f "${helm_output:-}"; exit 130' INT TERM
        helm upgrade --install "${RELEASE_NAME}" "${CHART_REF}" \
            --namespace "${NAMESPACE}" \
            --wait \
            --timeout "${HELM_TIMEOUT}" \
            ${values_flag[@]+"${values_flag[@]}"} \
            ${version_flag[@]+"${version_flag[@]}"} \
            ${dry_run_flag[@]+"${dry_run_flag[@]}"} \
            ${extra_set_flags[@]+"${extra_set_flags[@]}"} > "$helm_output" 2>&1 &
        helm_pid=$!
        run_deployment_progress_ui "$helm_pid"
        wait "$helm_pid" || helm_exit=$?
        if [[ $helm_exit -ne 0 ]]; then
            printf '%s\n' "${RED}[ERROR]${NC} Helm upgrade/install failed. Output below:"
            if [[ -f "$helm_output" ]]; then
                cat "$helm_output"
            else
                printf '%s\n' "${YELLOW}[WARN]${NC} Helm output log file was already removed: $helm_output"
            fi
            rm -f "$helm_output"
            exit "$helm_exit"
        fi
        rm -f "$helm_output"
        trap - INT TERM

    else
        helm upgrade --install "${RELEASE_NAME}" "${CHART_REF}" \
            --namespace "${NAMESPACE}" \
            --wait \
            --timeout "${HELM_TIMEOUT}" \
            ${values_flag[@]+"${values_flag[@]}"} \
            ${version_flag[@]+"${version_flag[@]}"} \
            ${dry_run_flag[@]+"${dry_run_flag[@]}"} \
            ${extra_set_flags[@]+"${extra_set_flags[@]}"} || helm_exit=$?
    fi

    if [[ $helm_exit -ne 0 ]]; then
        log_error "Helm installation failed (exit code ${helm_exit})."
        exit "$helm_exit"
    fi

    # When we used the progress UI, the cursor is after it; print a clear separator and the URL table
    if [[ "${SHOW_PROGRESS}" == "true" ]]; then
        printf '\n'
    fi
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "Dry-run complete (no resources deployed)."
    else
        log_info "Installation complete."
        if helm status "${RELEASE_NAME}" --namespace "${NAMESPACE}" &> /dev/null; then
            print_notes_table
        fi
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit 0
                ;;
            -v|--version)
                printf 'mlrun-ce installer %s\n' "$(installer_version)"
                exit 0
                ;;
            --skip-secret)
                SKIP_REGISTRY_SECRET="true"
                shift
                ;;
            --skip-validators)
                SKIP_VALIDATORS="true"
                shift
                ;;
            -f|--values)
                if [[ -z "${2:-}" ]]; then
                    log_error "Option $1 requires a value (path to YAML file)."
                    exit 1
                fi
                VALUES_FILE="$2"
                shift 2
                ;;
            --show-progress)
                SHOW_PROGRESS="true"
                shift
                ;;
            --uninstall)
                UNINSTALL="true"
                shift
                ;;
            --hard-clean)
                HARD_CLEAN="true"
                shift
                ;;
            --disable-system-monitoring)
                DISABLE_SYSTEM_MONITORING="true"
                shift
                ;;
            --disable-spark)
                DISABLE_SPARK="true"
                shift
                ;;
            --disable-mpi)
                DISABLE_MPI="true"
                shift
                ;;
            --disable-model-monitoring)
                DISABLE_MODEL_MONITORING="true"
                shift
                ;;
            --enable-ingress)
                ENABLE_INGRESS="true"
                if [[ -n "${2:-}" && "${2}" != --* ]]; then
                    INGRESS_CLASS="$2"; shift
                fi
                shift
                ;;
            --enable-otel)
                local otel_mode="full"
                if [[ -n "${2:-}" && "${2}" != --* ]]; then
                    otel_mode="$2"; shift
                fi
                case "$otel_mode" in
                    off)
                        ENABLE_OTEL_OPERATOR="false"
                        ENABLE_OTEL_COLLECTOR="false"
                        ENABLE_OTEL_NAMESPACE_LABEL="false"
                        ENABLE_OTEL_INSTRUMENTATION="false"
                        ;;
                    collector)
                        ENABLE_OTEL_OPERATOR="true"
                        ENABLE_OTEL_COLLECTOR="true"
                        ;;
                    full)
                        ENABLE_OTEL_OPERATOR="true"
                        ENABLE_OTEL_COLLECTOR="true"
                        ENABLE_OTEL_NAMESPACE_LABEL="true"
                        ENABLE_OTEL_INSTRUMENTATION="true"
                        ;;
                    *)
                        log_error "Invalid --enable-otel mode: '${otel_mode}' (expected off, collector, or full)."
                        exit 1
                        ;;
                esac
                shift
                ;;
            --enable-otel-operator)
                ENABLE_OTEL_OPERATOR="true"
                shift
                ;;
            --enable-otel-collector)
                ENABLE_OTEL_COLLECTOR="true"
                shift
                ;;
            --enable-otel-namespace-label)
                ENABLE_OTEL_NAMESPACE_LABEL="true"
                shift
                ;;
            --enable-otel-instrumentation)
                ENABLE_OTEL_INSTRUMENTATION="true"
                shift
                ;;
            --local-registry)
                LOCAL_REGISTRY="true"
                shift
                ;;
            --chart-path)
                if [[ -z "${2:-}" || "${2:-}" == --* ]]; then
                    log_error "Option $1 requires a directory path (e.g. --chart-path ./charts/mlrun-ce)."
                    exit 1
                fi
                CHART_PATH="$2"
                shift 2
                ;;
            --ce-version)
                if [[ -z "${2:-}" || "${2:-}" == --* ]]; then
                    log_error "Option $1 requires a version value (e.g. --ce-version 0.11.0)."
                    exit 1
                fi
                CE_VERSION="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN="true"
                shift
                ;;
            --non-interactive)
                NON_INTERACTIVE="true"
                shift
                ;;
            --config)
                if [[ -z "${2:-}" || "${2:-}" == --* ]]; then
                    log_error "Option $1 requires a file path (e.g. --config ce-config.yaml)."
                    exit 1
                fi
                CONFIG_FILE="$2"
                shift 2
                ;;
            *)
                log_warn "Unknown option: $1 (ignored)"
                shift
                ;;
        esac
    done
}

do_hard_clean() {
    log_warn "Hard clean: deleting all PVCs in namespace '${NAMESPACE}'..."
    local pvcs
    pvcs="$(kubectl get pvc --namespace "${NAMESPACE}" --no-headers -o custom-columns=':metadata.name' 2>/dev/null)" || true
    if [[ -z "$pvcs" ]]; then
        log_info "No PVCs found in namespace '${NAMESPACE}'."
    else
        while IFS= read -r pvc; do
            [[ -z "$pvc" ]] && continue
            log_info "  Deleting PVC: ${pvc}"
            kubectl delete pvc "${pvc}" --namespace "${NAMESPACE}" --timeout 60s 2>/dev/null || \
                kubectl delete pvc "${pvc}" --namespace "${NAMESPACE}" --force --grace-period=0 --wait=false 2>/dev/null || true
        done <<< "$pvcs"
    fi

    log_warn "Hard clean: deleting released/failed PVs bound to namespace '${NAMESPACE}'..."
    local pvs
    pvs="$(kubectl get pv --no-headers -o custom-columns=':metadata.name,:spec.claimRef.namespace,:status.phase' 2>/dev/null \
        | awk -v ns="${NAMESPACE}" '$2 == ns { print $1 }')" || true
    if [[ -z "$pvs" ]]; then
        log_info "No PVs found for namespace '${NAMESPACE}'."
    else
        while IFS= read -r pv; do
            [[ -z "$pv" ]] && continue
            log_info "  Deleting PV: ${pv}"
            kubectl delete pv "${pv}" --timeout 60s 2>/dev/null || \
                kubectl delete pv "${pv}" --force --grace-period=0 --wait=false 2>/dev/null || true
        done <<< "$pvs"
    fi

    log_info "Hard clean complete."
}

do_uninstall() {
    check_requirements
    if ! helm status "${RELEASE_NAME}" --namespace "${NAMESPACE}" &>/dev/null; then
        log_warn "Release '${RELEASE_NAME}' not found in namespace '${NAMESPACE}' (already uninstalled?)."
    else
        log_info "Uninstalling MLRun CE release '${RELEASE_NAME}' from namespace '${NAMESPACE}'..."
        helm uninstall "${RELEASE_NAME}" --namespace "${NAMESPACE}" --timeout 960s
        log_info "Uninstall complete."
    fi

    if [[ "${HARD_CLEAN}" == "true" ]]; then
        do_hard_clean
    fi
}

# Chart's fixed NodePorts (not configurable via values.yaml).
REQUIRED_NODEPORTS=(30010 30020 30040 30050 30060 30070 30093 30094 30100 30110)

# The Helm floor mirrors charts/mlrun-ce/README.md's "Helm >=3.6" — the chart's own stated
# requirement, so the installer never refuses a Helm the chart itself supports.
#
# Kubernetes has no floor by default: the chart declares no kubeVersion in Chart.yaml and
# the README states no cluster version, so there is nothing to enforce. The check reports
# what it finds and only warns when MIN_K8S_VERSION is set explicitly. Both are overridable
# upward for anyone who wants to enforce a stricter environment.
MIN_K8S_VERSION="${MIN_K8S_VERSION:-}"
MIN_HELM_VERSION="${MIN_HELM_VERSION:-3.6}"
MIN_K8S_MAJOR=""
MIN_K8S_MINOR=""
if [[ -n "${MIN_K8S_VERSION}" ]]; then
    if [[ "${MIN_K8S_VERSION}" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
        MIN_K8S_MAJOR="${BASH_REMATCH[1]}"
        MIN_K8S_MINOR="${BASH_REMATCH[2]}"
    else
        log_error "MIN_K8S_VERSION must be in MAJOR.MINOR form (e.g. 1.30), got: '${MIN_K8S_VERSION}'"
        exit 1
    fi
fi
if [[ "${MIN_HELM_VERSION}" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
    MIN_HELM_MAJOR="${BASH_REMATCH[1]}"
    MIN_HELM_MINOR="${BASH_REMATCH[2]}"
else
    log_error "MIN_HELM_VERSION must be in MAJOR.MINOR form (e.g. 3.6), got: '${MIN_HELM_VERSION}'"
    exit 1
fi

# Informational: reports the cluster's Kubernetes version. Never blocks — neither the chart
# nor its README states a required cluster version. Warns only against an explicitly set
# MIN_K8S_VERSION.
validate_k8s_version() {
    local raw major minor
    raw="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || true)"
    if [[ -z "$raw" ]]; then
        log_warn "  Could not determine Kubernetes version; skipping version check."
        return 0
    fi
    if [[ "$raw" =~ v([0-9]+)\.([0-9]+) ]]; then
        major="${BASH_REMATCH[1]}"
        minor="${BASH_REMATCH[2]}"
    else
        log_warn "  Could not parse Kubernetes version '${raw}'; skipping version check."
        return 0
    fi
    # Keyed off the derived global, not MIN_K8S_VERSION: `MIN_K8S_VERSION=x source install.sh`
    # (how the tests load it) discards the prefix assignment once source returns, which would
    # trip `set -u` here.
    if [[ -z "${MIN_K8S_MAJOR}" ]]; then
        log_info "  Kubernetes version: ${major}.${minor}"
        return 0
    fi
    if (( major < MIN_K8S_MAJOR || (major == MIN_K8S_MAJOR && minor < MIN_K8S_MINOR) )); then
        log_warn "  Kubernetes version ${major}.${minor} is below the requested minimum (${MIN_K8S_MAJOR}.${MIN_K8S_MINOR})."
        return 0
    fi
    log_info "  Kubernetes version: ${major}.${minor} (>= ${MIN_K8S_MAJOR}.${MIN_K8S_MINOR} required)"
}

# Blocking: helm CLI version must be >= MIN_HELM_MAJOR.MIN_HELM_MINOR.
validate_helm_version() {
    local raw major minor
    raw="$(helm version --short 2>/dev/null || true)"
    if [[ -z "$raw" ]]; then
        log_warn "  Could not determine Helm version; skipping version check."
        return 0
    fi
    if [[ "$raw" =~ v([0-9]+)\.([0-9]+) ]]; then
        major="${BASH_REMATCH[1]}"
        minor="${BASH_REMATCH[2]}"
    else
        log_warn "  Could not parse Helm version '${raw}'; skipping version check."
        return 0
    fi
    if (( major < MIN_HELM_MAJOR || (major == MIN_HELM_MAJOR && minor < MIN_HELM_MINOR) )); then
        log_error "  Helm version ${major}.${minor} is below the minimum supported version (${MIN_HELM_MAJOR}.${MIN_HELM_MINOR})."
        return 1
    fi
    log_info "  Helm version: ${major}.${minor} (>= ${MIN_HELM_MAJOR}.${MIN_HELM_MINOR} required)"
}

# Blocking: cluster must have a default StorageClass (chart's PVCs rely on one).
validate_storage_class() {
    local default_sc
    default_sc="$(kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null | grep '=true$' || true)"
    if [[ -z "$default_sc" ]]; then
        log_error "  No default StorageClass found in the cluster. MLRun CE requires a default StorageClass for its PVCs."
        return 1
    fi
    log_info "  Default StorageClass: ${default_sc%%=*}"
}

# Warning only: --enable-ingress flips the chart's own Ingress resources on, but this
# installer never installs a controller for them (BYO-controller only). Check one exists
# so the user finds out now, not after the Ingress silently never gets an address.
validate_ingress_controller() {
    if [[ "${ENABLE_INGRESS}" != "true" ]]; then
        return 0
    fi
    if kubectl get ingressclass "${INGRESS_CLASS}" &> /dev/null; then
        log_info "  Ingress: IngressClass '${INGRESS_CLASS}' found"
    else
        log_warn "  Ingress: no IngressClass named '${INGRESS_CLASS}' found in the cluster."
        log_warn "  --enable-ingress only configures the chart's Ingress resources — it does not install a controller."
        log_warn "  Install one providing that class (e.g. https://kubernetes.github.io/ingress-nginx/) or the Ingress won't resolve."
    fi
}

# Warning only: best-effort docker login with the resolved registry credentials.
validate_registry_auth() {
    if [[ "${LOCAL_REGISTRY}" == "true" ]]; then
        log_info "  Registry auth: skipped (--local-registry in use, no external registry to check)"
        return 0
    fi
    if [[ -z "${REGISTRY_USERNAME_VALUE:-}" || -z "${REGISTRY_PASSWORD_VALUE:-}" ]]; then
        log_info "  Registry auth: skipped (no registry credentials resolved, e.g. -f-only mode)"
        return 0
    fi
    local server="${REGISTRY_SERVER_VALUE:-https://index.docker.io/v1/}"
    if printf '%s' "${REGISTRY_PASSWORD_VALUE}" | docker login "${server}" -u "${REGISTRY_USERNAME_VALUE}" --password-stdin &> /dev/null; then
        log_info "  Registry auth: login to ${server} succeeded"
    else
        log_warn "  Registry auth: could not log in to ${server} with the provided credentials (best-effort check; install will continue)"
    fi
}

# Warning only: chart's fixed NodePorts already bound by another Service.
validate_nodeport_conflicts() {
    local used_ports port conflicts=()
    used_ports="$(kubectl get svc --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{"\n"}{end}{end}' 2>/dev/null | awk -v ns="${NAMESPACE}" '$1 != ns { print $2 }' || true)"
    for port in "${REQUIRED_NODEPORTS[@]}"; do
        if grep -qx "${port}" <<< "$used_ports"; then
            conflicts+=("${port}")
        fi
    done
    if [[ ${#conflicts[@]} -gt 0 ]]; then
        log_warn "  NodePort conflict: already in use by another Service outside namespace '${NAMESPACE}': ${conflicts[*]}"
    else
        log_info "  NodePorts: no conflicts detected"
    fi
}

# Converts a Kubernetes allocatable-resource quantity to Ki. Memory is always Ki-suffixed,
# but ephemeral-storage is commonly reported as a bare byte count (no suffix) depending on
# the underlying cAdvisor source — handle both, plus Mi/Gi/Ti for good measure. Echoes
# nothing (and returns non-zero) if the value doesn't match any known form.
_allocatable_to_ki() {
    local raw="$1"
    if [[ "$raw" =~ ^([0-9]+)Ki$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$raw" =~ ^([0-9]+)Mi$ ]]; then
        echo $(( BASH_REMATCH[1] * 1024 ))
    elif [[ "$raw" =~ ^([0-9]+)Gi$ ]]; then
        echo $(( BASH_REMATCH[1] * 1024 * 1024 ))
    elif [[ "$raw" =~ ^([0-9]+)Ti$ ]]; then
        echo $(( BASH_REMATCH[1] * 1024 * 1024 * 1024 ))
    elif [[ "$raw" =~ ^[0-9]+$ ]]; then
        echo $(( raw / 1024 ))
    else
        return 1
    fi
}

# Warning only: cluster-wide allocatable RAM/storage above the documented floor (8Gi each).
validate_node_capacity() {
    local mem_ki=0 disk_ki=0 value value_ki
    while read -r value; do
        [[ -z "$value" ]] && continue
        value_ki="$(_allocatable_to_ki "$value")" || continue
        mem_ki=$(( mem_ki + value_ki ))
    done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.memory}{"\n"}{end}' 2>/dev/null || true)
    while read -r value; do
        [[ -z "$value" ]] && continue
        value_ki="$(_allocatable_to_ki "$value")" || continue
        disk_ki=$(( disk_ki + value_ki ))
    done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.ephemeral-storage}{"\n"}{end}' 2>/dev/null || true)

    if [[ "$mem_ki" -eq 0 && "$disk_ki" -eq 0 ]]; then
        log_warn "  Could not determine node capacity; skipping check."
        return 0
    fi

    local mem_gi=$(( mem_ki / 1024 / 1024 ))
    local disk_gi=$(( disk_ki / 1024 / 1024 ))
    if (( mem_gi < 8 )); then
        log_warn "  Node capacity: total allocatable memory ~${mem_gi}Gi is below the documented floor of 8Gi"
    else
        log_info "  Node capacity: total allocatable memory ~${mem_gi}Gi"
    fi
    if (( disk_gi < 8 )); then
        log_warn "  Node capacity: total allocatable ephemeral storage ~${disk_gi}Gi is below the documented floor of 8Gi"
    else
        log_info "  Node capacity: total allocatable ephemeral storage ~${disk_gi}Gi"
    fi
}

# Dispatcher: runs every check, reports all problems, and exits 1 once at the end if any
# blocking check failed (rather than stopping at the first one).
run_validators() {
    log_info "Running pre-install validators..."
    local failed=0

    validate_k8s_version
    validate_helm_version || failed=1
    validate_storage_class || failed=1
    validate_registry_auth
    validate_ingress_controller
    validate_nodeport_conflicts
    validate_node_capacity

    if [[ "$failed" == "1" ]]; then
        log_error "One or more required pre-install checks failed (see above). Bypass with --skip-validators if you must proceed anyway."
        exit 1
    fi
    log_info "Pre-install validation passed."
}

# Verb dispatch, kept in front of parse_args rather than inside it so the flag parser stays
# a pure flag parser. Anything that isn't a known command — a flag, or nothing at all — is
# an install, which is what every invocation documented before commands existed relied on.
# A bare unknown word is rejected rather than silently installed: `mlrun-ce-installer
# unistall` should not wipe a cluster's worth of PVCs on a typo.
parse_command() {
    SUBCOMMAND="install"
    COMMAND_ARGS=()

    if [[ $# -eq 0 ]]; then
        return 0
    fi

    case "$1" in
        install|uninstall)
            SUBCOMMAND="$1"
            shift
            ;;
        version)
            printf 'mlrun-ce installer %s\n' "$(installer_version)"
            exit 0
            ;;
        help)
            usage
            exit 0
            ;;
        -*)
            ;;
        *)
            log_error "Unknown command '$1'. Expected one of: install, uninstall, version, help."
            log_info "Flags may be passed without a command, e.g. '--dry-run' is the same as 'install --dry-run'."
            exit 1
            ;;
    esac

    COMMAND_ARGS=("$@")
}

main() {
    parse_command "$@"
    # bash < 4.4 treats an empty array as unset under `set -u`, so an argument-less run
    # would abort here without the ${a[@]+...} guard.
    parse_args ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"}

    # The `uninstall` command and the older --uninstall flag are the same thing.
    if [[ "${SUBCOMMAND}" == "uninstall" ]]; then
        UNINSTALL="true"
    fi

    # Auto-enable non-interactive when running inside a CI environment
    if [[ "${CI:-}" == "true" ]]; then
        NON_INTERACTIVE="true"
    fi

    load_config

    if [[ "${HARD_CLEAN}" == "true" && "${UNINSTALL}" != "true" ]]; then
        log_error "--hard-clean requires --uninstall."
        exit 1
    fi

    if [[ "${UNINSTALL}" == "true" ]]; then
        do_uninstall
        return
    fi

    check_requirements
    ensure_namespace

    if [[ "${LOCAL_REGISTRY}" == "true" || "${ENABLE_INGRESS}" == "true" ]]; then
        resolve_external_host
    fi

    if [[ "${LOCAL_REGISTRY}" == "true" ]]; then
        deploy_local_registry
    fi

    if [[ -n "${VALUES_FILE}" && ! -f "${VALUES_FILE}" ]]; then
        log_error "Values file not found: ${VALUES_FILE}"
        exit 1
    fi

    # Pure -f-only mode (no --config) stays fully self-contained: no secret creation, no
    # registry/host resolution — the values file must already reference an existing secret.
    # -f combined with --config runs the same secret creation / registry resolution as
    # --config-only, so the config-resolved fields have something to layer their --set on top of.
    if [[ -n "${VALUES_FILE}" && -z "${CONFIG_FILE}" ]]; then
        log_info "Using values file: ${VALUES_FILE} (skipping secret creation and install prompts)"
    else
        if [[ "${SKIP_REGISTRY_SECRET}" != "true" ]]; then
            create_registry_secret
        else
            log_info "Skipping registry secret creation (--skip-secret or SKIP_REGISTRY_SECRET)"
            verify_existing_registry_secret
        fi
        gather_install_params
    fi

    if [[ "${SKIP_VALIDATORS}" != "true" ]]; then
        run_validators
    else
        log_info "Skipping pre-install validators (--skip-validators or SKIP_VALIDATORS)"
    fi

    helm_install
}

[[ "${INSTALL_SH_SOURCE_ONLY:-}" == "true" ]] || main "$@"