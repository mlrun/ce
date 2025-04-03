#!/usr/bin/env python3
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
import typer
import yaml
from kubernetes import config, client

try:
    import colorama
except ImportError:
    colorama = None

app = typer.Typer(help="Manage MLRun CE installation.")

REPO_URL = "git@github.com:mlrun/ce.git"

HELM_REPOS = {
    "mlrun": "https://mlrun.github.io/ce",
    "nuclio": "https://nuclio.github.io/nuclio/charts",
    "v3io-stable": "https://v3io.github.io/helm-charts/stable",
    "minio": "https://charts.min.io/",
    "spark-operator": "https://kubeflow.github.io/spark-operator",
    "prometheus-community": "https://prometheus-community.github.io/helm-charts",
}

REQUIRED_COMMANDS = ["git", "helm", "kubectl"]

def echo_color(text: str, color: Optional[str] = "auto", err: bool = False) -> None:
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

def is_traefik_installed(debug: bool = False) -> bool:
    cmd = ["kubectl", "get", "pods", "-A"]
    if debug:
        typer.echo(f"[DEBUG] Checking for Traefik: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if debug:
            typer.echo("[DEBUG] kubectl get pods -A failed, assuming Traefik is not installed.")
        return False
    return "traefik" in res.stdout.lower()

def clear_namespaces(namespace: str, debug: bool):
    echo_color("Clearing Kubernetes namespaces.")
    run_command(["kubectl", "delete", "namespace", namespace], debug=debug, raise_on_error=False)
    run_command(["kubectl", "delete", "namespace", "ambassador"], debug=debug, raise_on_error=False)

def ensure_namespace(namespace: str, debug: bool = False):
    try:
        run_command(["kubectl", "get", "namespace", namespace], debug=debug)
    except subprocess.CalledProcessError:
        echo_color(f"Namespace '{namespace}' not found. Creating it...",)
        run_command(["kubectl", "create", "namespace", namespace], debug=debug)

def setup_ingress(debug: bool):
    if is_traefik_installed(debug=debug):
        typer.echo("Traefik is already installed on this cluster. Skipping ingress-nginx installation.")
        return
    typer.echo("Setting up NGINX Ingress Controller.")
    run_command(["helm", "repo", "add", "ingress-nginx", "https://kubernetes.github.io/ingress-nginx"], debug=debug)
    run_command(["helm", "repo", "update"], debug=debug)
    res = subprocess.run(["helm", "status", "ingress-nginx", "-n", "ingress-nginx"],
                         capture_output=True, text=True)
    if "not found" in (res.stdout + res.stderr).lower():
        run_command([
            "helm", "install", "--namespace", "ingress-nginx",
            "--create-namespace", "--set", "controller.ingressClassResource.default=true",
            "ingress-nginx", "ingress-nginx/ingress-nginx"
        ], debug=debug)

def create_ingress(namespace: str, debug: bool):
    ensure_namespace(namespace, debug)
    typer.echo("Ensuring Ingress resources are created...")
    ingress_class = "traefik" if is_traefik_installed(debug=debug) else "nginx"
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
    ingress_hosts = [
        {
            "host": f"mlrun.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "mlrun-ui", "servicePort": 80}],
        },
        {
            "host": f"mlrun-api.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "mlrun-api", "servicePort": 8080}],
        },
        {
            "host": f"mlrun-api-chief.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "mlrun-api-chief", "servicePort": 8080}],
        },
        {
            "host": f"nuclio.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "nuclio-dashboard", "servicePort": 8070}],
        },
        {
            "host": f"nuclio-dashboard.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "nuclio-dashboard", "servicePort": 8070}],
        },
        {
            "host": f"jupyter.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "mlrun-jupyter", "servicePort": 8888}],
        },
        {
            "host": f"minio.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "minio-console", "servicePort": 9001}],
        },
        {
            "host": f"grafana.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "grafana", "servicePort": 80}],
        },
        {
            "host": f"kfp.{namespace}.svc.cluster.local",
            "paths": [
                {"path": "/", "serviceName": "ml-pipeline-ui", "servicePort": 80},
                {"path": "/apis/", "serviceName": "ml-pipeline", "servicePort": 8888},
            ],
        },
        {
            "host": f"metadata-envoy.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "metadata-envoy-service", "servicePort": 9090}],
        },
        {
            "host": f"workflow-metrics.{namespace}.svc.cluster.local",
            "paths": [{"path": "/", "serviceName": "workflow-controller-metrics", "servicePort": 9091}],
        },
    ]
    for item in ingress_hosts:
        host_item = {"host": item["host"], "http": {"paths": []}}
        for p in item["paths"]:
            host_item["http"]["paths"].append({
                "path": p["path"],
                "pathType": "Prefix",
                "backend": {
                    "service": {
                        "name": p["serviceName"],
                        "port": {"number": p["servicePort"]},
                    }
                },
            })
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

def add_helm_repositories(debug: bool):
    echo_color("Setting up Helm repositories.")
    for name, url in HELM_REPOS.items():
        run_command(["helm", "repo", "add", name, url], debug=False)
    run_command(["helm", "repo", "update"], debug=False)

def setup_registry_secret(
    docker_user: str, docker_pass: str, docker_registry: str, namespace: str, debug: bool
):
    echo_color("Setting up Docker registry secret.")
    ns_cmd = subprocess.run(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True
    )
    run_command(["kubectl", "apply", "-f", "-"], input_data=ns_cmd.stdout, debug=debug)
    res = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", "registry-credentials"],
        capture_output=True, text=True
    )
    if "NotFound" in res.stderr:
        run_command([
            "kubectl", "-n", namespace, "create", "secret", "docker-registry",
            "registry-credentials", "--docker-username", docker_user,
            "--docker-password", docker_pass, "--docker-server", docker_registry,
            "--docker-email", f"{docker_user}@iguazio.com",
        ], debug=debug)

SEMVER_RC_REGEX = re.compile(r"^\d+\.\d+\.\d+(?:-rc\d+)?$")

def clean_version(version_str):
    match = re.search(r"\d+\.\d+\.\d+(?:-rc\d+)?", version_str)
    return match.group(0) if match else version_str.strip()

def is_valid_version(version):
    return bool(SEMVER_RC_REGEX.match(version))

def get_all_tags(repository: str):
    tags = []
    page = 1
    per_page = 100
    token = os.environ.get("GITHUB_TOKEN", None)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"token {token}"
    while True:
        params = {"page": page, "per_page": per_page}
        try:
            response = requests.get(
                f"https://api.github.com/repos/{repository}/tags",
                params=params, timeout=30, headers=headers
            )
            response.raise_for_status()
            page_tags = response.json()
            if not page_tags:
                break
            tags.extend(page_tags)
            page += 1
        except requests.RequestException as e:
            echo_color(f"HTTP error occurred while fetching tags for repo {repository}: {e}", err=True)
            break
    return tags

def get_latest_valid_version(repo_name: str):
    latest_version = None
    tags = get_all_tags(repo_name)
    for tag in tags:
        tag_name = tag.get("name", "")
        cleaned_version = clean_version(tag_name)
        if is_valid_version(cleaned_version):
            latest_version = cleaned_version
            echo_color(f"Valid version found for repo {repo_name}: {latest_version}")
            break
        else:
            echo_color(f"Ignoring invalid version: {cleaned_version}")
    if not latest_version:
        raise ValueError(f"No valid version for repo {repo_name} found with the required criteria.")
    return latest_version

def setup_ce(user: str, server: str, ce_version: str, namespace: str, ce_dir: Path, branch: str, debug: bool):
    if not ce_version:
        ce_version = get_latest_valid_version("mlrun/ce")
        ce_version = ce_version.replace("mlrun-ce-", "")
    add_helm_repositories(debug=debug)
    if not ce_dir.is_dir():
        run_command(["git", "clone", REPO_URL, str(ce_dir)], debug=debug)
    else:
        if branch:
            run_command(["git", "checkout", branch], cwd=ce_dir, debug=debug)
            run_command(["git", "pull"], cwd=ce_dir, debug=debug)
    res = subprocess.run(["helm", "status", "mlrun-admin", "-n", namespace],
                         capture_output=True, text=True)
    if "not found" in (res.stdout + res.stderr).lower():
        run_command([
            "helm", "--namespace", namespace, "upgrade", "--install", "mlrun-admin",
            "--create-namespace", "mlrun/mlrun-ce", "--devel", "--version", ce_version,
            "--values", f"{ce_dir}/charts/mlrun-ce/admin_installation_values.yaml",
        ], debug=False)
    registry_url = f"{server.rstrip('/')}/{user}"
    install_args = [
        "--namespace", namespace, "--create-namespace",
        "--set", f"global.registry.url={registry_url}",
        "--set", "global.registry.secretName=registry-credentials",
        "--set", "global.externalHostAddress=mlrun.svc.cluster.local",
        "--set", "mlrun.api.securityContext.readOnlyRootFilesystem=false",
        "--set", "mlrun.api.chief.tolerations[0].key=node.kubernetes.io/disk-pressure",
        "--set", "mlrun.api.chief.tolerations[0].operator=Exists",
        "--set", "mlrun.api.chief.tolerations[0].effect=NoSchedule",
        "--set", "global.localEnvironment=true",
        "--set", "global.persistence.storageClass=hostpath",
        "--set", f"global.persistence.hostPath={Path.home() / 'mlrun-data'}",
        "mlrun/mlrun-ce", "--devel", "--version", ce_version,
        "--values", f"{ce_dir}/charts/mlrun-ce/non_admin_cluster_ip_installation_values.yaml",
        "--set", "argoWorkflows.controller.metricsConfig.enabled=false",
    ]
    run_command(["helm", "upgrade", "--install", "mlrun"] + install_args, debug=debug)

def upgrade_images(
    mlrun_ver: str, nuclio_ver: str, ce_dir: Path, user: str,
    docker_registry: str, arch: str, namespace: str, debug: bool,
):
    charts = ce_dir / "charts" / "mlrun-ce"
    if not charts.is_dir():
        echo_color(f"{charts} not found. Skipping local image upgrade.", color=typer.colors.YELLOW)
        return
    if not mlrun_ver:
        mlrun_ver = get_latest_valid_version("mlrun/mlrun")
        mlrun_ver = mlrun_ver.replace("v", "")
    if not nuclio_ver:
        nuclio_ver = get_latest_valid_version("nuclio/nuclio")
        nuclio_ver = nuclio_ver.replace("v", "")
    registry_url = f"{docker_registry.rstrip('/')}/{user}"
    run_command(["helm", "dependency", "build"], cwd=charts, debug=debug)
    run_command([
        "helm", "upgrade", "mlrun", ".", "--namespace", namespace, "--reuse-values",
        "--set", f"global.registry.url={registry_url}",
        "--set", f"mlrun.api.image.tag={mlrun_ver}",
        "--set", f"mlrun.ui.image.tag={mlrun_ver}",
        "--set", f"mlrun.api.sidecars.logCollector.image.tag={mlrun_ver}",
        "--set", f"jupyterNotebook.image.tag={mlrun_ver}",
        "--set", f"nuclio.controller.image.tag={nuclio_ver}-{arch}",
        "--set", f"nuclio.dashboard.image.tag={nuclio_ver}-{arch}",
    ], cwd=charts, debug=debug)

def get_k8s_dns_ip():
    try:
        config.load_kube_config()
        v1 = client.CoreV1Api()
        try:
            svc = v1.read_namespaced_service("kube-dns", "kube-system")
        except client.exceptions.ApiException as e:
            if e.status == 404:
                svc = v1.read_namespaced_service("coredns", "kube-system")
            else:
                raise
        return svc.spec.cluster_ip
    except Exception as e:
        echo_color(f"Error retrieving Kubernetes DNS IP: {e}", err=True)
        raise e

def set_dns(dns_ip: str):
    resolv_conf = "/etc/resolv.conf"
    backup_file = "/etc/resolv.conf.bak"
    try:
        with open(resolv_conf, "r") as f:
            lines = f.readlines()
    except Exception as e:
        echo_color(f"Error reading {resolv_conf}: {e}", err=True)
        return
    new_lines = lines.copy()
    nameserver_exists = any(line.strip().startswith("nameserver") and dns_ip in line for line in lines)
    search_exists = any(line.strip().startswith("search") and "cluster.local" in line for line in lines)
    if not nameserver_exists:
        new_lines.insert(0, f"nameserver {dns_ip}\n")
    if not search_exists:
        new_lines.insert(0, "search cluster.local\n")
    if new_lines != lines:
        try:
            if not os.path.exists(backup_file):
                subprocess.run(["sudo", "cp", resolv_conf, backup_file], check=True)
            with tempfile.NamedTemporaryFile("w", delete=False) as tf:
                tf.writelines(new_lines)
                temp_file_name = tf.name
            subprocess.run(["sudo", "cp", temp_file_name, resolv_conf], check=True)
            os.remove(temp_file_name)
            echo_color("Modified /etc/resolv.conf with Kubernetes DNS settings.")
        except Exception as e:
            echo_color(f"Error modifying {resolv_conf}: {e}", err=True)
    else:
        echo_color("/etc/resolv.conf already configured with Kubernetes DNS.")

# New function: update /etc/hosts with desired IP (overwriting existing MLRun entries)
def add_dns_entries_to_hosts(namespace: str, target_ip: str, debug: bool = False):
    hostnames = [
        f"mlrun.{namespace}.svc.cluster.local",
        f"mlrun-api.{namespace}.svc.cluster.local",
        f"mlrun-api-chief.{namespace}.svc.cluster.local",
        f"nuclio.{namespace}.svc.cluster.local",
        f"nuclio-dashboard.{namespace}.svc.cluster.local",
        f"jupyter.{namespace}.svc.cluster.local",
        f"minio.{namespace}.svc.cluster.local",
        f"grafana.{namespace}.svc.cluster.local",
        f"kfp.{namespace}.svc.cluster.local",
        f"metadata-envoy.{namespace}.svc.cluster.local",
        f"workflow-metrics.{namespace}.svc.cluster.local",
    ]
    try:
        with open("/etc/hosts", "r") as f:
            lines = f.readlines()
    except Exception as e:
        echo_color(f"Error reading /etc/hosts: {e}", err=True)
        return
    new_lines = []
    for line in lines:
        if any(host in line for host in hostnames):
            continue
        new_lines.append(line)
    new_entries = ""
    for hostname in hostnames:
        new_entries += f"{target_ip} {hostname}\n"
        if debug:
            echo_color(f"[DEBUG] Setting {hostname} to {target_ip}", color=typer.colors.MAGENTA)
    updated_content = "".join(new_lines) + new_entries
    try:
        subprocess.run(["sudo", "cp", "/etc/hosts", "/etc/hosts.bak"], check=True)
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(updated_content)
            temp_file_name = tf.name
        subprocess.run(["sudo", "cp", temp_file_name, "/etc/hosts"], check=True)
        subprocess.run(["sudo", "rm", temp_file_name], check=True)
        echo_color("Updated /etc/hosts with cluster DNS entries.")
    except subprocess.CalledProcessError as e:
        echo_color(f"Failed to update /etc/hosts: {e}", err=True)

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

# New function: get the external node IP from kubectl get nodes.
def get_node_external_ip(debug: bool = False) -> Optional[str]:
    try:
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, check=True
        )
        nodes = json.loads(result.stdout)
        for node in nodes.get("items", []):
            addresses = node.get("status", {}).get("addresses", [])
            for addr in addresses:
                if addr.get("type") == "ExternalIP":
                    external_ip = addr.get("address")
                    if debug:
                        echo_color(f"[DEBUG] Found external node IP: {external_ip}", color=typer.colors.MAGENTA)
                    return external_ip
        return None
    except Exception as e:
        echo_color(f"Error retrieving external node IP: {e}", err=True)
        return None

# Main installation entry point.
def install_ce_on_docker(
    user: str,
    passwd: str,
    docker_registry: str,
    ce_dir: Path,
    clear_ns: bool,
    ce_ver: str,
    mlrun_ver: str,
    nuclio_ver: str,
    branch: str,
    arch: str,
    namespace: str,
    debug: bool,
    update_hosts: bool,           # Flag: update /etc/hosts using external node IP
    ingress_local_port: int = 80  # Local port to use for port forwarding
):
    for c in REQUIRED_COMMANDS:
        check_command_exists(c)
    if clear_ns:
        clear_namespaces(namespace, debug)
    (Path.home() / "mlrun-data").mkdir(exist_ok=True)
    setup_ingress(debug)
    create_ingress(namespace, debug)
    setup_registry_secret(user, passwd, docker_registry, namespace, debug)
    setup_ce(user, docker_registry, ce_ver, namespace, ce_dir, branch, debug)
    upgrade_images(mlrun_ver, nuclio_ver, ce_dir, user, docker_registry, arch, namespace, debug)
    patch_mlrun_env()
    # Decide how to update DNS resolution:
    if update_hosts:
        external_ip = get_node_external_ip(debug)
        if external_ip:
            echo_color(f"Using external node IP: {external_ip}")
        else:
            echo_color("External node IP not found, falling back to 127.0.0.1", color=typer.colors.YELLOW)
            external_ip = "127.0.0.1"
        add_dns_entries_to_hosts(namespace, target_ip=external_ip, debug=debug)
    else:
        # Fallback: update /etc/resolv.conf with cluster DNS IP
        dns_ip = get_k8s_dns_ip()
        if dns_ip:
            set_dns(dns_ip)
    echo_color("MLRun CE installation complete!")

@app.command()
def install(
    ctx: typer.Context,
    docker_user: str = typer.Option(..., help="Docker username for pulling/pushing images."),
    docker_password: str = typer.Option(..., help="Password or token for the specified Docker user."),
    docker_registry: str = typer.Option(..., help="Docker registry (e.g., 'docker.io' or a private registry)."),
    ce_folder: Path = typer.Option(Path.home() / "mlrun-ce", "--ce-folder", help="Folder in which to clone and store the MLRun CE source."),
    clear_k8s_namespaces: bool = typer.Option(False, "--clear-namespaces", help="Remove existing MLRun-related Kubernetes namespaces before install."),
    ce_version: str = typer.Option("", "--ce-version", help="MLRun CE chart version to install. If empty, fetches the latest valid version."),
    mlrun_version: str = typer.Option("", "--mlrun-version", help="MLRun version (image tag) to use. If empty, fetches the latest valid version."),
    nuclio_version: str = typer.Option("", "--nuclio-version", help="Nuclio version (image tag) to use. If empty, fetches the latest valid version."),
    branch: str = typer.Option("", "--branch", help="Git branch name to check out when upgrading images from the CE repo."),
    namespace: str = typer.Option("mlrun", "--namespace", help="Kubernetes namespace."),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode for more verbose log output."),
    arch: str = typer.Option(platform.machine(), "--arch", help="Processor arch to use."),
    update_hosts: bool = typer.Option(
        False,
        "--update-hosts",
        help="Update /etc/hosts to map cluster DNS names to the external node IP."
    ),
    port_forward_ingress: bool = typer.Option(
        False,
        "--port-forward-ingress",
        help="Start port forwarding for ingress (maps local port to ingress controller's port 80 and updates /etc/hosts to 127.0.0.1)."
    ),
):
    install_ce_on_docker(
        docker_user,
        docker_password,
        docker_registry,
        ce_folder,
        clear_k8s_namespaces,
        ce_version,
        mlrun_version,
        nuclio_version,
        branch,
        arch,
        namespace,
        debug,
        update_hosts,
        port_forward_ingress,
    )

if __name__ == "__main__":
    app()
