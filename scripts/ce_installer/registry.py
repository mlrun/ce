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
"""Registry pull secret, the optional in-cluster registry, and the CoreDNS patch."""

import os
import re
from pathlib import Path
from typing import List

from .cluster import resolve_local_registry_url
from .console import InstallerError, die, log_error, log_info, log_warn
from .settings import DEFAULT_DOCKER_SERVER, Settings, env_str, prompt_or_env
from .shell import kubectl

LOCAL_REGISTRY_MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: local-registry
  namespace: {namespace}
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
  namespace: {namespace}
spec:
  selector:
    app: local-registry
  type: ClusterIP
  ports:
    - port: 5000
      targetPort: 5000
"""

LOCAL_REGISTRY_INGRESS = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: local-registry
  namespace: {namespace}
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
spec:
  ingressClassName: {ingress_class}
  rules:
    - host: registry.{host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: local-registry
                port:
                  number: 5000
"""


def insert_hosts_entry(corefile: str, ip: str, host: str) -> str:
    """Add an `ip host` line to CoreDNS's Corefile.

    CoreDNS only allows one hosts{} block per server. If one already exists (e.g. Docker
    Desktop adds host.docker.internal), insert the entry inside it before 'fallthrough'.
    Otherwise create a new hosts{} block before the first 'forward' line.
    """
    lines = corefile.splitlines()
    entry = f"        {ip} {host}"
    result: List[str] = []
    inserted = False

    if "hosts {" in corefile:
        in_hosts = False
        for line in lines:
            if "hosts {" in line:
                in_hosts = True
            if in_hosts and re.match(r"^\s*}", line):
                if not inserted:
                    result.append(entry)
                    inserted = True
                in_hosts = False
            elif not inserted and in_hosts and "fallthrough" in line:
                result.append(entry)
                inserted = True
            result.append(line)
    else:
        for line in lines:
            if not inserted and re.search(r"forward ", line):
                result.append("    hosts {")
                result.append(entry)
                result.append("        fallthrough")
                result.append("    }")
                inserted = True
            result.append(line)

    return "\n".join(result)


def patch_coredns_for_registry(settings: Settings, registry_host: str) -> None:
    ingress_ip = kubectl(
        settings,
        "get",
        "svc",
        "ingress-nginx-controller",
        "--namespace",
        settings.namespace,
        "-o",
        "jsonpath={.spec.clusterIP}",
    )
    clusterip = ingress_ip.out.strip() if ingress_ip.ok else ""
    if not clusterip:
        log_warn("Could not get ingress controller ClusterIP; skipping CoreDNS patch.")
        log_warn(f"Pods may not resolve {registry_host} — add a hosts entry manually if needed.")
        return

    corefile_result = kubectl(
        settings,
        "get",
        "configmap",
        "coredns",
        "-n",
        "kube-system",
        "-o",
        "jsonpath={.data.Corefile}",
    )
    corefile = corefile_result.out if corefile_result.ok else ""

    if registry_host in corefile:
        log_info(f"CoreDNS already has an entry for {registry_host}; skipping patch.")
        return

    patched = insert_hosts_entry(corefile, clusterip, registry_host)

    rendered = kubectl(
        settings,
        "create",
        "configmap",
        "coredns",
        f"--from-literal=Corefile={patched}",
        "--namespace",
        "kube-system",
        "--dry-run=client",
        "-o",
        "yaml",
    )
    kubectl(settings, "apply", "-f", "-", input_data=rendered.out)
    kubectl(settings, "rollout", "restart", "deployment/coredns", "--namespace", "kube-system")
    kubectl(
        settings,
        "rollout",
        "status",
        "deployment/coredns",
        "--namespace",
        "kube-system",
        "--timeout=60s",
    )

    log_info(f"CoreDNS patched: {registry_host} -> {clusterip}")


def deploy_local_registry(settings: Settings) -> None:
    # Resolve the URL early so create_registry_secret, which runs before
    # gather_install_params, already has it.
    settings.local_registry_url = resolve_local_registry_url(settings)

    # The guard sits after the URL is resolved, not before: the URL still has to reach the
    # rendered --set flags for a dry run to represent the real install. Everything below
    # this point mutates the cluster, which a dry run must not do — and on a cluster where
    # the namespace does exist, an unguarded apply would quietly deploy a real registry.
    if settings.dry_run:
        log_info(f"Dry-run: would deploy local registry at '{settings.local_registry_url}'")
        return

    log_info("Deploying local Docker registry...")
    kubectl(
        settings,
        "apply",
        "-f",
        "-",
        "--namespace",
        settings.namespace,
        input_data=LOCAL_REGISTRY_MANIFEST.format(namespace=settings.namespace),
        check=True,
    )

    if not settings.enable_ingress:
        return

    kubectl(
        settings,
        "apply",
        "-f",
        "-",
        "--namespace",
        settings.namespace,
        input_data=LOCAL_REGISTRY_INGRESS.format(
            namespace=settings.namespace,
            ingress_class=settings.ingress_class,
            host=settings.external_host_address,
        ),
        check=True,
    )
    settings.local_registry_url = f"registry.{settings.external_host_address}"
    log_info(f"Local registry ingress created: {settings.local_registry_url}")
    patch_coredns_for_registry(settings, settings.local_registry_url)

    # /etc/hosts needs a real IP; host.docker.internal is already localhost on Docker Desktop.
    hosts_ip = settings.external_host_address
    if hosts_ip == "host.docker.internal":
        hosts_ip = "127.0.0.1"
    log_warn("To push images from this machine, add to /etc/hosts:")
    log_warn(f"  {hosts_ip}  {settings.local_registry_url}")
    log_warn("In Docker Desktop: Settings -> Docker Engine -> add:")
    log_warn(f'  "insecure-registries": ["{settings.local_registry_url}"]')


def verify_existing_registry_secret(settings: Settings) -> None:
    if not kubectl(
        settings, "get", "secret", settings.registry_secret_name, "--namespace", settings.namespace
    ).ok:
        log_error(
            f"--skip-secret was used but secret '{settings.registry_secret_name}' "
            f"does not exist in namespace '{settings.namespace}'."
        )
        log_error(
            "Create it first (kubectl create secret docker-registry ...), or drop "
            "--skip-secret to let the installer create it."
        )
        raise InstallerError(code=1)


def _replace_existing_secret(settings: Settings) -> None:
    if kubectl(
        settings, "get", "secret", settings.registry_secret_name, "--namespace", settings.namespace
    ).ok:
        log_info(f"Secret '{settings.registry_secret_name}' already exists; replacing...")
        kubectl(
            settings,
            "delete",
            "secret",
            settings.registry_secret_name,
            "--namespace",
            settings.namespace,
        )


def _create_local_registry_secret(settings: Settings) -> None:
    if settings.dry_run:
        log_info(f"Dry-run: would create local registry secret '{settings.registry_secret_name}'")
        return

    _replace_existing_secret(settings)
    log_info(f"Creating local registry secret '{settings.registry_secret_name}'...")
    kubectl(
        settings,
        "create",
        "secret",
        "docker-registry",
        settings.registry_secret_name,
        "--namespace",
        settings.namespace,
        "--docker-server",
        settings.local_registry_url,
        "--docker-username",
        "local",
        "--docker-password",
        "local",
        "--docker-email",
        "local@local",
        check=True,
    )


def create_registry_secret(settings: Settings) -> None:
    if settings.local_registry:
        _create_local_registry_secret(settings)
        return

    username = prompt_or_env(
        settings,
        "REGISTRY_USERNAME",
        "Docker registry username",
        settings.config_registry_username,
    )

    # REGISTRY_PASSWORD (env) > REGISTRY_PASSWORD_FILE > interactive masked prompt. Never
    # settable via ce-config.yaml — env, file or prompt only.
    if not env_str("REGISTRY_PASSWORD") and settings.registry_password_file:
        password_path = Path(settings.registry_password_file)
        if not password_path.is_file():
            raise die(
                "REGISTRY_PASSWORD_FILE is set but the file does not exist: "
                f"{settings.registry_password_file}"
            )
        # Strip the trailing newline a file almost always carries at EOF; leaving it in
        # produces a secret that fails auth in a way that is painful to trace back here.
        os.environ["REGISTRY_PASSWORD"] = password_path.read_text().strip("\r\n")

    password = prompt_or_env(
        settings, "REGISTRY_PASSWORD", "Docker registry password", "", secret=True
    )
    server = prompt_or_env(
        settings,
        "REGISTRY_SERVER",
        "Docker server URL",
        settings.config_registry_server or DEFAULT_DOCKER_SERVER,
    )
    email = prompt_or_env(
        settings, "REGISTRY_EMAIL", "Docker registry email", settings.config_registry_email
    )

    if not username or not password:
        raise die("Registry username and password are required.")

    settings.registry_username_value = username
    settings.registry_password_value = password
    settings.registry_server_value = server

    if settings.dry_run:
        log_info(f"Dry-run: would create registry secret '{settings.registry_secret_name}'")
        return

    _replace_existing_secret(settings)
    log_info(f"Creating Docker registry secret '{settings.registry_secret_name}'...")
    kubectl(
        settings,
        "create",
        "secret",
        "docker-registry",
        settings.registry_secret_name,
        "--namespace",
        settings.namespace,
        "--docker-username",
        username,
        "--docker-password",
        password,
        "--docker-server",
        server,
        "--docker-email",
        email,
        check=True,
    )
