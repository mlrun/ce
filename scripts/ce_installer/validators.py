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
"""Pre-install checks.

Blocking: Helm version and default StorageClass, run by `run_blocking_validators` before
the cluster is touched; both run before it exits, so a user fixes everything in one pass.
Everything in `run_validators` reports and continues, except a busy NodePort set in -f; a
busy default NodePort is moved to a free one there, with a warning.
"""

import json
import re
import tempfile
from typing import Dict, List, Optional, Set, Tuple

from .cluster import chart_app_version, effective_context
from .console import die, log_error, log_info, log_warn
from .helm_ops import README_URL, _lookup, _values_file_contents
from .settings import (
    DEFAULT_DOCKER_SERVER,
    MIN_HELM_VERSION,
    MLRUN_CE_INSTALL_DOCS_URL,
    NODEPORT_RANGE,
    NODEPORTS,
    RECOMMENDED_K8S_VERSION,
    Settings,
)
from .shell import docker_available, helm, kubectl, run

# The chart's images with no arm64 build, and `mlrun`, which function pods run on.
MLRUN_AMD64_ONLY_IMAGES = ("mlrun-api", "mlrun-ui", "jupyter", "log-collector", "mlrun")
ARM64_ANCHOR = "#apple-silicon-arm64"

STORAGECLASS_JSONPATH = (
    r'{range .items[*]}{.metadata.name}{"="}'
    r"{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}"
    r'{"="}'
    r"{.metadata.annotations.storageclass\.beta\.kubernetes\.io/is-default-class}"
    r'{"\n"}{end}'
)

# One line per Service: "<namespace>/<name>/<owning release> <port> <port> ...". The release
# comes from the annotation helm writes on everything it owns, and is empty for Services it
# does not.
#
# The newline terminates the Service, not each port. With it inside the ports range a
# Service with no ports emitted no newline at all, so the next Service's "ns/release"
# prefix landed on the same line and was parsed as that Service's port number — which then
# shifted every real port on the line out of the parser's reach. One unported Service
# anywhere in the cluster was enough to hide a genuine conflict.
NODEPORT_JSONPATH = (
    r'{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"/"}'
    r"{.metadata.annotations.meta\.helm\.sh/release-name}"
    r'{" "}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}'
)


def parse_major_minor(raw: str) -> Optional[Tuple[int, int]]:
    match = re.search(r"v(\d+)\.(\d+)", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _major_minor(version: str) -> Tuple[int, int]:
    major, minor = version.split(".")
    return int(major), int(minor)


def validate_k8s_version(settings: Settings) -> bool:
    """Advisory: reports the cluster's Kubernetes version.

    Never blocks — the chart declares no kubeVersion. Warns below the version the MLRun CE
    docs require, and below an explicitly set MIN_K8S_VERSION.
    """
    # The kubelet version of an arbitrary node is not the cluster's version: kubelets are
    # allowed to trail the control plane by a couple of minor versions, and on a managed
    # cluster mid-upgrade .items[0] can be either side of the split. `version` asks the API
    # server itself. A node listing is also a much bigger permission to need than this.
    result = kubectl(settings, "version", "-o", "json")
    raw = ""
    if result.ok:
        try:
            raw = str(json.loads(result.out).get("serverVersion", {}).get("gitVersion", ""))
        except (ValueError, AttributeError):
            raw = ""
    if not raw:
        log_warn("  Could not determine Kubernetes version; skipping version check.")
        return True

    parsed = parse_major_minor(raw)
    if parsed is None:
        log_warn(f"  Could not parse Kubernetes version '{raw}'; skipping version check.")
        return True

    found = "{}.{}".format(*parsed)
    if settings.min_k8s_major is None:
        log_info(f"  Kubernetes version: {found}")
    else:
        floor = f"{settings.min_k8s_major}.{settings.min_k8s_minor}"
        if parsed < (settings.min_k8s_major, settings.min_k8s_minor):
            log_warn(f"  Kubernetes version {found} is below the requested minimum ({floor}).")
        else:
            log_info(f"  Kubernetes version: {found} (>= {floor} required)")
    if parsed < _major_minor(RECOMMENDED_K8S_VERSION):
        log_warn(
            f"  The MLRun CE docs require Kubernetes >= {RECOMMENDED_K8S_VERSION}: "
            f"{MLRUN_CE_INSTALL_DOCS_URL}"
        )
    return True


def validate_helm_version(settings: Settings) -> bool:
    """Blocking: the helm CLI version must be at or above the one the MLRun CE docs require."""
    result = helm(settings, "version", "--short")
    raw = result.out.strip() if result.ok else ""
    if not raw:
        log_warn("  Could not determine Helm version; skipping version check.")
        return True

    parsed = parse_major_minor(raw)
    if parsed is None:
        log_warn(f"  Could not parse Helm version '{raw}'; skipping version check.")
        return True

    found = "{}.{}".format(*parsed)
    if parsed < _major_minor(MIN_HELM_VERSION):
        log_error(
            f"  Helm version {found} is below the minimum supported version "
            f"({MIN_HELM_VERSION}), which the MLRun CE docs require: {MLRUN_CE_INSTALL_DOCS_URL}"
        )
        log_info("  Install a newer Helm: https://helm.sh/docs/intro/install/")
        return False
    log_info(f"  Helm version: {found} (>= {MIN_HELM_VERSION} required)")
    return True


def validate_storage_class(settings: Settings) -> bool:
    """Blocking: the cluster must have a default StorageClass — the chart's PVCs rely on one.

    Both annotations are read: Kubernetes still honours the deprecated beta key, and
    clusters provisioned years ago can carry only that one. Missing it would fail the
    install over a StorageClass that does in fact default.
    """
    result = kubectl(settings, "get", "storageclass", "-o", "jsonpath=" + STORAGECLASS_JSONPATH)
    rows = result.out.splitlines() if result.ok else []
    matched = [row for row in rows if re.search(r"=true(=|$)", row)]
    if not matched:
        log_error(
            "  No default StorageClass found in the cluster. MLRun CE requires a default "
            "StorageClass for its PVCs."
        )
        return False

    # Every match, not just the first: a cluster with two defaults is a misconfiguration
    # Kubernetes does not reject, and which one wins is then up to the API server. Naming
    # only matched[0] reported it as a healthy single default.
    names = [row.split("=", 1)[0] for row in matched]
    log_info("  Default StorageClass: {}".format(", ".join(names)))
    if len(names) > 1:
        log_warn(
            f"  {len(names)} StorageClasses are marked default; Kubernetes picks one "
            "arbitrarily for PVCs that do not name a class."
        )
    return True


def validate_ingress_controller(settings: Settings) -> bool:
    """Warning only: --enable-ingress flips the chart's own Ingress resources on, but the
    installer never installs a controller for them (bring-your-own-controller only)."""
    if not settings.enable_ingress:
        return True

    if kubectl(settings, "get", "ingressclass", settings.ingress_class).ok:
        log_info(f"  Ingress: IngressClass '{settings.ingress_class}' found")
        return True

    log_warn(f"  Ingress: no IngressClass named '{settings.ingress_class}' found in the cluster.")
    log_warn(
        "  --enable-ingress only configures the chart's Ingress resources — it does "
        "not install a controller."
    )
    log_warn(
        "  Install one providing that class (e.g. https://kubernetes.github.io/ingress-nginx/) "
        "or the Ingress won't resolve."
    )
    return True


def validate_registry_auth(settings: Settings) -> bool:
    """Warning only: best-effort docker login with the resolved registry credentials."""
    if settings.local_registry:
        log_info(
            "  Registry auth: skipped (--local-registry in use, no external registry to check)"
        )
        return True
    if not settings.registry_username_value or not settings.registry_password_value:
        name = settings.registry_secret_name
        if settings.skip_registry_secret:
            reason = f"--skip-secret, using the existing secret '{name}'"
        elif settings.registry_secret_reused:
            reason = f"reusing the installer's secret '{name}' from a previous install"
        elif settings.values_file and not settings.config_file:
            reason = "-f-only mode, the values file names the pull secret"
        else:
            reason = f"secret '{name}' already exists and is used as it stands"
        log_info(f"  Registry auth: skipped ({reason})")
        return True
    if not docker_available():
        log_info(
            "  Registry auth: skipped (docker not available — this check is the only use for it)"
        )
        return True
    if settings.dry_run:
        # A dry run sends the credentials nowhere: the login is the only validator that
        # hands them to a third party.
        log_info("  Registry auth: skipped (--dry-run)")
        return True

    server = settings.registry_server_value or DEFAULT_DOCKER_SERVER
    # A throwaway --config, because a successful login stores the credentials in the
    # invoking user's ~/.docker/config.json — a check has no business doing that.
    with tempfile.TemporaryDirectory(prefix="mlrun-ce-docker-") as docker_config:
        result = run(
            ["docker", "--config", docker_config, "login", server,
             "-u", settings.registry_username_value, "--password-stdin"],
            input_data=settings.registry_password_value,
        )  # fmt: skip
    if result.ok:
        log_info(f"  Registry auth: login to {server} succeeded")
    else:
        log_warn(
            f"  Registry auth: could not log in to {server} with the provided credentials "
            "(best-effort check; install will continue)"
        )
    return True


def nodeports_held_elsewhere(settings: Settings) -> Dict[int, str]:
    """Every NodePort in the cluster held by a Service this release does not own, mapped
    to that Service's `namespace/name`."""
    result = kubectl(
        settings, "get", "svc", "--all-namespaces", "-o", "jsonpath=" + NODEPORT_JSONPATH
    )
    # Only Services this release already owns are skipped, because those NodePorts are ours
    # to take back on an upgrade. Everything else counts, including the rest of the target
    # namespace: helm will not adopt a Service it does not own, so an unrelated one sitting
    # on 30040 fails the install on a port the bash version reported as clear.
    held: Dict[int, str] = {}
    for line in result.out.splitlines() if result.ok else []:
        fields = line.split()
        if not fields:
            continue
        namespace, name, release = [*fields[0].split("/", 2), "", ""][:3]
        if namespace == settings.namespace and release == settings.release_name:
            continue
        for port in fields[1:]:
            if port.isdigit():
                held[int(port)] = f"{namespace}/{name}"
    return held


def _nodeport_keys_in_use(settings: Settings, user_values: dict) -> List[str]:
    """The NODEPORTS keys whose Service this install creates as a NodePort."""
    off = set()
    if settings.disable_system_monitoring:
        off |= {key for key in NODEPORTS if key.startswith("kube-prometheus-stack.")}
    if settings.disable_model_monitoring:
        off.add("timescaledb.service.nodePort")
    keys = []
    for key in NODEPORTS:
        service_type = _lookup(user_values, key.rsplit(".", 1)[0] + ".type")
        if key not in off and service_type in (None, "NodePort"):
            keys.append(key)
    return keys


def _free_nodeport(start: int, taken: Set[int]) -> Optional[int]:
    after = [port for port in NODEPORT_RANGE if port > start]
    before = [port for port in NODEPORT_RANGE if port < start]
    return next((port for port in after + before if port not in taken), None)


def resolve_nodeport_conflicts(settings: Settings) -> bool:
    """Move a busy NodePort to a free one, warning; refuse a busy one the user set in -f.

    The chart's NOTES, the URL table, MLRUN_GRAFANA_URL and Jupyter's link to the MLRun UI
    all read the port from the values, so writing the new port there is enough for every
    URL to follow. Returns False when a port the user chose is taken.
    """
    held = nodeports_held_elsewhere(settings)
    user_values = _values_file_contents(settings)
    keys = _nodeport_keys_in_use(settings, user_values)

    def port_of(key: str) -> int:
        chosen = _lookup(user_values, key)
        return int(chosen) if chosen else settings.node_ports.get(key, NODEPORTS[key])

    taken = set(held) | {port_of(key) for key in keys}
    refused = moved = 0
    for key in keys:
        port = port_of(key)
        holder = held.get(port)
        if not holder:
            continue
        if _lookup(user_values, key):
            log_error(
                f"  NodePort {port} ({key}, set in {settings.values_file}) is already held "
                f"by Service {holder}. Choose a free port for {key}."
            )
            refused += 1
            continue
        free = _free_nodeport(port, taken)
        if free is None:
            log_error(f"  NodePort {port} ({key}) is held by Service {holder}, and none is free.")
            refused += 1
            continue
        taken.add(free)
        settings.node_ports[key] = free
        moved += 1
        host = settings.external_host_address
        url = f" (http://{host}:{free})" if host else ""
        log_warn(
            f"  NodePort {port} ({key}) is held by Service {holder}; using {free} instead{url}."
        )
    if moved:
        log_warn("  The URLs printed after the install use the new port(s).")
        if effective_context(settings).startswith("kind-"):
            log_warn(
                "  kind reaches a NodePort only if the cluster maps it (extraPortMappings); "
                "reach a moved one with `kubectl port-forward`."
            )
    if not moved and not refused:
        log_info("  NodePorts: no conflicts detected")
    return not refused


def allocatable_to_ki(raw: str) -> Optional[int]:
    """Convert a Kubernetes allocatable-resource quantity to Ki.

    Memory is always Ki-suffixed, but ephemeral-storage is commonly reported as a bare byte
    count (no suffix) depending on the underlying cAdvisor source — handle both, plus
    Mi/Gi/Ti for good measure.
    """
    raw = raw.strip()
    units = {"Ki": 1, "Mi": 1024, "Gi": 1024 * 1024, "Ti": 1024 * 1024 * 1024}
    for suffix, factor in units.items():
        match = re.fullmatch(r"(\d+)" + suffix, raw)
        if match:
            return int(match.group(1)) * factor
    if re.fullmatch(r"\d+", raw):
        return int(raw) // 1024
    return None


def validate_node_capacity(settings: Settings) -> bool:
    """Warning only: cluster-wide allocatable RAM/storage above the documented 8Gi floor."""

    def total(jsonpath: str) -> int:
        result = kubectl(settings, "get", "nodes", "-o", "jsonpath=" + jsonpath)
        acc = 0
        for line in result.out.splitlines() if result.ok else []:
            if not line.strip():
                continue
            value = allocatable_to_ki(line)
            if value is not None:
                acc += value
        return acc

    mem_ki = total(r'{range .items[*]}{.status.allocatable.memory}{"\n"}{end}')
    disk_ki = total(r'{range .items[*]}{.status.allocatable.ephemeral-storage}{"\n"}{end}')

    if mem_ki == 0 and disk_ki == 0:
        log_warn("  Could not determine node capacity; skipping check.")
        return True

    mem_gi = mem_ki // 1024 // 1024
    disk_gi = disk_ki // 1024 // 1024
    if mem_gi < 8:
        log_warn(
            f"  Node capacity: total allocatable memory ~{mem_gi}Gi is below the documented "
            "floor of 8Gi"
        )
    else:
        log_info(f"  Node capacity: total allocatable memory ~{mem_gi}Gi")

    if disk_gi < 8:
        log_warn(
            f"  Node capacity: total allocatable ephemeral storage ~{disk_gi}Gi is below the "
            "documented floor of 8Gi"
        )
    else:
        log_info(f"  Node capacity: total allocatable ephemeral storage ~{disk_gi}Gi")
    return True


def validate_node_architecture(settings: Settings) -> bool:
    """Warning only: MLRun publishes some images for amd64 alone, which arm64 nodes refuse."""
    result = kubectl(
        settings, "get", "nodes", "-o", "jsonpath={.items[*].status.nodeInfo.architecture}"
    )
    architectures = set(result.out.split()) if result.ok else set()
    if "arm64" not in architectures:
        return True

    tag = settings.mlrun_version or chart_app_version(settings) or "$MLRUN_VERSION"
    log_warn(
        f"  Node architecture: arm64. MLRun publishes {', '.join(MLRUN_AMD64_ONLY_IMAGES)} "
        "for amd64 only, so those pods fail with ImagePullBackOff unless the amd64 images are "
        "on the node first. On Docker Desktop, pull them before installing:"
    )
    for image in MLRUN_AMD64_ONLY_IMAGES:
        log_warn(f"    docker pull --platform linux/amd64 quay.io/mlrun/{image}:{tag}")
    log_warn(
        "  Build MLRun functions for amd64: func.spec.build.extra_args = "
        '"--custom-platform=linux/amd64"'
    )
    log_warn(f"  Docker Desktop's kind cluster needs one more step: {README_URL}{ARM64_ANCHOR}")
    return True


def run_blocking_validators(settings: Settings) -> None:
    """The checks that can stop an install, run before anything mutates the cluster.

    They used to run alongside the warnings, after the namespace, the local registry and
    the pull secret had been created, so a missing StorageClass left all three behind.
    Neither needs anything those steps produce.
    """
    log_info("Running pre-install validators...")
    failed = not validate_helm_version(settings)
    failed |= not validate_storage_class(settings)
    if failed:
        raise die(
            "One or more required pre-install checks failed (see above). Bypass with "
            "--skip-validators if you must proceed anyway."
        )


def run_validators(settings: Settings) -> None:
    """The checks that need the run's values resolved: registry auth needs the credentials,
    the NodePorts the -f file. All warn and continue, except a busy NodePort set in -f."""
    log_info("Running the remaining pre-install validators...")
    validate_k8s_version(settings)
    validate_registry_auth(settings)
    validate_ingress_controller(settings)
    ports_ok = resolve_nodeport_conflicts(settings)
    validate_node_capacity(settings)
    validate_node_architecture(settings)
    if not ports_ok:
        raise die(
            "A NodePort set in the values file is already taken (see above); helm would "
            "fail on it. Set a free port, or remove the key to let the installer pick one."
        )
    log_info("Pre-install validation passed.")
