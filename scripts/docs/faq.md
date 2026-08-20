# FAQ / Known gotchas

Things that look like bugs but aren't, plus a few setup gotchas worth knowing about
before you hit them.

---

### The registry secret doesn't exist / install fails with a `helm --wait` timeout on image pull

`--skip-secret` means "use an existing secret, don't create one" — `install.sh` verifies
that secret actually exists in the target namespace and exits 1 immediately with a clear
message if it doesn't, rather than letting the install proceed and fail later with an
opaque `helm --wait` timeout once pods can't pull images.

With `-f`/`--values`, there's no equivalent check today — the secret your values file
references must already exist, or the install will similarly time out once pods try to
pull images using a nonexistent `imagePullSecrets` entry.

### `--dry-run` fails on `PrometheusRule`/`ServiceMonitor` validation

`--dry-run` uses `helm --dry-run=server`, which validates against the live API server. If
the target cluster lacks the Prometheus Operator CRDs, the `kube-prometheus-stack`
subchart's `PrometheusRule`/`ServiceMonitor` resources fail server-side validation. This
is a Helm limitation (charts with CRDs can't fully dry-run without those CRDs present),
not an `install.sh` bug.

### Can I install two `mlrun-ce` releases on one cluster?

No, even in different namespaces with different release/secret names and NodePort
overrides via `-f`. The chart's `workflow-controller` `PriorityClass` is cluster-scoped
with a hardcoded name (no values.yaml knob), so a second release's `helm install` fails
immediately with an ownership-metadata error once one release already owns it. Not an
`install.sh` bug — the chart itself has no multi-release story on a shared cluster short
of patching that template.

### `--hard-clean` didn't delete everything — a Kafka pod and its PVC are still there

`helm uninstall` (and `--hard-clean`) can leave orphaned Strimzi `Kafka`/
`KafkaNodePool`/`StrimziPodSet` custom resources and their broker pod behind: once the
`strimzi-kafka-operator` Deployment is gone, nothing reconciles those CRs, so the broker
pod keeps running and its PVC's `kubernetes.io/pvc-protection` finalizer blocks
`--hard-clean`'s PVC deletion indefinitely.

Fix is manual: delete the `strimzipodset` and pod directly (releases the finalizer), then
the `kafka`/`kafkanodepool` CRs:

```bash
kubectl delete strimzipodset --all -n mlrun
kubectl delete pod -l strimzi.io/cluster -n mlrun --force --grace-period=0
kubectl delete kafka,kafkanodepool --all -n mlrun
```

Not something `do_hard_clean` can anticipate from `install.sh` alone — it's a
chart/Strimzi ordering issue.

### What address does the installer suggest for `EXTERNAL_HOST_ADDRESS`, and why?

`resolve_external_host()` picks a suggested default in this order (always just a
default — override with `EXTERNAL_HOST_ADDRESS`/`installer.externalHostAddress` any time
it's wrong for your cluster):

1. **`KUBE_CONTEXT` is set** → the target cluster's node internal IP (via `kubectl get
   node`). The minikube/docker-desktop heuristics below are statements about the *local
   machine's own* ambient environment and are meaningless once a specific — possibly
   remote — context is explicitly selected; `kubectl config current-context` also can't
   be made `--context`-aware (it always reports the kubeconfig's ambient current-context
   regardless of `--context`), so this case is handled separately. This is the right
   default for e.g. a lab cluster reachable directly on the corporate network via a
   named context — even if `kubectl`/`helm` reach the API server itself through an SSH
   tunnel (only the API server port is tunneled in that setup; NodePort services aren't,
   so `localhost` would resolve to nothing there).
2. **minikube is installed and has an IP** → that IP.
3. **Current context matches `docker-desktop`** → `host.docker.internal` (resolves to the
   host from both pods and your terminal on Docker Desktop).
4. **None of the above** (e.g. kind, k3d, or another local cluster type) → `localhost`.
   These tools typically NodePort-map to `localhost` rather than an internal
   Docker-network IP, so it's a better generic guess than a node-IP lookup that's often
   unreachable from the host.

### Docker Desktop: pushing to `--local-registry --enable-ingress` fails with a TLS error

The local registry serves plain HTTP through your ingress controller (port 80). Docker
and kaniko default to HTTPS for any non-`localhost` registry, which causes a TLS error on
port 443. Two things need configuring:

**1. Docker CLI on your machine**

1. Open **Docker Desktop → Settings → Docker Engine**
2. Add `registry.host.docker.internal` to `insecure-registries`:

```json
{
  "insecure-registries": ["registry.host.docker.internal"]
}
```

3. Click **Apply & Restart**

After that you can push images from your machine:

```bash
docker tag myimage registry.host.docker.internal/myimage
docker push registry.host.docker.internal/myimage
```

**2. Kaniko inside the cluster**

The installer automatically passes `mlrun.api.kaniko.insecureRegistry=true` to the Helm
chart when `--local-registry --enable-ingress` are both set, so MLRun build jobs use HTTP
when pushing to the local registry. No manual action needed.

For reference, once installed, the registry is reachable at
`registry.host.docker.internal` from everywhere:

```bash
# From your terminal
curl http://registry.host.docker.internal/v2/_catalog

# From inside a pod
kubectl run test --rm -it --image=curlimages/curl --restart=Never \
  -n mlrun -- curl http://registry.host.docker.internal/v2/_catalog
```

### `--enable-ingress` is set but the Ingress URLs don't resolve

This installer never installs an ingress controller for you — `--enable-ingress` only
flips the chart's own Ingress resources on via `--set`. If no IngressClass matching your
`--enable-ingress`'s class (default `nginx`) exists in the cluster, the pre-install
validators warn about this before the install even starts, but the install proceeds
anyway (the Ingress resources get created either way). Install a controller providing
that class — e.g. [ingress-nginx](https://kubernetes.github.io/ingress-nginx/) — and the
existing Ingress resources will start resolving with no re-install needed.

### Deleting everything, including the namespace

`--uninstall --hard-clean` runs `helm uninstall` and then deletes every PVC in the
namespace and every PV bound to it. **This is irreversible and will cause data loss.**
The namespace and CRDs themselves are not deleted by `install.sh`; remove them yourself
if needed:

```bash
kubectl delete namespace mlrun
```
