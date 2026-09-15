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

Blocking: Helm version and default StorageClass. Everything else reports and continues —
`run_validators` collects failures and exits once at the end rather than stopping at the
first one, so a user fixes everything in one pass.
"""

import re
from typing import List, Optional, Tuple

from .console import die, log_error, log_info, log_warn
from .settings import DEFAULT_DOCKER_SERVER, REQUIRED_NODEPORTS, Settings
from .shell import docker_available, helm, kubectl, run

STORAGECLASS_JSONPATH = (
    r'{range .items[*]}{.metadata.name}{"="}'
    r"{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}"
    r'{"="}'
    r"{.metadata.annotations.storageclass\.beta\.kubernetes\.io/is-default-class}"
    r'{"\n"}{end}'
)

NODEPORT_JSONPATH = (
    r'{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{"\n"}{end}{end}'
)


def parse_major_minor(raw: str) -> Optional[Tuple[int, int]]:
    match = re.search(r"v(\d+)\.(\d+)", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def validate_k8s_version(settings: Settings) -> bool:
    """Informational: reports the cluster's Kubernetes version.

    Never blocks — neither the chart nor its README states a required cluster version.
    Warns only against an explicitly set MIN_K8S_VERSION.
    """
    result = kubectl(
        settings, "get", "nodes", "-o", "jsonpath={.items[0].status.nodeInfo.kubeletVersion}"
    )
    raw = result.out.strip() if result.ok else ""
    if not raw:
        log_warn("  Could not determine Kubernetes version; skipping version check.")
        return True

    parsed = parse_major_minor(raw)
    if parsed is None:
        log_warn(f"  Could not parse Kubernetes version '{raw}'; skipping version check.")
        return True

    major, minor = parsed
    if settings.min_k8s_major is None:
        log_info(f"  Kubernetes version: {major}.{minor}")
        return True

    found = f"{major}.{minor}"
    floor = f"{settings.min_k8s_major}.{settings.min_k8s_minor}"
    if (major, minor) < (settings.min_k8s_major, settings.min_k8s_minor):
        log_warn(f"  Kubernetes version {found} is below the requested minimum ({floor}).")
        return True
    log_info(f"  Kubernetes version: {found} (>= {floor} required)")
    return True


def validate_helm_version(settings: Settings) -> bool:
    """Blocking: the helm CLI version must be at or above the configured floor."""
    result = helm(settings, "version", "--short")
    raw = result.out.strip() if result.ok else ""
    if not raw:
        log_warn("  Could not determine Helm version; skipping version check.")
        return True

    parsed = parse_major_minor(raw)
    if parsed is None:
        log_warn(f"  Could not parse Helm version '{raw}'; skipping version check.")
        return True

    major, minor = parsed
    found = f"{major}.{minor}"
    floor = f"{settings.min_helm_major}.{settings.min_helm_minor}"
    if (major, minor) < (settings.min_helm_major, settings.min_helm_minor):
        log_error(f"  Helm version {found} is below the minimum supported version ({floor}).")
        return False
    log_info(f"  Helm version: {found} (>= {floor} required)")
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
    log_info("  Default StorageClass: {}".format("\n".join(matched).split("=", 1)[0]))
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
        log_info("  Registry auth: skipped (no registry credentials resolved, e.g. -f-only mode)")
        return True
    if not docker_available():
        log_info(
            "  Registry auth: skipped (docker not available — this check is the only use for it)"
        )
        return True

    server = settings.registry_server_value or DEFAULT_DOCKER_SERVER
    result = run(
        ["docker", "login", server, "-u", settings.registry_username_value, "--password-stdin"],
        input_data=settings.registry_password_value,
    )
    if result.ok:
        log_info(f"  Registry auth: login to {server} succeeded")
    else:
        log_warn(
            f"  Registry auth: could not log in to {server} with the provided credentials "
            "(best-effort check; install will continue)"
        )
    return True


def validate_nodeport_conflicts(settings: Settings) -> bool:
    """Warning only: the chart's fixed NodePorts already bound by another Service."""
    result = kubectl(
        settings, "get", "svc", "--all-namespaces", "-o", "jsonpath=" + NODEPORT_JSONPATH
    )
    # Mirrors `awk -v ns=NS '$1 != ns { print $2 }'`: the jsonpath emits the namespace once
    # per Service followed by one nodePort per line, so only the first line of each Service
    # carries the namespace in $1.
    used: List[str] = []
    for line in result.out.splitlines() if result.ok else []:
        fields = line.split()
        if fields and fields[0] != settings.namespace:
            used.append(fields[1] if len(fields) > 1 else "")

    conflicts = [str(port) for port in REQUIRED_NODEPORTS if str(port) in used]
    if conflicts:
        log_warn(
            "  NodePort conflict: already in use by another Service outside namespace "
            "'{}': {}".format(settings.namespace, " ".join(conflicts))
        )
    else:
        log_info("  NodePorts: no conflicts detected")
    return True


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


def run_validators(settings: Settings) -> None:
    log_info("Running pre-install validators...")
    failed = False

    validate_k8s_version(settings)
    failed |= not validate_helm_version(settings)
    failed |= not validate_storage_class(settings)
    validate_registry_auth(settings)
    validate_ingress_controller(settings)
    validate_nodeport_conflicts(settings)
    validate_node_capacity(settings)

    if failed:
        raise die(
            "One or more required pre-install checks failed (see above). Bypass with "
            "--skip-validators if you must proceed anyway."
        )
    log_info("Pre-install validation passed.")
