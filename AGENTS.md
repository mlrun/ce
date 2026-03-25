# AGENTS.md

## Overview

This is a Helm umbrella chart repository for **MLRun Community Edition (CE)** — an open-source MLOps stack. The main chart lives at `charts/mlrun-ce/` and bundles: Nuclio, MLRun, Jupyter, MPI Operator, SeaweedFS (S3-compatible storage), Spark Operator, Kubeflow Pipelines, Prometheus stack, TimescaleDB, and Strimzi Kafka Operator.

## Commands

```bash
# Lint the helm chart (requires helm and chart-testing `ct` installed)
make helm-lint

# Update sub-chart dependencies (run before lint or packaging)
make helm-update-dependencies

# Add all required helm repos from requirements.yaml
make helm-repo-add

# Package the chart as a tarball
make package

# Run full local end-to-end test on a Kind cluster (requires docker, kind, kubectl, helm)
./tests/kind-test.sh full          # Create Kind cluster + install chart
./tests/kind-test.sh create        # Create cluster only
./tests/kind-test.sh install       # Install chart (assumes cluster exists)
./tests/kind-test.sh verify        # Verify installation
./tests/kind-test.sh delete        # Delete Kind cluster
CLEANUP_ON_EXIT=true ./tests/kind-test.sh  # Auto-cleanup after test
```

## Architecture

### Chart Structure

`charts/mlrun-ce/` is an **umbrella chart** that:
1. Declares sub-chart dependencies in `requirements.yaml` (pulled from external Helm repos)
2. Provides additional Kubernetes resources via `templates/` that the sub-charts don't include
3. Wires all components together through shared `values.yaml`

### Template Organization (`charts/mlrun-ce/templates/`)

- `config/` — ConfigMaps and Secrets shared across components: MLRun env config, Jupyter env config, S3 credentials secret, Pipelines config, Spark config, Grafana dashboards
- `seaweedfs/` — SeaweedFS-specific resources: S3 IAM config secret, bucket init job, admin UI NodePort service, ingress
- `kafka/` — Kafka Strimzi custom resources: KafkaNodePool, Kafka cluster CR, bootstrap alias Service, RBAC, NetworkPolicy
- `timescaledb/` — TimescaleDB Deployment, Service, PVC
- `jupyter-notebook/` — Jupyter Deployment and supporting resources
- `pipelines/` — Kubeflow Pipelines resources
- `persistency/` — PVC definitions
- `aws/` — AWS-specific resources

### Key Design Patterns

**S3 credentials propagation**: The top-level `s3.accessKey`/`s3.secretKey`/`s3.bucket` values flow into a `s3-credentials` Secret (created by `templates/config/s3-credentials-secret.yaml`), which is then mounted via `envFrom` in MLRun API and Jupyter pods. SeaweedFS uses the same credentials via the `seaweedfs-s3-config` Secret.

**Global registry anchor**: `global.registry: &userRegistry` in `values.yaml` uses YAML anchors to multiplex the same docker registry config to both `nuclio.global.registry` and `mlrun.global.registry`.

**SeaweedFS as S3 backend**: SeaweedFS replaced MinIO. The helpers in `_helpers.tpl` (`mlrun-ce.s3.*`) generate the SeaweedFS service URL. Legacy `mlrun-ce.minio.*` helpers are kept as aliases pointing to the SeaweedFS helpers.

**Component enable/disable**: Most components can be disabled via `<component>.enabled: false`. The Kafka setup requires the Strimzi operator (deployed as a sub-chart via `strimzi-kafka-operator`) and custom Strimzi CRs in `templates/kafka/`.

### Values Files

- `charts/mlrun-ce/admin_installation_values.yaml` — admin install
- `charts/mlrun-ce/non_admin_installation_values.yaml` — non-admin install
- `charts/mlrun-ce/non_admin_cluster_ip_installation_values.yaml` — non-admin with ClusterIP
