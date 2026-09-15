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
"""Namespace, external-host resolution and chart-source selection."""

import shutil
from pathlib import Path

import yaml

from .console import die, log_info, log_warn
from .settings import Settings, prompt_or_env
from .shell import helm, kubectl, run


def ensure_namespace(settings: Settings) -> None:
    if kubectl(settings, "get", "namespace", settings.namespace).ok:
        log_info(f"Namespace '{settings.namespace}' already exists")
    elif settings.dry_run:
        log_info(f"Dry-run: would create namespace '{settings.namespace}'")
    else:
        log_info(f"Creating namespace '{settings.namespace}'...")
        kubectl(settings, "create", "namespace", settings.namespace, check=True)


def resolve_external_host(settings: Settings) -> None:
    if settings.external_host_address:
        return

    suggested = "localhost"
    # minikube/docker-desktop are heuristics about the ambient local environment —
    # meaningless once KUBE_CONTEXT explicitly selects a different (possibly remote)
    # cluster, and `kubectl config current-context` always reports the kubeconfig's ambient
    # current-context regardless of --context, so it cannot be made KUBE_CONTEXT-aware.
    # Skip straight to the node-IP fallback, which already goes through the
    # KUBE_CONTEXT-aware kubectl wrapper.
    if settings.kube_context:
        node_ip = kubectl(
            settings,
            "get",
            "node",
            "-o",
            'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}',
        )
        if node_ip.ok and node_ip.out.strip():
            suggested = node_ip.out.strip()
    elif shutil.which("minikube") and run(["minikube", "ip"]).ok:
        suggested = run(["minikube", "ip"]).out.strip() or suggested
    else:
        current = kubectl(settings, "config", "current-context")
        if current.ok and "docker-desktop" in current.out:
            # host.docker.internal resolves to the host from both pods and the host
            # terminal on Docker Desktop.
            suggested = "host.docker.internal"
    # No heuristic matched (e.g. kind/k3d, or a local cluster type not special-cased):
    # `suggested` keeps its "localhost" default. Those tools typically NodePort-map to
    # localhost rather than an internal Docker-network IP, so this is a better generic
    # guess than a node-IP lookup that may not be reachable from here.

    settings.external_host_address = prompt_or_env(
        settings,
        "EXTERNAL_HOST_ADDRESS",
        "Local URL / address to reach the cluster (e.g. localhost or minikube ip)",
        settings.config_external_host_address or suggested,
    )

    if not settings.external_host_address:
        raise die("External host address is required.")


def resolve_local_registry_url(settings: Settings) -> str:
    if settings.enable_ingress:
        return f"registry.{settings.external_host_address}"
    return f"local-registry.{settings.namespace}.svc.cluster.local:5000"


def gather_install_params(settings: Settings) -> None:
    resolve_external_host(settings)

    if settings.local_registry:
        settings.local_registry_url = resolve_local_registry_url(settings)
        log_info(f"Local registry URL: {settings.local_registry_url}")
        settings.registry_url = settings.local_registry_url
        return

    suggested = ""
    if settings.registry_username_value:
        suggested = f"index.docker.io/{settings.registry_username_value}"

    registry_url = prompt_or_env(
        settings,
        "REGISTRY_URL",
        "Docker registry URL for images (e.g. index.docker.io/<username>)",
        settings.config_registry_url or suggested,
    )
    if not registry_url:
        raise die("Registry URL is required (e.g. index.docker.io/<username>).")
    settings.registry_url = registry_url


def chart_deps_satisfied(chart_dir: Path) -> bool:
    """True when every dependency in requirements.lock is already vendored in charts/.

    Lets an egress-restricted run (a pod, an air-gapped host) skip a dependency fetch that
    has nothing left to do. Tarball names do not always equal the dependency name — the
    lock's `strimzi-kafka-operator` ships as `strimzi-kafka-operator-helm-3-chart-<v>.tgz`
    — so match on the version suffix with a name prefix rather than an exact filename.
    """
    lock = chart_dir / "requirements.lock"
    if not lock.is_file():
        return False
    try:
        data = yaml.safe_load(lock.read_text()) or {}
    except yaml.YAMLError:
        return False

    deps = data.get("dependencies") or []
    if not deps:
        return False

    charts_dir = chart_dir / "charts"
    if not charts_dir.is_dir():
        return False
    present = [path.name for path in charts_dir.glob("*.tgz")]

    for dep in deps:
        name, version = dep.get("name"), dep.get("version")
        if not name or not version:
            return False
        suffix = f"-{version}.tgz"
        if not any(f.endswith(suffix) and f[: -len(suffix)].startswith(name) for f in present):
            return False
    return True


def resolve_chart_source(settings: Settings) -> None:
    if not settings.chart_path:
        log_info("Adding Helm repository...")
        helm(settings, "repo", "add", "mlrun-ce", settings.helm_repo_url)
        helm(settings, "repo", "update")
        settings.chart_ref = "mlrun-ce/mlrun-ce"
        return

    chart_dir = Path(settings.chart_path)
    if not chart_dir.is_dir():
        raise die(f"Chart path not found: {settings.chart_path}")
    if not (chart_dir / "Chart.yaml").is_file():
        raise die(
            f"No Chart.yaml found in {settings.chart_path} — is this a valid Helm chart directory?"
        )
    if settings.ce_version:
        log_warn(
            "--ce-version is ignored in local-path mode (chart version comes from "
            f"{settings.chart_path}/Chart.yaml)."
        )
    log_info(f"Using local chart: {settings.chart_path}")

    if settings.skip_dependency_update:
        log_info("Skipping chart dependency resolution (--skip-dependency-update).")
    elif chart_deps_satisfied(chart_dir):
        log_info("Chart dependencies already vendored and match requirements.lock; skipping fetch.")
    elif (chart_dir / "requirements.lock").is_file():
        # `build` honours requirements.lock; `update` re-resolves requirements.yaml and
        # rewrites the lock, which is the maintainer operation rather than the consumer one.
        log_info("Running helm dependency build...")
        helm(settings, "dependency", "build", settings.chart_path, check=True)
    else:
        log_info("Running helm dependency update...")
        helm(settings, "dependency", "update", settings.chart_path, check=True)

    settings.chart_ref = settings.chart_path
