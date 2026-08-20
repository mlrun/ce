# Parameters Reference

Full flag and environment variable reference for `install.sh`. For guided walkthroughs
see the main [README](../README.md); for the `ce-config.yaml` schema and precedence
rules see [configuration.md](configuration.md).

Every flag has an environment-variable equivalent (for CI / non-interactive use), and
**flag > env var > `ce-config.yaml` > built-in default** everywhere.

---

## Flags

```
Usage: install.sh [options]

Options:
  -h, --help                     Show help
  --uninstall                    Uninstall the MLRun CE Helm release
  --hard-clean                   Use with --uninstall: delete all PVCs and PVs (data loss!)
  --skip-secret                  Skip creating the Docker registry secret
  --skip-validators               Skip the pre-install validators (K8s/Helm version,
                                   StorageClass, registry auth, NodePort conflicts, node capacity)
  -f, --values FILE              Use this YAML values file as the base for installation.
                                 Alone (no --config): fully self-contained, skips secret
                                 creation and prompts. Combined with --config: --config still
                                 creates the secret and resolves as --set overrides that win
                                 over this file. See configuration.md's "Precedence" section.
  --show-progress                Live deployment progress UI during install
  --disable-system-monitoring    Disable Grafana/Prometheus stack
  --disable-spark                Disable Spark operator
  --disable-mpi                  Disable MPI operator resources
  --disable-model-monitoring     Disable Kafka + TimescaleDB components
  --enable-ingress [CLASS]       Enable the chart's Ingress resources (class defaults to "nginx").
                                 Requires a controller already in the cluster — not installed for you.
  --enable-otel [MODE]            off|collector|full (default when bare: full). collector = operator
                                 +collector only; full = all 4 below. Granular flags still work too.
  --enable-otel-operator          Install the OpenTelemetry Operator (CRDs/webhook/manager)
  --enable-otel-collector         Deploy the OpenTelemetry Collector (OTLP -> Prometheus)
  --enable-otel-namespace-label   Auto-instrument every Python pod in the namespace
  --enable-otel-instrumentation   Create the Instrumentation CR
  --local-registry               Deploy a local registry:2 registry inside the cluster
  --chart-path DIR               Install from a local chart directory instead of the published repo;
                                 use ./charts/mlrun-ce for this repo's chart (runs helm dependency
                                 update on the path first)
  --ce-version VERSION           Pin the MLRun CE Helm chart version (default: latest; ignored with --chart-path)
  --dry-run                      Render the chart without deploying (helm --dry-run=server)
  --non-interactive              Never prompt; fail with exit 1 if a required value is missing
                                 (auto-set when CI=true)
  --config FILE                  Read defaults from a ce-config.yaml file's 'installer:' block
                                 (requires yq; flag/env values always win over the file).
                                 Can be combined with -f/--values — see configuration.md's "Precedence".
```

---

## Environment Variables

| Variable               | Default                           | Description                                              |
|------------------------|-----------------------------------|----------------------------------------------------------|
| `NAMESPACE`            | `mlrun`                           | Kubernetes namespace                                     |
| `RELEASE_NAME`         | `mlrun-ce`                        | Helm release name                                        |
| `REGISTRY_SECRET_NAME` | `registry-credentials`            | Name of the K8s Docker registry secret                   |
| `HELM_REPO_URL`        | `https://mlrun.github.io/ce`      | Helm chart repository URL                                |
| `REGISTRY_USERNAME`    | —                                 | Docker registry username                                 |
| `REGISTRY_PASSWORD`    | —                                 | Docker registry password (never read from ce-config.yaml) |
| `REGISTRY_PASSWORD_FILE` | —                               | Path to a file containing just the password (never read from ce-config.yaml; `REGISTRY_PASSWORD` wins if both are set) |
| `REGISTRY_SERVER`      | `https://index.docker.io/v1/`     | Docker server URL                                        |
| `REGISTRY_EMAIL`       | —                                 | Docker registry email                                    |
| `REGISTRY_URL`         | —                                 | Registry URL for images (e.g. `index.docker.io/myuser`) |
| `EXTERNAL_HOST_ADDRESS`| —                                 | Host address the cluster is reachable at (see [FAQ](faq.md) for the autodetect fallback chain) |
| `SKIP_REGISTRY_SECRET` | `false`                           | Set to `true` to skip secret creation                    |
| `SKIP_VALIDATORS`      | `false`                           | Set to `true` to skip the pre-install validators         |
| `MIN_K8S_VERSION`      | — (no floor)                      | Kubernetes version to warn below (`MAJOR.MINOR`); never blocks the install |
| `MIN_HELM_VERSION`     | `3.6`                             | Helm CLI version floor the blocking validator enforces (`MAJOR.MINOR`) |
| `DISABLE_SYSTEM_MONITORING` | `false`                     | Set to `true` to disable the Grafana/Prometheus stack    |
| `DISABLE_SPARK`        | `false`                           | Set to `true` to disable the Spark operator              |
| `DISABLE_MPI`          | `false`                           | Set to `true` to disable MPI operator resources          |
| `DISABLE_MODEL_MONITORING` | `false`                       | Set to `true` to disable Kafka + TimescaleDB components  |
| `SHOW_PROGRESS`        | `false`                           | Set to `true` for live progress UI                       |
| `PROGRESS_INTERVAL_SEC`| `10`                              | Refresh interval (seconds) for progress UI               |
| `ENABLE_INGRESS`       | `false`                           | Set to `true` to enable the chart's Ingress resources (requires your own controller) |
| `INGRESS_CLASS`        | `nginx`                           | Ingress class name                                       |
| `ENABLE_OTEL_OPERATOR`        | `false`                     | Set to `true` for `--set opentelemetry-operator.enabled=true`  |
| `ENABLE_OTEL_COLLECTOR`       | `false`                     | Set to `true` for `--set opentelemetry.collector.enabled=true` |
| `ENABLE_OTEL_NAMESPACE_LABEL` | `false`                     | Set to `true` for `--set opentelemetry.namespaceLabel.enabled=true` |
| `ENABLE_OTEL_INSTRUMENTATION` | `false`                     | Set to `true` for `--set opentelemetry.instrumentation.enabled=true` |
| `LOCAL_REGISTRY`       | `false`                           | Set to `true` to deploy a local `registry:2` registry   |
| `CHART_PATH`           | —                                 | Path to a local chart directory, e.g. `./charts/mlrun-ce` |
| `CE_VERSION`           | —                                 | Pin the chart version (ignored when `CHART_PATH` is set)|
| `DRY_RUN`              | `false`                           | Set to `true` to render the chart without deploying     |
| `NON_INTERACTIVE`      | `false`                           | Set to `true` to suppress all prompts                   |
| `CI`                   | —                                 | Set to `true` to auto-enable non-interactive mode       |
| `CONFIG_FILE`          | —                                 | Path to a `ce-config.yaml` file (same as `--config`)    |
| `KUBE_CONTEXT`         | —                                 | kubectl/helm context to use (default: current kubeconfig context) |
| `MLRUN_VERSION`        | —                                 | Pin mlrun api/ui image tag                              |
| `NUCLIO_VERSION`       | —                                 | Pin nuclio controller/dashboard image tag                |
