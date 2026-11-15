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
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .console import InstallerError, die, log_error, log_info, log_warn
from .settings import Settings, env_str, prompt_or_env
from .shell import helm, kubectl, run

# A reserved TLD (RFC 2606): a dry run with no registry renders this, and nothing can
# ever push to it by accident.
DRY_RUN_REGISTRY_URL = "dry-run.invalid/registry"


def ensure_namespace(settings: Settings) -> None:
    if kubectl(settings, "get", "namespace", settings.namespace).ok:
        log_info(f"Namespace '{settings.namespace}' already exists")
    elif settings.dry_run:
        log_info(f"Dry-run: would create namespace '{settings.namespace}'")
    else:
        log_info(f"Creating namespace '{settings.namespace}'...")
        kubectl(settings, "create", "namespace", settings.namespace, check=True)


def effective_context(settings: Settings) -> str:
    """The kubeconfig context every call targets: KUBE_CONTEXT, else the current one."""
    if settings.kube_context:
        return settings.kube_context
    current = kubectl(settings, "config", "current-context")
    return current.out.strip() if current.ok else ""


def api_server(settings: Settings) -> str:
    # --minify narrows the view to the context the wrapper selects.
    server = kubectl(
        settings, "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
    )
    return server.out.strip() if server.ok else ""


MINIKUBE_PROVIDER = "minikube.sigs.k8s.io"


def _is_minikube_context(settings: Settings, context: str) -> bool:
    """minikube names the context after the profile and tags it with a provider extension,
    so a profile called anything but `minikube` is still recognised."""
    if context == "minikube":
        return True
    provider = kubectl(
        settings,
        "config",
        "view",
        "-o",
        f'jsonpath={{.contexts[?(@.name=="{context}")].context.extensions[*].extension.provider}}',
    )
    return provider.ok and MINIKUBE_PROVIDER in provider.out


def suggest_external_host(settings: Settings) -> str:
    """Guess the address this machine reaches the target cluster's NodePorts on.

    Keyed on the context the run targets, not on what happens to be installed: an installed
    minikube says nothing about a kind cluster being the target, and a docker-desktop
    context selected through KUBE_CONTEXT is still Docker Desktop, whose node IP is not
    reachable from the host.
    """
    context = effective_context(settings)
    if context and _is_minikube_context(settings, context) and shutil.which("minikube"):
        minikube_ip = run(["minikube", "ip", "-p", context])
        if minikube_ip.ok and minikube_ip.out.strip():
            return minikube_ip.out.strip()
    if context in ("docker-desktop", "docker-for-desktop"):
        # Resolves to the host from both pods and the host terminal on Docker Desktop.
        return "host.docker.internal"
    if settings.kube_context:
        # An explicitly chosen context is often a remote cluster, where localhost is wrong.
        node_ip = kubectl(
            settings,
            "get",
            "node",
            "-o",
            'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}',
        )
        if node_ip.ok and node_ip.out.strip():
            return node_ip.out.strip()
    # kind, k3d and the like map NodePorts to localhost rather than to an internal
    # Docker-network IP.
    return "localhost"


def resolve_external_host(settings: Settings) -> None:
    if settings.external_host_address:
        return

    suggested = settings.config_external_host_address or suggest_external_host(settings)
    settings.external_host_address = prompt_or_env(
        settings,
        "EXTERNAL_HOST_ADDRESS",
        "Local URL / address to reach the cluster (e.g. localhost or minikube ip)",
        suggested,
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

    if settings.dry_run:
        settings.registry_url = env_str("REGISTRY_URL") or settings.config_registry_url
        if not settings.registry_url:
            settings.registry_url = DRY_RUN_REGISTRY_URL
            log_info(f"Dry run: no registry URL given; rendering with {DRY_RUN_REGISTRY_URL}.")
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

    The cache goes with it: `helm repo update` writes `<alias>-index.yaml` into the shared
    HELM_REPOSITORY_CACHE, so the user's own `mlrun-ce` alias would otherwise read this
    run's index until they next updated.

    Either variable the user set explicitly is left as-is: that is them naming a location on
    purpose.
    """
    isolated = [name for name in ("HELM_REPOSITORY_CONFIG", "HELM_REPOSITORY_CACHE")
                if not os.environ.get(name)]  # fmt: skip
    if not isolated:
        return
    directory = Path(tempfile.mkdtemp(prefix="mlrun-ce-helm-repos-"))
    atexit.register(shutil.rmtree, str(directory), True)
    if "HELM_REPOSITORY_CONFIG" in isolated:
        os.environ["HELM_REPOSITORY_CONFIG"] = str(directory / "repositories.yaml")
    if "HELM_REPOSITORY_CACHE" in isolated:
        os.environ["HELM_REPOSITORY_CACHE"] = str(directory / "cache")


def dependency_repo_urls(chart_dir: Path) -> List[str]:
    """The http(s) repositories the chart's dependencies come from, in declaration order.

    apiVersion v1 lists them in requirements.yaml, v2 in Chart.yaml. `file://` and `oci://`
    dependencies need no repository entry, so they are left out.
    """
    urls: List[str] = []
    for name in ("requirements.yaml", "Chart.yaml"):
        path = chart_dir / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        for dep in data.get("dependencies") or []:
            url = str(dep.get("repository") or "").rstrip("/")
            if url.startswith(("http://", "https://")) and url not in urls:
                urls.append(url)
    return urls


def register_dependency_repos(settings: Settings, chart_dir: Path) -> None:
    """Add every dependency repository before `helm dependency build|update`.

    Both refuse with "no repository definition" for any URL not already in the repository
    config, so on a fresh helm a clean checkout could not install its own chart. The adds go
    into the isolated config, never the user's; the versions still come from the lock.
    """
    isolate_helm_repo_config()
    for index, url in enumerate(dependency_repo_urls(chart_dir)):
        helm(settings, "repo", "add", "--force-update", f"mlrun-ce-dep-{index}", url, check=True)


def _published_charts(
    settings: Settings, version: str = "", devel: bool = True
) -> Optional[List[Dict]]:
    """The mlrun-ce/mlrun-ce entries `helm search` reports, or None if it could not say.
    Without `version`, helm reports only the newest; prereleases count only with `devel`."""
    args = ["search", "repo", "mlrun-ce/mlrun-ce"]
    if devel:
        args.append("--devel")
    args += ["-o", "json"]
    if version:
        args += ["--version", version]
    found = helm(settings, *args)
    try:
        entries = json.loads(found.out) if found.ok else None
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    # The keyword is a substring match, so mlrun-ce/mlrun-ce-anything would match too.
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") == "mlrun-ce/mlrun-ce"
    ]


def _published_versions(
    settings: Settings, version: str = "", devel: bool = True
) -> Optional[List[str]]:
    charts = _published_charts(settings, version, devel)
    if charts is None:
        return None
    return [str(chart.get("version", "")).lstrip("v") for chart in charts]


def chart_app_version(settings: Settings) -> str:
    """The MLRun version of the chart being installed (its appVersion), or "" if unknown."""
    if settings.chart_path:
        try:
            chart = yaml.safe_load((Path(settings.chart_path) / "Chart.yaml").read_text())
        except (OSError, yaml.YAMLError):
            return ""
        return str(chart.get("appVersion", "")) if isinstance(chart, dict) else ""
    if not settings.ce_version:
        return ""
    charts = _published_charts(settings, settings.ce_version) or [{}]
    return str(charts[0].get("app_version", ""))


def newest_kind(settings: Settings) -> str:
    return "newest, prereleases included" if settings.devel else "newest stable"


def pin_newest_chart_version(settings: Settings) -> None:
    """Pin the newest chart (stable unless --devel), so the summary names what helm installs.

    If the search cannot say, the run stays unpinned and helm picks the same chart itself.
    """
    newest = _published_versions(settings, devel=settings.devel)
    if not newest:
        log_warn(f"Could not find the {newest_kind(settings)} chart version; helm picks it.")
        return
    settings.ce_version = newest[0]
    settings.ce_version_is_newest = True
    log_info(f"Installing chart version {settings.ce_version}, the {newest_kind(settings)}.")


def verify_chart_version_published(settings: Settings) -> None:
    """Stop before anything is created when the --ce-version chart is not in the repo.

    helm would otherwise fail at the very end, after the namespace, registry and secret
    exist. A search that itself fails proves nothing, so that only warns.
    """
    wanted = settings.ce_version
    if not wanted:
        return
    matches = _published_versions(settings, wanted)
    if matches is None:
        log_warn(f"Could not check that chart version {wanted} is published; continuing.")
        return
    if wanted.lstrip("v") in matches:
        return

    newest = _published_versions(settings, devel=False) or []
    newest_hint = f" The newest stable one there is {newest[0]}." if newest else ""
    log_error(f"Chart mlrun-ce {wanted} is not published at {settings.helm_repo_url}.{newest_hint}")
    if newest:
        log_error(f"  To install the published chart, pass --ce-version {newest[0]}.")
    raise InstallerError(code=1)


def resolve_chart_source(settings: Settings) -> None:
    if settings.devel and (settings.ce_version or settings.chart_path):
        log_warn("--devel is ignored: --ce-version or --chart-path already names the chart.")
        settings.devel = False
    if not settings.chart_path:
        if settings.update_dependencies:
            log_warn("--update-dependencies is ignored: it applies only to --chart-path.")
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
        if settings.ce_version:
            log_info(f"Installing chart version {settings.ce_version} (--ce-version).")
            verify_chart_version_published(settings)
        else:
            pin_newest_chart_version(settings)
        return

    validate_chart_path(settings)
    chart_dir = Path(settings.chart_path)
    log_info(f"Using local chart: {settings.chart_path}")

    if chart_deps_satisfied(chart_dir):
        log_info("Chart dependencies already vendored and match requirements.lock; skipping fetch.")
    elif not settings.update_dependencies:
        # Fetching reaches several upstream Helm repos, which an air-gapped host or a pod
        # cannot, so it is asked for rather than done behind the user's back.
        raise die(
            f"The chart dependencies in {chart_dir / 'charts'} are missing or do not match "
            "requirements.lock. Pass --update-dependencies to fetch them, or run "
            "`make helm-update-dependencies` in the repo."
        )
    elif (chart_dir / "requirements.lock").is_file():
        # `build` honours requirements.lock; `update` re-resolves requirements.yaml and
        # rewrites the lock, which is the maintainer operation rather than the consumer one.
        register_dependency_repos(settings, chart_dir)
        log_info("Running helm dependency build...")
        helm(settings, "dependency", "build", settings.chart_path, check=True)
    else:
        register_dependency_repos(settings, chart_dir)
        log_info("Running helm dependency update...")
        helm(settings, "dependency", "update", settings.chart_path, check=True)

    settings.chart_ref = settings.chart_path
