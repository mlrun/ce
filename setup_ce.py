#!/usr/bin/env python3
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests
import typer
import yaml

try:
    import colorama
except ImportError:
    colorama = None

app = typer.Typer(help="Manage MLRun CE installation & Telepresence intercept.")

REPO_URL = "git@github.com:mlrun/ce.git"

INGRESS_HOSTS = [
    {
        "host": "mlrun.k8s.internal",
        "paths": [{"path": "/", "serviceName": "mlrun-ui", "servicePort": 80}],
    },
    {
        "host": "mlrun-api.k8s.internal",
        "paths": [{"path": "/", "serviceName": "mlrun-api", "servicePort": 8080}],
    },
    {
        "host": "mlrun-api-chief.k8s.internal",
        "paths": [{"path": "/", "serviceName": "mlrun-api-chief", "servicePort": 8080}],
    },
    {
        "host": "nuclio.k8s.internal",
        "paths": [
            {"path": "/", "serviceName": "nuclio-dashboard", "servicePort": 8070}
        ],
    },
    {
        "host": "nuclio-dashboard.k8s.internal",
        "paths": [
            {"path": "/", "serviceName": "nuclio-dashboard", "servicePort": 8070}
        ],
    },
    {
        "host": "jupyter.k8s.internal",
        "paths": [{"path": "/", "serviceName": "mlrun-jupyter", "servicePort": 8888}],
    },
    {
        "host": "minio.k8s.internal",
        "paths": [{"path": "/", "serviceName": "minio-console", "servicePort": 9001}],
    },
    {
        "host": "grafana.k8s.internal",
        "paths": [{"path": "/", "serviceName": "grafana", "servicePort": 80}],
    },
    {
        "host": "kfp.k8s.internal",
        "paths": [
            {"path": "/", "serviceName": "ml-pipeline-ui", "servicePort": 80},
            {"path": "/apis/", "serviceName": "ml-pipeline", "servicePort": 8888},
        ],
    },
    {
        "host": "metadata-envoy.k8s.internal",
        "paths": [
            {"path": "/", "serviceName": "metadata-envoy-service", "servicePort": 9090}
        ],
    },
    {
        "host": "workflow-metrics.k8s.internal",
        "paths": [
            {
                "path": "/",
                "serviceName": "workflow-controller-metrics",
                "servicePort": 9091,
            }
        ],
    },
]

HELM_REPOS = {
    "mlrun": "https://mlrun.github.io/ce",
    "nuclio": "https://nuclio.github.io/nuclio/charts",
    "v3io-stable": "https://v3io.github.io/helm-charts/stable",
    "minio": "https://charts.min.io/",
    "spark-operator": "https://kubeflow.github.io/spark-operator",
    "prometheus-community": "https://prometheus-community.github.io/helm-charts",
}

METALLB_CONFIG_YAML = """apiVersion: v1
kind: ConfigMap
metadata:
  namespace: metallb-system
  name: config
data:
  config: |
    address-pools:
    - name: default
      protocol: layer2
      addresses:
      - 192.168.56.200-192.168.56.250
"""

WINDOWS_SCHEDULED_TASK_NAME = "LoopbackAliases"
WINDOWS_LOOPBACK_SCRIPT = Path(r"C:\persist_loopbacks.bat")

REQUIRED_COMMANDS = ["git", "helm", "kubectl"]


def echo_color(text: str, color: Optional[str] = "auto", err: bool = False) -> None:
    """
    Print text with an optional color using Typer.
    If color='auto', it chooses red if err=True, else green.
    If color=None, it prints without color.

    :param text: The text to print.
    :param color: "auto", None, or a Typer color constant (e.g., typer.colors.BLUE).
    :param err: Whether to print to stderr instead of stdout.
    """
    if color == "auto":
        color = typer.colors.RED if err else typer.colors.GREEN

    if color is None:
        typer.echo(text, err=err)
    else:
        typer.echo(typer.style(text, fg=color), err=err)


def run_command(
        cmd: list[str],
        raise_on_error: bool = True,
        cwd: Optional[Path] = None,
        input_data: Optional[str] = None,
        debug: bool = False,
):
    if debug:
        echo_color(f"[DEBUG] Running: {' '.join(cmd)}", color=typer.colors.MAGENTA)
    try:
        subprocess.run(
            cmd,
            check=raise_on_error,
            cwd=str(cwd) if cwd else None,
            input=input_data,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
        )
    except subprocess.SubprocessError:
        echo_color(f"[ERROR] Command failed: {' '.join(cmd)}", err=True)
        raise


@app.callback()
def main(ctx: typer.Context):
    if colorama is not None:
        colorama.init()
    ctx.ensure_object(dict)


def is_process_running(process_name: str) -> bool:
    """Check if a process is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", process_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_command_exists(cmd: str) -> bool:
    if shutil.which(cmd) is None:
        echo_color(f"[WARNING] Command '{cmd}' not on PATH.", color=typer.colors.YELLOW)
        return False
    else:
        return True


def clear_namespaces(namespace: str, debug: bool):
    echo_color("Clearing Kubernetes namespaces.")
    run_command(
        ["kubectl", "delete", "namespace", namespace], debug=debug, raise_on_error=False
    )
    run_command(
        ["kubectl", "delete", "namespace", "ambassador"],
        debug=debug,
        raise_on_error=False,
    )


def windows_loopback_script(ips: list[str]):
    lines = ["@echo off"]
    for ip in ips:
        lines.append(
            f'netsh interface ip add address "Loopback Pseudo-Interface 1" {ip} 255.255.255.0 1>nul 2>nul'
        )
    lines.append("exit /b 0")
    WINDOWS_LOOPBACK_SCRIPT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def windows_scheduled_task(debug: bool):
    run_command(
        ["schtasks", "/delete", "/f", "/tn", WINDOWS_SCHEDULED_TASK_NAME], debug=debug
    )
    run_command(
        [
            "schtasks",
            "/create",
            "/tn",
            WINDOWS_SCHEDULED_TASK_NAME,
            "/sc",
            "onstart",
            "/ru",
            "SYSTEM",
            "/rl",
            "HIGHEST",
            "/tr",
            str(WINDOWS_LOOPBACK_SCRIPT),
        ],
        debug=debug,
    )
    echo_color(f"Scheduled task '{WINDOWS_SCHEDULED_TASK_NAME}' created or updated.")


def install_telepresence_all_os(debug: bool):
    echo_color("Installing Telepresence binary.")

    if check_command_exists("telepresence"):
        echo_color("Telepresence is already installed. Skipping.")
        return

    sys_str = platform.system().lower()
    if sys_str == "darwin":
        if not check_command_exists("brew"):
            echo_color(
                "Homebrew not found, cannot install Telepresence automatically.",
                color=typer.colors.YELLOW,
            )
            return

        formula_url = "https://raw.githubusercontent.com/datawire/homebrew-blackbird/97e0a28d02adb42221ae4160c35a35f3a00f9eed/Formula/telepresence-arm64.rb"
        local_formula = "/tmp/telepresence-arm64.rb"

        # Download the formula using wget
        if not check_command_exists("wget"):
            echo_color("wget not found, cannot download the formula.", err=True)
            return

        run_command(["wget", "-O", local_formula, formula_url], debug=debug)

        # Install the formula using brew
        run_command(
            ["brew", "install", local_formula], debug=debug, raise_on_error=False
        )
    elif sys_str == "linux":
        tmp_path = Path(tempfile.gettempdir()) / "telepresence"
        run_command(
            [
                "curl",
                "-fL",
                "https://app.getambassador.io/download/tel2oss/releases/download/v2.14.4/telepresence-linux-amd64",
                "-o",
                str(tmp_path),
            ],
            debug=debug,
        )
        run_command(["chmod", "+x", str(tmp_path)], debug=debug)
        run_command(
            ["sudo", "-S", "mv", str(tmp_path), "/usr/local/bin/telepresence"],
            debug=debug,
        )
    elif sys_str == "windows":
        # If Telepresence is not installed, try installing via choco
        if not check_command_exists("choco"):
            run_command(
                [
                    "powershell.exe",
                    "Set-ExecutionPolicy",
                    "Bypass",
                    "-Scope",
                    "Process",
                    "-Force;",
                    "[System.Net.ServicePointManager]::SecurityProtocol="
                    "[System.Net.ServicePointManager]::SecurityProtocol -bor 3072;",
                    "iex",
                    "(New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1')",
                ],
                debug=debug,
            )
        if check_command_exists("choco"):
            run_command(
                ["choco", "install", "telepresence", "--version=2.14.4", "-y"],
                debug=debug,
            )
        else:
            echo_color(
                "Chocolatey not available; cannot install Telepresence.", err=True
            )


def setup_telepresence(intercept: bool, install_telepresence: bool, namespace: str, debug: bool):
    if install_telepresence:
        install_telepresence_all_os(debug)

    if intercept:
        echo_color("Installing Telepresence in Helm.")

        run_command(
            ["sudo", "-S", "pkill", "-f", "telepresence"], debug=debug, raise_on_error=False
        )
        run_command(
            ["sudo", "-S", "telepresence", "quit", "-s"], debug=debug, raise_on_error=False
        )
        run_command(["telepresence", "helm", "install"], debug=debug, raise_on_error=False)
        run_command(
            ["telepresence", "helm", "upgrade", "--set", "timeouts.agentArrival=300s"],
            debug=debug,
        )
        run_command(["telepresence", "connect"], debug=debug)
        run_command(
            [
                "kubectl",
                "wait",
                "--for=condition=available",
                "--timeout=300s",
                "deployment/mlrun-api-chief",
                "-n",
                namespace,
            ],
            debug=debug,
        )
        run_command(
            [
                "telepresence",
                "--namespace",
                namespace,
                "intercept",
                "mlrun-api-chief",
                "--service",
                "mlrun-api-chief",
                "--port",
                "8080:8080",
                "--env-file",
                "mlrun-ce-docker.env",
            ],
            debug=debug,
        )


def is_traefik_installed(debug: bool = False) -> bool:
    """
    Checks if Traefik is installed on the cluster by looking for Traefik pods in all namespaces.
    You can customize this logic to best suit your environment.
    """
    cmd = ["kubectl", "get", "pods", "-A"]
    if debug:
        typer.echo(f"[DEBUG] Checking for Traefik: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # If we failed to run kubectl, fallback to saying no
        if debug:
            typer.echo(
                "[DEBUG] kubectl get pods -A failed, assuming Traefik is not installed."
            )
        return False

    # Simple detection if 'traefik' is found in any pod name
    return "traefik" in res.stdout.lower()


def setup_ingress(debug: bool):
    """
    Installs ingress-nginx only if Traefik is not detected.
    Otherwise, we assume the user wants to rely on Traefik for ingress.
    """
    traefik_found = is_traefik_installed(debug=debug)
    if traefik_found:
        typer.echo(
            "Traefik is already installed on this cluster. Skipping ingress-nginx installation."
        )
        return

    typer.echo("Setting up NGINX Ingress Controller.")
    run_command(
        [
            "helm",
            "repo",
            "add",
            "ingress-nginx",
            "https://kubernetes.github.io/ingress-nginx",
        ],
        debug=debug,
    )
    run_command(["helm", "repo", "update"], debug=debug)
    res = subprocess.run(
        ["helm", "status", "ingress-nginx", "-n", "ingress-nginx"],
        capture_output=True,
        text=True,
    )
    if "not found" in (res.stdout + res.stderr).lower():
        run_command(
            [
                "helm",
                "install",
                "--namespace",
                "ingress-nginx",
                "--create-namespace",
                "--set",
                "controller.ingressClassResource.default=true",
                "ingress-nginx",
                "ingress-nginx/ingress-nginx",
            ],
            debug=debug,
        )


def add_helm_repositories(debug: bool):
    echo_color("Setting up Helm repositories.")
    for name, url in HELM_REPOS.items():
        run_command(["helm", "repo", "add", name, url], debug=False)
    run_command(["helm", "repo", "update"], debug=False)


def setup_registry_secret(
        docker_user: str, docker_pass: str, docker_server: str, namespace: str, debug: bool
):
    echo_color("Setting up Docker registry secret.")
    ns_cmd = subprocess.run(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        capture_output=True,
        text=True,
    )
    run_command(["kubectl", "apply", "-f", "-"], input_data=ns_cmd.stdout, debug=debug)

    res = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", "registry-credentials"],
        capture_output=True,
        text=True,
    )
    if "NotFound" in res.stderr:
        run_command(
            [
                "kubectl",
                "-n",
                namespace,
                "create",
                "secret",
                "docker-registry",
                "registry-credentials",
                "--docker-username",
                docker_user,
                "--docker-password",
                docker_pass,
                "--docker-server",
                docker_server,
                "--docker-email",
                f"{docker_user}@iguazio.com",
            ],
            debug=debug,
        )


SEMVER_RC_REGEX = re.compile(r"^\d+\.\d+\.\d+(?:-rc\d+)?$")


def clean_version(version_str):
    match = re.search(r"\d+\.\d+\.\d+(?:-rc\d+)?", version_str)
    return match.group(0) if match else version_str.strip()


def is_valid_version(version):
    return bool(SEMVER_RC_REGEX.match(version))


def get_all_tags(url):
    tags = []
    page = 1
    per_page = 100
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Authorization": f"token {token}"}
    while True:
        params = {"page": page, "per_page": per_page}
        try:
            response = requests.get(url, params=params, timeout=30, headers=headers)
            response.raise_for_status()
            page_tags = response.json()
            if not page_tags:
                break
            tags.extend(page_tags)
            page += 1
        except requests.RequestException as e:
            print(f"HTTP error occurred while fetching tags: {e}")
            break
    return tags


def get_latest_valid_version(tags_url):
    latest_version = None
    tags = get_all_tags(tags_url)
    for tag in tags:
        tag_name = tag.get("name", "")
        cleaned_version = clean_version(tag_name)
        if is_valid_version(cleaned_version):
            latest_version = cleaned_version
            print(f"Valid version found: {latest_version}")
            break
        else:
            print(f"Ignoring invalid version: {cleaned_version}")
    if not latest_version:
        raise ValueError("No valid version found with the required criteria.")
    return latest_version


def setup_ce(user: str, server: str, ce_version: str, namespace: str, ce_dir: Path, debug: bool):
    if not ce_version:
        ce_version = get_latest_valid_version(
            "https://api.github.com/repos/mlrun/ce/tags"
        )
        ce_version = ce_version.replace("mlrun-ce-", "")

    add_helm_repositories(debug=debug)

    res = subprocess.run(
        ["helm", "status", "mlrun-admin", "-n", namespace],
        capture_output=True,
        text=True,
    )
    if "not found" in (res.stdout + res.stderr).lower():
        run_command(
            [
                "helm",
                "--namespace",
                namespace,
                "upgrade",
                "--install",
                "mlrun-admin",
                "--create-namespace",
                "mlrun/mlrun-ce",
                "--devel",
                "--version",
                ce_version,
                "--values",
                f"{ce_dir}/charts/mlrun-ce/admin_installation_values.yaml",
            ],
            debug=False,
        )

    registry_url = f"{server.rstrip('/')}/{user}"
    install_args = [
        "--namespace",
        namespace,
        "--create-namespace",
        "--set",
        f"global.registry.url={registry_url}",
        "--set",
        "global.registry.secretName=registry-credentials",
        "--set",
        "global.externalHostAddress=mlrun.svc.cluster.local",
        "--set",
        "mlrun.api.securityContext.readOnlyRootFilesystem=false",
        "--set",
        "mlrun.api.chief.tolerations[0].key=node.kubernetes.io/disk-pressure",
        "--set",
        "mlrun.api.chief.tolerations[0].operator=Exists",
        "--set",
        "mlrun.api.chief.tolerations[0].effect=NoSchedule",
        "--set",
        "global.localEnvironment=true",
        "--set",
        "global.persistence.storageClass=hostpath",
        "--set",
        f"global.persistence.hostPath={Path.home() / 'mlrun-data'}",
        "mlrun/mlrun-ce",
        "--devel",
        "--version",
        ce_version,
        "--set",
        'mlrun.ui.ingress.enabled=true',
        "--values",
        f"{ce_dir}/charts/mlrun-ce/non_admin_cluster_ip_installation_values.yaml",
        "--set",
        "argoWorkflows.controller.metricsConfig.enabled=false",
    ]

    run_command(
        ["helm", "upgrade", "--install", "mlrun"] + install_args,
        debug=debug,
    )


def upgrade_images(
        mlrun_ver: str, nuclio_ver: str, ce_dir: Path, user: str, server: str, branch: str, arch: str, namespace: str,
        debug: bool
):
    if not ce_dir.is_dir():
        run_command(["git", "clone", REPO_URL, str(ce_dir)], debug=debug)
    else:
        if branch:
            run_command(["git", "checkout", branch], cwd=ce_dir, debug=debug)
            run_command(["git", "pull"], cwd=ce_dir, debug=debug)

    charts = ce_dir / "charts" / "mlrun-ce"
    if not charts.is_dir():
        echo_color(
            f"{charts} not found. Skipping local image upgrade.",
            color=typer.colors.YELLOW,
        )
        return

    if not mlrun_ver:
        mlrun_ver = get_latest_valid_version(
            "https://api.github.com/repos/mlrun/mlrun/tags"
        )
        mlrun_ver = mlrun_ver.replace("v", "")

    if not nuclio_ver:
        nuclio_ver = get_latest_valid_version(
            "https://api.github.com/repos/nuclio/nuclio/tags"
        )
        nuclio_ver = nuclio_ver.replace("v", "")

    registry_url = f"{server.rstrip('/')}/{user}"
    run_command(["helm", "dependency", "build"], cwd=charts, debug=debug)

    run_command(
        [
            "helm",
            "upgrade",
            "mlrun",
            ".",
            "--namespace",
            namespace,
            "--reuse-values",
            "--set",
            f"global.registry.url={registry_url}",
            "--set",
            f"mlrun.api.image.tag={mlrun_ver}",
            "--set",
            f"mlrun.ui.image.tag={mlrun_ver}",
            "--set",
            f"mlrun.api.sidecars.logCollector.image.tag={mlrun_ver}",
            "--set",
            f"jupyterNotebook.image.tag={mlrun_ver}",
            "--set",
            f"nuclio.controller.image.tag={nuclio_ver}-{arch}",
            "--set",
            f"nuclio.dashboard.image.tag={nuclio_ver}-{arch}",
            "--set",
            'mlrun.ui.ingress.enabled=true',
            "--set",
            "mlrun.api.ingress.hosts[0].host=mlrun-api"

        ],
        cwd=charts,
        debug=debug,
    )


def create_ingress(namespace: str, debug: bool):
    typer.echo("Ensuring Ingress resources are created...")

    traefik_found = is_traefik_installed(debug=debug)
    ingress_class = "traefik" if traefik_found else "nginx"

    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "mlrun-ce-ingress",
            "namespace": namespace,
            "annotations": {},
        },
        "spec": {"ingressClassName": ingress_class, "rules": []},
    }

    for item in INGRESS_HOSTS:
        host_item = {"host": item["host"], "http": {"paths": []}}
        for p in item["paths"]:
            host_item["http"]["paths"].append(
                {
                    "path": p["path"],
                    "pathType": "Prefix",
                    "backend": {
                        "service": {
                            "name": p["serviceName"],
                            "port": {"number": p["servicePort"]},
                        }
                    },
                }
            )
        ingress["spec"]["rules"].append(host_item)

    proc = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml.dump(ingress, sort_keys=False),
        text=True,
    )
    if proc.returncode == 0:
        typer.echo("Ingress ensured.")
    else:
        echo_color("Failed to create/update Ingress.", err=True)


def patch_mlrun_env():
    env_file = Path("mlrun-ce-docker.env")
    home_dir = str(Path.home())
    new_line = f"MLRUN_HTTPDB__DIRPATH={home_dir}/mlrun/db"
    if env_file.is_file():
        lines = env_file.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith("MLRUN_HTTPDB__DIRPATH="):
                lines[i] = new_line
                found = True
        if not found:
            lines.append(new_line)
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_file.write_text(new_line + "\n", encoding="utf-8")


def install_ce_on_docker(
        user: str,
        passwd: str,
        server: str,
        ce_dir: Path,
        clear_ns: bool,
        intercept: bool,
        install_tel: bool,
        ce_ver: str,
        mlrun_ver: str,
        nuclio_ver: str,
        branch: str,
        arch: str,
        namespace: str,
        debug: bool,
):
    for c in REQUIRED_COMMANDS:
        check_command_exists(c)
    if clear_ns:
        clear_namespaces(namespace, debug)
    (Path.home() / "mlrun-data").mkdir(exist_ok=True)

    setup_ingress(debug)
    setup_registry_secret(user, passwd, server, namespace, debug)
    setup_ce(user, server, ce_ver, namespace, ce_dir, debug)
    upgrade_images(mlrun_ver, nuclio_ver, ce_dir, user, server, branch, arch, namespace, debug)
    create_ingress(namespace, debug)
    setup_telepresence(
        intercept=intercept,
        install_telepresence=install_tel,
        namespace=namespace,
        debug=debug,
    )
    patch_mlrun_env()
    echo_color("MLRun CE installation complete!")


@app.command()
def install(
        ctx: typer.Context,
        docker_user: str = typer.Option(
            ...,
            help="Docker username for pulling/pushing images."
        ),
        docker_password: str = typer.Option(
            ...,
            help="Password or token for the specified Docker user."
        ),
        docker_server: str = typer.Option(
            ...,
            help="Docker registry server (e.g., 'docker.io' or a private registry)."
        ),
        ce_folder: Path = typer.Option(
            Path.home() / "mlrun-ce",
            "--ce-folder",
            help="Folder in which to clone and store the MLRun CE source."
        ),
        clear_k8s_namespaces: bool = typer.Option(
            False,
            "--clear-namespaces",
            help="Remove existing MLRun-related Kubernetes namespaces before install."
        ),
        intercept: bool = typer.Option(
            False,
            "--intercept",
            help="Intercept the MLRun API Chief deployment using Telepresence."
        ),
        install_tel: bool = typer.Option(
            False,
            "--install-telepresence",
            help="Install Telepresence if not found on the system."
        ),
        ce_version: str = typer.Option(
            "",
            "--ce-version",
            help="MLRun CE chart version to install. If empty, fetches the latest valid version."
        ),
        mlrun_version: str = typer.Option(
            "",
            "--mlrun-version",
            help="MLRun version (image tag) to use. If empty, fetches the latest valid version."
        ),
        nuclio_version: str = typer.Option(
            "",
            "--nuclio-version",
            help="Nuclio version (image tag) to use. If empty, fetches the latest valid version."
        ),
        branch: str = typer.Option(
            "",
            "--branch",
            help="Git branch name to check out when upgrading images from the CE repo."
        ),
        namespace: str = typer.Option(
            "mlrun",
            "--namespace",
            help="Kubernetes namespaceo."
        ),
        debug: bool = typer.Option(
            False,
            "--debug",
            help="Enable debug mode for more verbose log output."
        ),
        arch: str = typer.Option(
            platform.machine(),
            "--arch",
            help="Processor arch to use."
        ),
):
    install_ce_on_docker(
        docker_user,
        docker_password,
        docker_server,
        ce_folder,
        clear_k8s_namespaces,
        intercept,
        install_tel,
        ce_version,
        mlrun_version,
        nuclio_version,
        branch,
        arch,
        namespace,
        debug,
    )


@app.command()
def intercept_only(
        ctx: typer.Context,
        install_tel: bool = typer.Option(
            False,
            "--install-telepresence",
            help="Install Telepresence if not installed."
        ),
        namespace: str = typer.Option(
            "mlrun",
            "--namespace",
            help="Kubernetes namespaceo."
        ),
        debug: bool = typer.Option(
            False,
            "--debug",
            help="Enable debug mode for more verbose log output."
        ),
):
    """
    Only intercept the MLRun API Chief deployment (without re-installing everything).
    """
    setup_telepresence(intercept=True, install_telepresence=install_tel, namespace=namespace, debug=debug)


@app.command()
def unintercept(
        ctx: typer.Context,
        debug: bool = typer.Option(
            False,
            "--debug",
            help="Enable debug mode for more verbose log output."
        ),
):
    """
    Disconnect Telepresence and leave the MLRun API Chief intercept.
    """
    run_command(["telepresence", "leave", "mlrun-api-chief"], debug=debug)
    run_command(["telepresence", "disconnect"], debug=debug)
    echo_color("Telepresence intercept removed and disconnected.")


if __name__ == "__main__":
    app()
