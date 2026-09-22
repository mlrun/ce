# MLRun Community Edition: MLRun Open Source Stack for MLOps

This repo contains the Helm charts for the MLRun Community Edition (CE) - a full open source MLOps stack.

The Open source MLRun CE chart includes the following stack:

 * Nuclio - https://github.com/nuclio/nuclio
 * MLRun - https://github.com/mlrun/mlrun
 * Jupyter - https://github.com/jupyter/notebook (+MLRun integrated)
 * MPI Operator - https://github.com/kubeflow/mpi-operator
 * SeaweedFS - https://github.com/seaweedfs/seaweedfs (S3-compatible storage)
 * Spark Operator - https://github.com/GoogleCloudPlatform/spark-on-k8s-operator
 * Pipelines - https://github.com/kubeflow/pipelines
 * Prometheus stack - https://github.com/prometheus-community/helm-charts

## Installation

Refer to the installation instructions in the [README](charts/mlrun-ce/README.md) of the `mlrun-ce` chart.

For a scripted install, [`scripts/install.py`](scripts/README.md) wraps those steps — registry
secret, pre-install validation and `helm install` — behind a single command:

```bash
CE_TAG=mlrun-ce-0.12.0-rc.14
uvx --from "git+https://github.com/mlrun/ce@${CE_TAG}#subdirectory=scripts" \
  mlrun-ce-installer install
```

It needs [uv](https://docs.astral.sh/uv/), helm and kubectl; uv supplies the Python and the
installer's dependencies itself.
