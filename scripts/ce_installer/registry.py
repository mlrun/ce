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

import base64
import json
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


DOCKER_CONFIG_SECRET = """apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {namespace}
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {payload}
"""


def docker_config_secret(
    *, name: str, namespace: str, server: str, username: str, password: str, email: str
) -> str:
    """Render the pull secret `kubectl create secret docker-registry` would have produced.

    Building the manifest here and piping it on stdin keeps the password out of argv, where
    it would otherwise sit in the process table for the length of the call and be printed
    verbatim by `run`'s failure path. The field order and the `auth` duplicate of
    user:pass are what kubectl emits, so a secret created either way compares equal.
    """
    payload = json.dumps(
        {
            "auths": {
                server: {
                    "username": username,
                    "password": password,
                    "email": email,
                    "auth": base64.b64encode(f"{username}:{password}".encode()).decode(),
                }
            }
        }
    )
    return DOCKER_CONFIG_SECRET.format(
        name=name,
        namespace=namespace,
        payload=base64.b64encode(payload.encode()).decode(),
    )


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


def ingress_controller_candidates(settings: Settings) -> List[str]:
    """Where to look for the ingress controller Service, best guess first.

    An explicit `namespace/name` always wins. Otherwise try the namespace ingress-nginx
    installs itself into by default, then the release namespace — the latter is where the
    bash installer looked, and is right only when the user put the controller there.
    Anything else (Traefik, a vendored controller, a non-standard namespace) has to say so
    via the override rather than be guessed at.
    """
    if settings.ingress_controller_service:
        return [settings.ingress_controller_service]
    return [
        "ingress-nginx/ingress-nginx-controller",
        f"{settings.namespace}/ingress-nginx-controller",
    ]


def resolve_ingress_controller_ip(settings: Settings) -> str:
    for candidate in ingress_controller_candidates(settings):
        namespace, _, name = candidate.partition("/")
        if not namespace or not name:
            raise die(
                f"Invalid ingress controller Service reference '{candidate}' "
                "(expected 'namespace/name')."
            )
        result = kubectl(
            settings,
            "get",
            "svc",
            name,
            "--namespace",
            namespace,
            "-o",
            "jsonpath={.spec.clusterIP}",
        )
        clusterip = result.out.strip() if result.ok else ""
        if clusterip:
            return clusterip
    return ""


def patch_coredns_for_registry(settings: Settings, registry_host: str) -> None:
    clusterip = resolve_ingress_controller_ip(settings)
    if not clusterip:
        log_warn("Could not get ingress controller ClusterIP; skipping CoreDNS patch.")
        log_warn(
            "Set INGRESS_CONTROLLER_SERVICE (or installer.localRegistry."
            "ingressControllerService) to 'namespace/name' if your controller is elsewhere."
        )
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
    # An unreadable or empty Corefile must not be treated as "no entries yet": the patch
    # below would then apply an empty Corefile and restart CoreDNS, taking cluster DNS down
    # while reporting success. There is nothing to patch safely, so leave it alone.
    if not corefile_result.ok or not corefile_result.out.strip():
        log_warn("Could not read the CoreDNS Corefile; skipping CoreDNS patch.")
        log_warn(f"Pods may not resolve {registry_host} — add a hosts entry manually if needed.")
        return
    corefile = corefile_result.out

    if registry_host in corefile:
        log_info(f"CoreDNS already has an entry for {registry_host}; skipping patch.")
        return

    patched = insert_hosts_entry(corefile, clusterip, registry_host)
    # insert_hosts_entry has nowhere to put the entry in a Corefile with neither a hosts{}
    # block nor a forward line, and returns it unchanged rather than guessing.
    if f"{clusterip} {registry_host}" not in patched:
        log_warn("Could not find a place to insert the hosts entry; skipping CoreDNS patch.")
        log_warn(f"Pods may not resolve {registry_host} — add a hosts entry manually if needed.")
        return

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
        "apply",
        "-f",
        "-",
        "--namespace",
        settings.namespace,
        input_data=docker_config_secret(
            name=settings.registry_secret_name,
            namespace=settings.namespace,
            server=settings.local_registry_url,
            username="local",
            password="local",
            email="local@local",
        ),
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
    password = env_str("REGISTRY_PASSWORD")
    if not password and settings.registry_password_file:
        password_path = Path(settings.registry_password_file)
        if not password_path.is_file():
            raise die(
                "REGISTRY_PASSWORD_FILE is set but the file does not exist: "
                f"{settings.registry_password_file}"
            )
        # Kept in a local rather than exported into os.environ: everything the installer
        # runs afterwards — helm, kubectl, docker — inherits this process's environment,
        # and a file-supplied password exists precisely so it is not sitting in one.
        #
        # Strip the trailing newline a file almost always carries at EOF; leaving it in
        # produces a secret that fails auth in a way that is painful to trace back here.
        password = password_path.read_text().strip("\r\n")

    if not password:
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
        "apply",
        "-f",
        "-",
        "--namespace",
        settings.namespace,
        input_data=docker_config_secret(
            name=settings.registry_secret_name,
            namespace=settings.namespace,
            server=server,
            username=username,
            password=password,
            email=email,
        ),
        check=True,
    )
