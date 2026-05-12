# AGENTS.md

## Overview

This is a Helm umbrella chart repository for **MLRun Community Edition (CE)** — an open-source MLOps stack. The main chart lives at `charts/mlrun-ce/` and bundles: Nuclio, MLRun, Jupyter, MPI Operator, SeaweedFS (S3-compatible storage), Spark Operator, Kubeflow Pipelines, Prometheus stack, TimescaleDB, and Strimzi Kafka Operator.

## Commands to lint, package, and manage the chart:

```bash
# Lint the helm chart (requires helm and chart-testing `ct` installed)
make helm-lint

# Update sub-chart dependencies (run before lint or packaging)
make helm-update-dependencies

# Add all required helm repos from requirements.yaml
make helm-repo-add

# Package the chart as a tarball
make package
```

## Architecture

### Chart Structure

`charts/mlrun-ce/` is an **umbrella chart** that:
1. Declares sub-chart dependencies in `requirements.yaml` (pulled from external Helm repos)
2. Provides additional Kubernetes resources via `templates/` that the sub-charts don't include
3. Wires all components together through shared `values.yaml`

### Template Organization (`charts/mlrun-ce/templates/`)

- `config/` — ConfigMaps and Secrets shared across components: MLRun env config, Jupyter env config, storage credentials secret, Pipelines config, Spark config, Grafana dashboards
- `seaweedfs/` — SeaweedFS-specific resources: S3 IAM config secret, bucket init job, admin UI NodePort service, ingress
- `kafka/` — Kafka Strimzi custom resources: KafkaNodePool, Kafka cluster CR, bootstrap alias Service, RBAC, NetworkPolicy
- `timescaledb/` — TimescaleDB Deployment, Service, PVC
- `jupyter-notebook/` — Jupyter Deployment and supporting resources
- `pipelines/` — Kubeflow Pipelines resources
- `spark-operator/` — Spark controller RBAC
- `persistency/` — PVC definitions
- `aws/` — AWS-specific resources

### Key Design Patterns

**S3 credentials propagation**: The top-level `storage.s3.accessKey`/`storage.s3.secretKey`/`storage.s3.bucket` values flow into a `storage-credentials` Secret (created by `templates/config/storage-secret.yaml`), which is then mounted via `envFrom` in MLRun API and Jupyter pods. SeaweedFS uses the same credentials via the `seaweedfs-s3-config` Secret.

**Global registry anchor**: `global.registry: &userRegistry` in `values.yaml` uses YAML anchors to multiplex the same docker registry config to both `nuclio.global.registry` and `mlrun.global.registry`.

**Component enable/disable**: Most components can be disabled via `<component>.enabled: false`. The Kafka setup requires the Strimzi operator (deployed as a sub-chart via `strimzi-kafka-operator`) and custom Strimzi CRs in `templates/kafka/`.

### Values Files

- `charts/mlrun-ce/values.yaml` - base values for all modes, and default installation.
- `charts/mlrun-ce/admin_installation_values.yaml` - use to install cluster resources such as CRDs, RBAC, and operators deployment.
- `charts/mlrun-ce/non_admin_installation_values.yaml` - use to install non-cluster resources such as Deployments, Services, and Ingresses with NodePort.
- `charts/mlrun-ce/non_admin_cluster_ip_installation_values.yaml` - use to install non-cluster resources such as Deployments, Services, and Ingresses with ClusterIP.

## Quick-Start Dev Workflow

From a fresh clone to a linted chart:

1. `make helm-repo-add` — adds all external repos (reads `requirements.yaml`; idempotent)
2. `make helm-update-dependencies` — downloads sub-chart tarballs into `charts/mlrun-ce/charts/` (must run before any lint or template render)
3. `make helm-lint` — runs `helm lint charts/mlrun-ce` + `ct lint`
   - `ct` only lints charts with changes relative to the target branch; always run from a feature branch, not directly on `development`
4. Render all templates locally (no cluster needed):
   ```bash
   helm template mlrun charts/mlrun-ce -f charts/mlrun-ce/values.yaml
   ```
5. Render a single template file:
   ```bash
   helm template mlrun charts/mlrun-ce -f charts/mlrun-ce/values.yaml --show-only templates/kafka/kafka-cluster.yaml
   ```
6. Schema-validate without a cluster:
   ```bash
   helm template mlrun charts/mlrun-ce -f charts/mlrun-ce/values.yaml | kubectl apply --dry-run=client -f -
   ```

## Component Dependency Map

| Component | Enabled by | Runtime dependencies | Key templates / notes |
|---|---|---|---|
| MLRun API + UI + DB | `mlrun.enabled` | `storage-credentials` Secret, `mlrun-common-env` ConfigMap; mlrun-db (MySQL) is bundled inside the mlrun sub-chart | `templates/config/mlrun-env-configmap.yaml`; rest is in the `mlrun` sub-chart |
| Jupyter | `jupyterNotebook.enabled` | `storage-credentials` Secret, `jupyter-common-env` ConfigMap | `templates/jupyter-notebook/` |
| Nuclio | always on (no `enabled` guard in umbrella) | `global.registry` must be set | sub-chart only — no custom templates |
| MPI Operator | always on (no `enabled` guard in umbrella) | none | sub-chart only — no custom templates |
| SeaweedFS | `seaweedfs.enabled` | PVC for data storage; creates `seaweedfs-s3-config` Secret consumed by Pipelines and MLRun | `templates/seaweedfs/`; `seaweedfs.s3.enableAuth: true` must be set or the Secret is skipped |
| Spark Operator | `spark-operator.enabled` | none | sub-chart + `templates/spark-operator/spark-controller-rbac.yaml` |
| Kafka | `kafka.enabled` | Strimzi CRDs — `strimzi-kafka-operator` sub-chart must also be enabled as a prerequisite; CRs use post-install hooks to wait for CRDs | `templates/kafka/` |
| Pipelines | `pipelines.enabled` | SeaweedFS (`seaweedfs.enabled` checked at render time; adds init container to wait for it), `mlrun-pipelines-config` ConfigMap | `templates/pipelines/`, `templates/config/mlrun-pipelines-config.yaml` |
| TimescaleDB | `timescaledb.enabled` | none; uses its own `<release>-timescaledb-secret` for the DB password | `templates/timescaledb/` — custom StatefulSet, not a sub-chart |
| Prometheus + Grafana | `kube-prometheus-stack.enabled` | none at runtime; model monitoring dashboards are pre-loaded as static JSON ConfigMaps | sub-chart config in `values.yaml`; dashboards in `templates/config/model-monitoring-*.yml` |

## How to Add a New Component

1. Add a top-level block to `values.yaml` and update the three install-mode values files:
   ```yaml
   myComponent:
     enabled: true
   ```
2. Create `charts/mlrun-ce/templates/myComponent/`. Every template file must open with `{{- if .Values.myComponent.enabled }}` and close with `{{- end }}`. Add label helpers to `_helpers.tpl` following the `mlrun-ce.<component>.labels` / `mlrun-ce.<component>.selectorLabels` pattern.
3. NodePort selection — avoid all currently occupied ports:
   - 30010 Grafana, 30020 Prometheus, 30040 Jupyter, 30050 Nuclio
   - 30060 MLRun UI, 30070 MLRun API, 30093 SeaweedFS Admin, 30094 SeaweedFS S3
   - 30100 Pipelines, 30110 TimescaleDB
   - Create a NodePort service only for user-facing UIs or APIs; internal-only components use ClusterIP.
4. Storage credentials — mount the existing `storage-credentials` Secret via `envFrom.secretRef`; do not create a second credentials secret. Expose all port numbers and other tunables as `values.yaml` keys rather than hardcoding them in templates.
5. CRD dependencies — if the component depends on CRDs from a sub-chart, use `helm.sh/hook: post-install,post-upgrade` with an appropriate `hook-weight` on the CRs (see `templates/kafka/` for the established pattern).
6. Keep Secret and ConfigMap names consistent with existing patterns (`storage-credentials`, `mlrun-common-env`, etc.). Add env config to `templates/config/` following the `mlrun-common-env` / `jupyter-common-env` pattern. Each Kubernetes resource that supports limits and requests should expose them in `values.yaml`.
7. Add the component's service URL to `templates/NOTES.txt`, update `charts/mlrun-ce/README.md` if a new NodePort is exposed, and add a section to this AGENTS.md describing the component's architecture and dependencies.
8. Bump `charts/mlrun-ce/Chart.yaml` and run `make helm-lint` before opening a PR.
