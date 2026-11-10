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
"""Registry pull secret, the optional in-cluster registry, and the CoreDNS hosts report."""

import base64
import ipaddress
import json
import re
from pathlib import Path
from typing import List

from .cluster import resolve_local_registry_url
from .config import report_missing_configuration
from .console import InstallerError, add_secret, die, log_error, log_info, log_warn
from .settings import DEFAULT_DOCKER_SERVER, Settings, confirm, env_str, prompt_or_env
from .shell import kubectl

# Everything the installer creates outside the chart carries this label, so --uninstall can
# remove exactly that and never a same-named object the user brought (a --skip-secret
# pull secret, say).
MANAGED_BY_KEY = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "mlrun-ce-installer"
MANAGED_BY_LABEL = f"{MANAGED_BY_KEY}={MANAGED_BY_VALUE}"
_MANAGED_BY_YAML = f"{MANAGED_BY_KEY}: {MANAGED_BY_VALUE}"
_MANAGED_BY_JSONPATH = "{.metadata.labels." + MANAGED_BY_KEY.replace(".", "\\.") + "}"

LOCAL_REGISTRY_IMAGE = "registry:2.8.3"
LOCAL_REGISTRY_STORAGE = "10Gi"

LOCAL_REGISTRY_MANIFEST = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: local-registry
  namespace: {{namespace}}
  labels:
    {_MANAGED_BY_YAML}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {LOCAL_REGISTRY_STORAGE}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: local-registry
  namespace: {{namespace}}
  labels:
    {_MANAGED_BY_YAML}
spec:
  replicas: 1
  # ReadWriteOnce: a rolling update's new pod could not mount the volume the old one holds.
  strategy:
    type: Recreate
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
          image: {LOCAL_REGISTRY_IMAGE}
          ports:
            - containerPort: 5000
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: data
              mountPath: /var/lib/registry
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: local-registry
---
apiVersion: v1
kind: Service
metadata:
  name: local-registry
  namespace: {{namespace}}
  labels:
    {_MANAGED_BY_YAML}
spec:
  selector:
    app: local-registry
  type: ClusterIP
  ports:
    - port: 5000
      targetPort: 5000
"""

LOCAL_REGISTRY_INGRESS = f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: local-registry
  namespace: {{namespace}}
  labels:
    {_MANAGED_BY_YAML}
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
spec:
  ingressClassName: {{ingress_class}}
  rules:
    - host: registry.{{host}}
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


DOCKER_CONFIG_SECRET = f"""apiVersion: v1
kind: Secret
metadata:
  name: {{name}}
  namespace: {{namespace}}
  labels:
    {_MANAGED_BY_YAML}
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {{payload}}
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


WILDCARD_DNS_SUFFIXES = (".nip.io", ".sslip.io")


def hosts_file_ip(external_host: str) -> str:
    """The address an /etc/hosts line for `<sub>.<external_host>` should carry, or "" when
    the names already resolve on their own.

    /etc/hosts takes an IP, never a name. localhost, and host.docker.internal on Docker
    Desktop, are 127.0.0.1. A wildcard-DNS name needs no line. Any other name is a domain
    only its DNS can answer for, so the line is written against a placeholder for the user
    to fill in.
    """
    if external_host.endswith(WILDCARD_DNS_SUFFIXES):
        return ""
    if external_host in ("localhost", "host.docker.internal"):
        return "127.0.0.1"
    try:
        ipaddress.ip_address(external_host)
    except ValueError:
        return "<ingress controller IP>"
    return external_host


def report_coredns_entry_for_registry(settings: Settings, registry_host: str) -> None:
    """Print the CoreDNS hosts entry a local-registry install needs, and how to add it.

    This used to edit the cluster's CoreDNS ConfigMap and restart the Deployment. That is
    shared, cluster-wide infrastructure owned by nobody in this namespace: the rewrite was
    a regex over a file whose format the installer does not control, an unrelated hosts{}
    block could be rewritten, the entry survived `--uninstall`, and getting it wrong takes
    DNS down for every workload on the cluster, not just MLRun. An installer scoped to one
    release should not be making that edit unasked, so it reports and lets the operator
    decide.

    Nothing here mutates anything, so it runs under --dry-run unchanged.
    """
    clusterip = resolve_ingress_controller_ip(settings)
    log_warn(f"Pods cannot resolve {registry_host} until CoreDNS knows about it.")
    if not clusterip:
        log_warn("  Could not find the ingress controller Service to get its ClusterIP.")
        log_warn(
            "  Set INGRESS_CONTROLLER_SERVICE (or installer.localRegistry."
            "ingressControllerService) to 'namespace/name' if your controller is elsewhere."
        )
        return

    log_warn("  Add this to the hosts{} block of the CoreDNS Corefile:")
    log_warn(f"      {clusterip} {registry_host}")
    log_warn("  Then: kubectl -n kube-system edit configmap coredns")
    log_warn("        kubectl -n kube-system rollout restart deployment/coredns")


LOCAL_REGISTRY_KINDS = ("pvc", "deployment", "service")


def _refuse_foreign_registry_objects(settings: Settings) -> None:
    """Stop when an object the registry manifest would apply exists without our label.

    `apply` would add the label to it, and `--uninstall` deletes by that label, so a
    Deployment or Service of the user's that happens to be called `local-registry` would
    be adopted on install and destroyed on uninstall. Unlike the pull secret it is not
    used as it stands: nothing says a stranger's objects add up to a working registry, and
    its URL is about to be handed to Nuclio. Read-only, so a dry run reports it too.
    """
    kinds = LOCAL_REGISTRY_KINDS + (("ingress",) if settings.enable_ingress else ())
    foreign = []
    for kind in kinds:
        found = kubectl(
            settings,
            "get",
            kind,
            "local-registry",
            "--namespace",
            settings.namespace,
            "-o",
            # The literal prefix makes an existing but unlabelled object print something.
            "jsonpath=exists:" + _MANAGED_BY_JSONPATH,
        )
        out = found.out.strip() if found.ok else ""
        if out.startswith("exists:") and out != f"exists:{MANAGED_BY_VALUE}":
            foreign.append(f"{kind}/local-registry")
    if not foreign:
        return
    log_error(
        f"Namespace '{settings.namespace}' already has objects the local registry would take "
        f"over: {', '.join(foreign)}."
    )
    log_error("  They were not created by this installer, and --uninstall would delete them.")
    log_error(
        "  Delete them, install into another namespace, or drop --local-registry and point "
        "REGISTRY_URL at the registry they serve."
    )
    raise InstallerError(code=1)


def prepare_local_registry(settings: Settings) -> None:
    """The read-only half of the local registry, run before anything is created: its URL,
    which the pull secret and the --set flags need, and the check for objects in the way."""
    settings.local_registry_url = resolve_local_registry_url(settings)
    if settings.enable_ingress and settings.external_host_address in ("localhost", "127.0.0.1"):
        log_warn(
            f"The registry would be {settings.local_registry_url}, which a pod may resolve to "
            "itself rather than to the ingress controller, so builds could fail to push."
        )
        log_warn(
            "  The tested setup is EXTERNAL_HOST_ADDRESS=host.docker.internal "
            "(Docker Desktop) or the address of a node."
        )
    _refuse_foreign_registry_objects(settings)


def deploy_local_registry(settings: Settings) -> None:
    # Everything below mutates the cluster, which a dry run must not do — and on a cluster
    # where the namespace does exist, an unguarded apply would quietly deploy a real registry.
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
    report_coredns_entry_for_registry(settings, settings.local_registry_url)

    hosts_ip = hosts_file_ip(settings.external_host_address)
    if hosts_ip:
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


SECRET_ABSENT, SECRET_OURS, SECRET_FOREIGN = "absent", "ours", "foreign"


def registry_secret_owner(settings: Settings) -> str:
    """Whether the pull secret is absent, the installer's (labelled), or someone else's."""
    existing = kubectl(
        settings,
        "get",
        "secret",
        settings.registry_secret_name,
        "--namespace",
        settings.namespace,
        "-o",
        # The literal prefix makes an existing but unlabelled secret print something.
        "jsonpath=exists:" + _MANAGED_BY_JSONPATH,
    )
    out = existing.out.strip() if existing.ok else ""
    if not out.startswith("exists:"):
        return SECRET_ABSENT
    return SECRET_OURS if out == f"exists:{MANAGED_BY_VALUE}" else SECRET_FOREIGN


def _credentials_supplied(settings: Settings) -> bool:
    return bool(env_str("REGISTRY_PASSWORD") or settings.registry_password_file)


def prepare_registry_secret(settings: Settings) -> bool:
    """Settle the pull secret before anything is created; True when it has to be written.

    Every prompt happens here, so a run cancelled at a prompt has changed nothing. A secret
    without the installer's label is the user's: `apply` would add the label to it, and
    `--uninstall` deletes by that label, so one created by hand as the README describes
    would be adopted on install and destroyed on uninstall. It is used as it stands, exactly
    as with --skip-secret, and nothing is asked for it. Reads only, so a dry run reports what
    the real run would do.
    """
    name = settings.registry_secret_name
    if settings.skip_registry_secret:
        log_info("Skipping registry secret creation (--skip-secret or SKIP_REGISTRY_SECRET)")
        verify_existing_registry_secret(settings)
        return False

    owner = registry_secret_owner(settings)
    missing = settings.missing_registry_credentials
    if owner == SECRET_OURS and missing:
        log_info(
            f"Reusing secret '{name}' from a previous install: no registry credentials were "
            "supplied to replace it."
        )
        settings.registry_secret_reused = True
        return False
    if missing:
        report_missing_configuration(missing)

    if owner == SECRET_FOREIGN:
        log_warn(f"Secret '{name}' already exists and was not created by this installer.")
        log_warn("  Using it unchanged; no registry credentials are asked for or written.")
        log_warn(
            "  To have the installer manage it, delete it first. "
            "Pass --skip-secret to silence this."
        )
        return False

    if settings.local_registry:
        return True

    # A re-run would otherwise ask for a password it already stored. Only offered when the
    # run was not handed credentials, which are then meant to replace the stored ones. A dry
    # run asks nothing about the registry, so it takes the answer that changes nothing.
    if (
        owner == SECRET_OURS
        and not _credentials_supplied(settings)
        and (
            settings.dry_run
            or (
                not settings.non_interactive
                and confirm(
                    settings,
                    f"Secret '{name}' from a previous install exists in "
                    f"'{settings.namespace}'. Reuse it?",
                )
            )
        )
    ):
        log_info(f"Reusing secret '{name}' (same as --skip-secret).")
        settings.registry_secret_reused = True
        return False

    collect_registry_credentials(settings)
    return True


def _is_immutable_type_change(error: str) -> bool:
    # ValidateSecretUpdate: `type: Invalid value: "<new type>": field is immutable`.
    return "field is immutable" in error and re.search(r"\btype: Invalid value", error) is not None


def _apply_registry_secret(settings: Settings, manifest: str) -> None:
    """Create or update the pull secret in a single call.

    `apply` updates an existing Secret in place. The bash installer deleted first because
    its `kubectl create secret` refused to overwrite, and the port inherited that even
    after moving to `apply` — which left a window where the release had no credentials,
    and lost them outright if anything failed between the delete and the create.

    The one thing `apply` cannot do is change a Secret's `type`, which is immutable, so a
    name already taken by an Opaque secret still needs a replace. Fall back only there:
    matched on the API server's own wording, because a Forbidden, a conflict or a timeout
    would otherwise delete working credentials and then likely fail the retry the same way.
    """
    name = settings.registry_secret_name
    applied = kubectl(
        settings, "apply", "-f", "-", "--namespace", settings.namespace, input_data=manifest
    )
    if applied.ok:
        return

    if not _is_immutable_type_change(applied.err):
        log_error(f"Could not create or update secret '{name}'; the existing one is untouched.")
        log_error(f"  {applied.err.strip() or applied.out.strip()}")
        raise InstallerError(code=1)

    log_warn(f"Secret '{name}' exists with a different type, which cannot change in place.")
    log_warn("  Replacing it.")
    deleted = kubectl(
        settings,
        "delete",
        "secret",
        name,
        "--namespace",
        settings.namespace,
        "--ignore-not-found",
    )
    if not deleted.ok:
        log_error(f"Could not delete secret '{name}' to replace it; it is unchanged.")
        log_error(f"  {deleted.err.strip() or deleted.out.strip()}")
        raise InstallerError(code=1)
    kubectl(
        settings,
        "apply",
        "-f",
        "-",
        "--namespace",
        settings.namespace,
        input_data=manifest,
        check=True,
    )


def collect_registry_credentials(settings: Settings) -> None:
    if settings.dry_run:
        # Nothing is pushed, pulled or logged in to, so there is nothing to ask for; the
        # password is not even read.
        settings.registry_username_value = (
            env_str("REGISTRY_USERNAME") or settings.config_registry_username
        )
        settings.registry_server_value = (
            env_str("REGISTRY_SERVER") or settings.config_registry_server or DEFAULT_DOCKER_SERVER
        )
        settings.registry_email_value = env_str("REGISTRY_EMAIL") or settings.config_registry_email
        log_info("Dry run: registry credentials are not asked for.")
        return

    will_prompt = not (env_str("REGISTRY_USERNAME") and _credentials_supplied(settings))
    if will_prompt and not settings.non_interactive:
        log_info(
            "MLRun builds an image for each function you deploy and pushes it to a container "
            "registry, which the cluster then pulls it from."
        )
        log_info(
            "  Give one you can push to (a Docker Hub username and access token is enough), "
            "or re-run with --local-registry to run one inside the cluster."
        )

    username = prompt_or_env(
        settings,
        "REGISTRY_USERNAME",
        "Docker registry username",
        settings.config_registry_username,
    )

    # REGISTRY_PASSWORD (env) > REGISTRY_PASSWORD_FILE > interactive masked prompt. The
    # config file may name the password file, never the password.
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
        settings,
        "REGISTRY_EMAIL",
        "Docker registry email",
        settings.config_registry_email,
        allow_empty=True,
    )

    if not username or not password:
        raise die("Registry username and password are required.")

    add_secret(password)
    settings.registry_username_value = username
    settings.registry_password_value = password
    settings.registry_server_value = server
    settings.registry_email_value = email


def apply_registry_secret(settings: Settings) -> None:
    """Write the pull secret prepare_registry_secret decided on."""
    name = settings.registry_secret_name
    kind = "local registry secret" if settings.local_registry else "Docker registry secret"
    if settings.dry_run:
        log_info(f"Dry-run: would create {kind} '{name}'")
        return

    log_info(f"Creating {kind} '{name}'...")
    if settings.local_registry:
        server, username, password, email = (
            settings.local_registry_url, "local", "local", "local@local",
        )  # fmt: skip
    else:
        server = settings.registry_server_value
        username = settings.registry_username_value
        password = settings.registry_password_value
        email = settings.registry_email_value
    _apply_registry_secret(
        settings,
        docker_config_secret(
            name=name,
            namespace=settings.namespace,
            server=server,
            username=username,
            password=password,
            email=email,
        ),
    )
