#!/usr/bin/env python3
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
"""Recording stub standing in for helm / kubectl / docker on PATH.

Every invocation is appended to $STUB_LOG as one JSON array per line, which is what
test_golden_argv.py compares against the recorded expectations in golden/. Answers are
derived from the arguments rather than fixed, so a test cannot pass by accident when the
installer stops asking the question it was supposed to ask.

Installed under several names; argv[0]'s basename selects the behaviour.
"""

import json
import os
import pathlib
import sys


def log(argv):
    path = os.environ.get("STUB_LOG")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write(json.dumps(argv) + "\n")
        handle.flush()


def jsonpath_of(args):
    """The jsonpath expression from the args, with kubectl's dot-escaping undone.

    Annotation keys have to reach kubectl as `storageclass\\.kubernetes\\.io/...` so the
    dots are not read as field separators. Matching on the escaped form here would make
    every `in path` check below silently false.
    """
    for arg in args:
        if arg.startswith("jsonpath="):
            return arg[len("jsonpath=") :].replace("\\.", ".")
    return ""


def handle_kubectl(args):
    # Strip the context wrapper so the matching below sees the real verb.
    if len(args) >= 2 and args[0] == "--context":
        args = args[2:]

    if not args:
        return 0, ""

    verb = args[0]

    if verb == "cluster-info":
        return 0, "Kubernetes control plane is running at https://127.0.0.1:6443"

    if verb == "config" and len(args) > 1 and args[1] == "current-context":
        return 0, os.environ.get("STUB_CURRENT_CONTEXT", "stub-context")

    if verb == "get":
        what = args[1] if len(args) > 1 else ""
        path = jsonpath_of(args)

        if what == "namespace":
            return (0, "") if os.environ.get("STUB_NAMESPACE_EXISTS") == "1" else (1, "not found")
        if what == "secret":
            return (0, "") if os.environ.get("STUB_SECRET_EXISTS") == "1" else (1, "not found")
        if what == "ingressclass":
            return (
                (0, "") if os.environ.get("STUB_INGRESSCLASS_EXISTS") == "1" else (1, "not found")
            )
        if what == "storageclass":
            # Answer according to the jsonpath handed over, so dropping the beta key from
            # the query changes the answer instead of silently still passing.
            fields = ["standard"]
            if "storageclass.kubernetes.io/is-default-class" in path:
                fields.append(os.environ.get("STUB_SC_STABLE", ""))
            if "storageclass.beta.kubernetes.io/is-default-class" in path:
                fields.append(os.environ.get("STUB_SC_BETA", ""))
            return 0, "=".join(fields)
        if what in ("nodes", "node"):
            if "kubeletVersion" in path:
                return 0, os.environ.get("STUB_K8S_VERSION", "v1.30.2")
            if "allocatable.memory" in path:
                return 0, os.environ.get("STUB_NODE_MEMORY", "16384000Ki")
            if "allocatable.ephemeral-storage" in path:
                return 0, os.environ.get("STUB_NODE_STORAGE", "56403987978")
            if "InternalIP" in path:
                return 0, os.environ.get("STUB_NODE_IP", "10.0.0.5")
            return 0, ""
        if what == "svc":
            if "ingress-nginx-controller" in args:
                return 0, os.environ.get("STUB_INGRESS_CLUSTERIP", "10.96.0.10")
            return 0, os.environ.get("STUB_NODEPORTS", "")
        if what == "configmap":
            return 0, os.environ.get("STUB_COREFILE", ".:53 {\n    forward . /etc/resolv.conf\n}")
        if what == "pvc":
            return 0, os.environ.get("STUB_PVCS", "")
        if what == "pv":
            return 0, os.environ.get("STUB_PVS", "")
        if what in ("deployments", "statefulsets"):
            return 0, "NAME   READY   AGE\nmlrun-api   1/1   2m"
        return 0, ""

    if verb in ("create", "delete", "apply", "rollout", "patch"):
        return 0, ""

    return 0, ""


def handle_helm(args):
    if len(args) >= 2 and args[0] == "--kube-context":
        args = args[2:]

    if not args:
        return 0, ""

    verb = args[0]

    if verb == "version":
        return 0, os.environ.get("STUB_HELM_VERSION", "v3.14.2+g2a2fb3b")
    if verb == "status":
        return (
            (0, "STATUS: deployed")
            if os.environ.get("STUB_RELEASE_EXISTS") == "1"
            else (1, "not found")
        )
    if verb == "get" and len(args) > 1 and args[1] == "notes":
        return 0, os.environ.get("STUB_NOTES", "")
    if verb in ("repo", "dependency", "upgrade", "uninstall"):
        return int(os.environ.get("STUB_HELM_EXIT", "0")), ""
    return 0, ""


def handle_docker(args):
    if args and args[0] == "info":
        return int(os.environ.get("STUB_DOCKER_INFO_EXIT", "0")), ""
    if args and args[0] == "login":
        # Drain stdin so --password-stdin behaves like the real client.
        try:
            sys.stdin.read()
        except Exception:
            pass
        return int(os.environ.get("STUB_DOCKER_LOGIN_EXIT", "0")), ""
    return 0, ""


def handle_minikube(args):
    if args and args[0] == "ip":
        return 0, os.environ.get("STUB_MINIKUBE_IP", "192.168.49.2")
    return 0, ""


HANDLERS = {
    "kubectl": handle_kubectl,
    "helm": handle_helm,
    "docker": handle_docker,
    "minikube": handle_minikube,
}


def main():
    name = pathlib.Path(sys.argv[0]).name
    args = sys.argv[1:]
    log([name, *args])

    handler = HANDLERS.get(name)
    if handler is None:
        return 0

    code, out = handler(args)
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
