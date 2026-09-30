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

import atexit
import os
import shutil
import tempfile
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
    else:
        # Asked once, not once to test and once to read: `minikube ip` shells out to the
        # node container, so the second call doubled the wait on every run for an answer
        # the first had already produced.
        minikube_ip = run(["minikube", "ip"]) if shutil.which("minikube") else None
        if minikube_ip is not None and minikube_ip.ok and minikube_ip.out.strip():
            suggested = minikube_ip.out.strip()
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
        if not settings.enable_ingress:
            # kaniko builds from inside the cluster and resolves this name fine, but the
            # image reference it writes is pulled by the node's container runtime, which
            # reads the host resolver and generally knows nothing about svc.cluster.local.
            # So the build succeeds and the function pod then fails to pull it.
            log_warn("Local registry is only addressable as in-cluster DNS without ingress.")
            log_warn(
                f"Nodes typically cannot resolve {settings.local_registry_url}, so MLRun "
                "builds may push and then fail at image pull. Add --enable-ingress for a "
                "node-resolvable name."
            )
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


def validate_chart_path(settings: Settings) -> None:
    """Check that --chart-path names a chart directory. No-op when it is not set.

    Called from execute() before anything touches the cluster as well as from
    resolve_chart_source, which runs long after the namespace is created and a local
    registry is deployed — a typo in the path used to leave both of those behind on the way
    to reporting that the directory was never there.
    """
    if not settings.chart_path:
        return
    chart_dir = Path(settings.chart_path)
    if not chart_dir.is_dir():
        raise die(f"Chart path not found: {settings.chart_path}")
    if not (chart_dir / "Chart.yaml").is_file():
        raise die(
            f"No Chart.yaml found in {settings.chart_path} — is this a valid Helm chart directory?"
        )


def isolate_helm_repo_config() -> None:
    """Point helm's repository list at a throwaway file for the rest of this process.

    `helm repo add --force-update mlrun-ce` rewrites ~/.config/helm/repositories.yaml, so a
    single install permanently rebinds an `mlrun-ce` alias the user may have pointed
    somewhere else — a change to their machine that outlives the install and that nothing
    here undoes. A private file gives the run the alias it needs and leaves theirs alone.

    An explicit HELM_REPOSITORY_CONFIG is left as-is: that is the user naming a file on
    purpose.
    """
    if os.environ.get("HELM_REPOSITORY_CONFIG"):
        return
    directory = tempfile.mkdtemp(prefix="mlrun-ce-helm-repos-")
    os.environ["HELM_REPOSITORY_CONFIG"] = str(Path(directory) / "repositories.yaml")
    atexit.register(shutil.rmtree, directory, True)


def resolve_chart_source(settings: Settings) -> None:
    if not settings.chart_path:
        isolate_helm_repo_config()
        log_info("Adding Helm repository...")
        # --force-update because plain `repo add` errors out when the `mlrun-ce` alias is
        # already bound to some other URL. bash swallowed that error, so an alias left over
        # from an earlier run silently decided where the chart came from, whatever
        # --helm-repo-url said. Checked, so the source is the one that was asked for.
        helm(
            settings,
            "repo",
            "add",
            "--force-update",
            "mlrun-ce",
            settings.helm_repo_url,
            check=True,
        )
        helm(settings, "repo", "update")
        settings.chart_ref = "mlrun-ce/mlrun-ce"
        return

    validate_chart_path(settings)
    chart_dir = Path(settings.chart_path)
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
