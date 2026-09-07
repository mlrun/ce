{{/* vim: set filetype=mustache: */}}

{{/*
Create fully qualified names.
*/}}

{{- define "mlrun-ce.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mlrun-ce.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := (include "mlrun-ce.name" .) -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.shared-persistency-pvc.fullname" -}}
{{- if (index .Values.mlrun.api.extraPersistentVolumeMounts 0).existingClaim -}}
{{- (index .Values.mlrun.api.extraPersistentVolumeMounts 0).existingClaim -}}
{{- else -}}
{{- printf "%s-shared-pvc"  (include "mlrun-ce.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Copied over from mlrun chart to duplicate the logic without constraining the values
*/}}
{{- define "mlrun-ce.jupyter.fullname" -}}
{{- if .Values.jupyterNotebook.fullnameOverride -}}
{{- .Values.jupyterNotebook.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.jupyterNotebook.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.jupyter.mlrunUIURL" -}}
{{- if .Values.jupyterNotebook.mlrunUIURL -}}
{{- .Values.jupyterNotebook.mlrunUIURL -}}
{{- else -}}
{{- printf "http://%s:%s/mlrun" .Values.global.externalHostAddress (.Values.mlrun.ui.service.nodePort | toString) -}}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.jupyter.claimName" -}}
{{- if .Values.jupyterNotebook.persistence.existingClaim -}}
{{- .Values.jupyterNotebook.persistence.existingClaim -}}
{{- else -}}
{{- include "mlrun-ce.jupyter.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Copied over from mlrun chart to duplicate the logic without constraining the values
*/}}
{{- define "mlrun-ce.mlrun.api.fullname" -}}
{{- if .Values.mlrun.api.fullnameOverride -}}
{{- .Values.mlrun.api.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.mlrun.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- printf "%s-%s" .Release.Name .Values.mlrun.api.name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s-%s" .Release.Name $name .Values.mlrun.api.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}


{{/*
Copied over from mlrun chart to duplicate the logic without constraining the values
*/}}

{{- define "mlrun-ce.mlrun.db.fullname" -}}
{{- if .Values.mlrun.db.fullnameOverride -}}
{{- .Values.mlrun.db.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.mlrun.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- printf "%s-%s" .Release.Name .Values.mlrun.db.name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s-%s" .Release.Name $name .Values.mlrun.db.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Copied over from mlrun chart to duplicate the logic without constraining the values
*/}}
{{- define "mlrun-ce.mlrun.ui.fullname" -}}
{{- if .Values.mlrun.ui.fullnameOverride -}}
{{- .Values.mlrun.ui.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.mlrun.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- printf "%s-%s" .Release.Name .Values.mlrun.ui.name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s-%s" .Release.Name $name .Values.mlrun.ui.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.mlrun.api.port" -}}
{{- .Values.mlrun.api.service.port | int -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "mlrun-ce.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
=============================================================================
S3 Storage Backend Helpers
Supports both MinIO and SeaweedFS as S3-compatible storage backends
=============================================================================
*/}}

{{/*
S3 Service URL - returns the endpoint URL for SeaweedFS
*/}}
{{- define "mlrun-ce.s3.service.url" -}}
http://seaweedfs-s3.{{.Release.Namespace}}.svc.cluster.local:{{ .Values.seaweedfs.s3.port }}
{{- end -}}

{{/*
S3 Service Host - returns just the hostname for pipeline config
*/}}
{{- define "mlrun-ce.s3.service.host" -}}
seaweedfs-s3.{{.Release.Namespace}}.svc.cluster.local
{{- end -}}

{{/*
S3 Service Port - returns the port for pipeline config
*/}}
{{- define "mlrun-ce.s3.service.port" -}}
{{- .Values.seaweedfs.s3.port | toString -}}
{{- end -}}

{{/*
External S3 credentials (storage.s3.*).
Used by MLRun and Jupyter when storage.mode is s3, and by seaweedfs.remote when provider is s3.
*/}}
{{- define "mlrun-ce.storage.s3.accessKey" -}}
{{- .Values.storage.s3.accessKey -}}
{{- end -}}

{{- define "mlrun-ce.storage.s3.secretKey" -}}
{{- .Values.storage.s3.secretKey -}}
{{- end -}}

{{/*
S3 Access Key - for MLRun and Jupyter.
In "local" mode uses the internal SeaweedFS credential (storage.local.accessKey).
In "s3" mode uses the external AWS credential (storage.s3.accessKey).
*/}}
{{- define "mlrun-ce.s3.accessKey" -}}
{{- if eq .Values.storage.mode "local" -}}
{{- .Values.storage.local.accessKey -}}
{{- else -}}
{{- include "mlrun-ce.storage.s3.accessKey" . -}}
{{- end -}}
{{- end -}}

{{/*
S3 Secret Key - for MLRun and Jupyter.
*/}}
{{- define "mlrun-ce.s3.secretKey" -}}
{{- if eq .Values.storage.mode "local" -}}
{{- .Values.storage.local.secretKey -}}
{{- else -}}
{{- include "mlrun-ce.storage.s3.secretKey" . -}}
{{- end -}}
{{- end -}}

{{/*
S3 Bucket - for MLRun and Jupyter.
*/}}
{{- define "mlrun-ce.s3.bucket" -}}
{{- if eq .Values.storage.mode "local" -}}
{{- .Values.storage.local.bucket -}}
{{- else -}}
{{- coalesce .Values.global.infrastructure.aws.bucketName .Values.storage.s3.bucket "mlrun" -}}
{{- end -}}
{{- end -}}

{{/*
Used by: SeaweedFS IAM config, bucket-init job, and KFP Pipelines.
*/}}
{{- define "mlrun-ce.seaweedfs.s3.accessKey" -}}
{{- .Values.storage.local.accessKey -}}
{{- end -}}

{{/*
SeaweedFS S3 Secret Key - sourced from storage.local.secretKey.
*/}}
{{- define "mlrun-ce.seaweedfs.s3.secretKey" -}}
{{- .Values.storage.local.secretKey -}}
{{- end -}}

{{/*
SeaweedFS S3 Bucket - sourced from storage.local.bucket.
*/}}
{{- define "mlrun-ce.seaweedfs.s3.bucket" -}}
{{- .Values.storage.local.bucket -}}
{{- end -}}

{{/*
SeaweedFS cluster addresses (allInOne mode with fullnameOverride: seaweedfs).
*/}}
{{- define "mlrun-ce.seaweedfs.filer.port" -}}
{{- .Values.seaweedfs.filer.port | default 8888 -}}
{{- end -}}

{{- define "mlrun-ce.seaweedfs.filerAddress" -}}
seaweedfs-all-in-one.{{ .Release.Namespace }}.svc.cluster.local:{{ include "mlrun-ce.seaweedfs.filer.port" . }}
{{- end -}}

{{- define "mlrun-ce.seaweedfs.masterAddress" -}}
seaweedfs-all-in-one.{{ .Release.Namespace }}.svc.cluster.local:9333
{{- end -}}

{{- define "mlrun-ce.seaweedfs.image" -}}
{{- $repo := .Values.seaweedfs.global.repository | default .Values.seaweedfs.image.repository | default "chrislusf/seaweedfs" -}}
{{- $tag := .Values.seaweedfs.image.tag | default "4.17" -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}

{{- define "mlrun-ce.seaweedfs.remote.enabled" -}}
{{- and .Values.seaweedfs.enabled .Values.seaweedfs.remote.enabled -}}
{{- end -}}

{{- define "mlrun-ce.seaweedfs.remote.localBucket" -}}
{{- .Values.storage.local.bucket -}}
{{- end -}}

{{- define "mlrun-ce.seaweedfs.remote.remoteBucket" -}}
{{- required "seaweedfs.remote.bucket is required when seaweedfs.remote.enabled is true" .Values.seaweedfs.remote.bucket -}}
{{- end -}}

{{/*
Shell snippet: export AZURE_STORAGE_ACCOUNT / AZURE_STORAGE_ACCESS_KEY from AZURE_STORAGE_CONNECTION_STRING when unset.
Used by seaweedfs-remote-config Job and seaweedfs-remote-gateway Deployment (filer.remote.gateway reads env at sync time).
*/}}
{{- define "mlrun-ce.seaweedfs.remote.azureCredentialBootstrap" -}}
if [ -z "${AZURE_STORAGE_ACCOUNT:-}" ] || [ -z "${AZURE_STORAGE_ACCESS_KEY:-}" ]; then
  if [ -z "${AZURE_STORAGE_CONNECTION_STRING:-}" ]; then
    echo "ERROR: set storage.azure.accountName+accountKey or connectionString."
    exit 1
  fi
  if echo "${AZURE_STORAGE_CONNECTION_STRING}" | grep -q 'SharedAccessSignature='; then
    echo "ERROR: SAS-only connection strings are not supported."
    exit 1
  fi
  _parsed_account=""
  _parsed_key=""
  _old_ifs="${IFS}"
  IFS=';'
  for _part in ${AZURE_STORAGE_CONNECTION_STRING}; do
    case "${_part}" in
      AccountName=*) _parsed_account="${_part#AccountName=}" ;;
      AccountKey=*) _parsed_key="${_part#AccountKey=}" ;;
    esac
  done
  IFS="${_old_ifs}"
  if [ -z "${_parsed_account}" ] || [ -z "${_parsed_key}" ]; then
    echo "ERROR: could not parse AccountName/AccountKey from connectionString."
    exit 1
  fi
  export AZURE_STORAGE_ACCOUNT="${_parsed_account}"
  export AZURE_STORAGE_ACCESS_KEY="${_parsed_key}"
fi
{{- end -}}

{{/*
Default KFP pipeline root URI scheme/path.
*/}}
{{- define "mlrun-ce.pipelines.defaultPipelineRoot" -}}
{{- $bucket := include "mlrun-ce.seaweedfs.s3.bucket" . -}}
minio://{{ $bucket }}/v2/artifacts
{{- end -}}

{{/*
True when KFP should wait for the in-cluster SeaweedFS S3 gateway at startup.
*/}}
{{- define "mlrun-ce.pipelines.usesLocalSeaweedFS" -}}
{{- .Values.seaweedfs.enabled -}}
{{- end -}}

{{/*
Legacy Minio Service URL - kept for backward compatibility
*/}}
{{- define "mlrun-ce.minio.service.url" -}}
{{ include "mlrun-ce.s3.service.url" . }}
{{- end -}}
{{- define "mlrun-ce.minio-pipeline.service.url" -}}
{{ include "mlrun-ce.s3.service.host" . }}
{{- end -}}

{{/*
=============================================================================
Storage Path Helpers
Handles both S3 and Azure Blob storage backends
=============================================================================
*/}}

{{- define "mlrun-ce.httpdb.realPath" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://
{{- else -}}
s3://
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.artifactPath" -}}
{{- $bucket := include "mlrun-ce.s3.bucket" . -}}
{{- $container := .Values.storage.azure.containerName | default "" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://{{ $container }}/projects/{{ `{{run.project}}` }}/artifacts
{{- else -}}
s3://{{ $bucket }}/projects/{{ `{{run.project}}` }}/artifacts
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.featureStore.dataPrefix" -}}
{{- $bucket := include "mlrun-ce.s3.bucket" . -}}
{{- $container := .Values.storage.azure.containerName | default "" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://{{ $container }}/projects/{project}/FeatureStore/{name}/{kind}
{{- else -}}
s3://{{ $bucket }}/projects/{project}/FeatureStore/{name}/{kind}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.model-endpoint.monitoring.userSpace" -}}
{{- $bucket := include "mlrun-ce.s3.bucket" . -}}
{{- $container := .Values.storage.azure.containerName | default "" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://{{ $container }}/projects/{{ `{{project}}` }}/model-endpoints/{{ `{{kind}}` }}
{{- else -}}
s3://{{ $bucket }}/projects/{{ `{{project}}` }}/model-endpoints/{{ `{{kind}}` }}
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.model-endpoint.monitoring.application" -}}
{{- $bucket := include "mlrun-ce.s3.bucket" . -}}
{{- $container := .Values.storage.azure.containerName | default "" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://{{ $container }}/users/pipelines/{{ `{{project}}` }}/monitoring-apps/
{{- else -}}
s3://{{ $bucket }}/users/pipelines/{{ `{{project}}` }}/monitoring-apps/
{{- end -}}
{{- end -}}

{{- define "mlrun-ce.model-endpoint.monitoring.default" -}}
{{- $bucket := include "mlrun-ce.s3.bucket" . -}}
{{- $container := .Values.storage.azure.containerName | default "" -}}
{{- if eq .Values.storage.mode "azure-blob" -}}
az://{{ $container }}/projects/{{ `{{project}}` }}/model-endpoints/{{ `{{kind}}` }}
{{- else -}}
s3://{{ $bucket }}/projects/{{ `{{project}}` }}/model-endpoints/{{ `{{kind}}` }}
{{- end -}}
{{- end -}}

{{/*
MLRun storage auto mount params
Global toggle is for fast toggling between on-prem/standalone and s3 cases
Can be overriden if params are explicitly specified
Uses SeaweedFS as the storage backend
*/}}
{{- define "mlrun.storage.auto.mount.params" -}}
  {{- if .Values.mlrun.storageAutoMountParams -}}
    {{ .Values.mlrun.storageAutoMountParams }}
  {{- else if not .Values.global.infrastructure.aws.s3NonAnonymous -}}
    "secret_name=storage-credentials"
  {{- else -}}
    "non_anonymous=True"
  {{- end -}}
{{- end -}}


{{/*
Mlrun DB labels
*/}}
{{- define "mlrun-ce.mlrun.db.labels" -}}
{{ include "mlrun-ce.common.labels" . }}
{{ include "mlrun-ce.mlrun.db.selectorLabels" . }}
{{- end -}}

{{/*
Mlrun DB selector labels
*/}}
{{- define "mlrun-ce.mlrun.db.selectorLabels" -}}
{{ include "mlrun-ce.common.selectorLabels" . }}
app.kubernetes.io/component: {{ .Values.mlrun.db.name | quote }}
{{- end -}}

{{/*
Mlrun API labels
*/}}
{{- define "mlrun-ce.mlrun.api.labels" -}}
{{ include "mlrun-ce.common.labels" . }}
{{ include "mlrun-ce.mlrun.api.selectorLabels" . }}
{{- end -}}


{{/*
Mlrun API selector labels
*/}}
{{- define "mlrun-ce.mlrun.api.selectorLabels" -}}
{{ include "mlrun-ce.common.selectorLabels" . }}
app.kubernetes.io/component: {{ .Values.mlrun.api.name | quote }}
{{- end -}}


{{/*
Common labels
*/}}
{{- define "mlrun-ce.common.labels" -}}
helm.sh/chart: {{ include "mlrun-ce.chart" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Common selector labels
*/}}
{{- define "mlrun-ce.common.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mlrun-ce.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Jupyter selector labels
*/}}
{{- define "mlrun-ce.jupyter.selectorLabels" -}}
{{ include "mlrun-ce.common.selectorLabels" . }}
app.kubernetes.io/component: {{ .Values.jupyterNotebook.name | quote }}
{{- end -}}

{{/*
Jupyter labels
*/}}
{{- define "mlrun-ce.jupyter.labels" -}}
{{ include "mlrun-ce.common.labels" . }}
{{ include "mlrun-ce.jupyter.selectorLabels" . }}
{{- end -}}




{{/*
Pipelines selector labels
*/}}
{{- define "mlrun-ce.pipelines.selectorLabels" -}}
{{ include "mlrun-ce.common.selectorLabels" . }}
app.kubernetes.io/component: {{ .Values.pipelines.name | quote }}
{{- end -}}

{{/*
Pipelines labels
*/}}
{{- define "mlrun-ce.pipelines.labels" -}}
{{ include "mlrun-ce.common.labels" . }}
{{ include "mlrun-ce.pipelines.selectorLabels" . }}
{{- end -}}

{{/*
TimescaleDB helpers
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "mlrun-ce.timescaledb.name" -}}
{{- default "timescaledb" .Values.timescaledb.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mlrun-ce.timescaledb.fullname" -}}
{{- if .Values.timescaledb.fullnameOverride }}
{{- .Values.timescaledb.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
    {{- $name := default "timescaledb" .Values.timescaledb.nameOverride }}
    {{- if contains $name .Release.Name }}
        {{- .Release.Name | trunc 63 | trimSuffix "-" }}
    {{- else }}
        {{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
    {{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "mlrun-ce.timescaledb.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
TimescaleDB Common labels
*/}}
{{- define "mlrun-ce.timescaledb.labels" -}}
helm.sh/chart: {{ include "mlrun-ce.timescaledb.chart" . }}
{{ include "mlrun-ce.timescaledb.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
TimescaleDB Selector labels
*/}}
{{- define "mlrun-ce.timescaledb.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mlrun-ce.timescaledb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: timescaledb
{{- end }}

{{/*
TimescaleDB connection string for MLRun model monitoring
*/}}
{{- define "mlrun-ce.timescaledb.connectionString" -}}
postgresql://{{ .Values.timescaledb.auth.username | urlquery }}:{{ .Values.timescaledb.auth.password | urlquery }}@{{ include "mlrun-ce.timescaledb.fullname" . }}:{{ .Values.timescaledb.service.port }}/{{ .Values.timescaledb.auth.database }}
{{- end }}

{{/*
=============================================================================
OpenTelemetry helpers
=============================================================================
*/}}

{{/*
OpenTelemetry Collector name
*/}}
{{- define "mlrun-ce.otel.collector.name" -}}
{{- default "otel" .Values.opentelemetry.collector.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
OpenTelemetry Collector fullname
*/}}
{{- define "mlrun-ce.otel.collector.fullname" -}}
{{- if .Values.opentelemetry.collector.fullnameOverride }}
{{- .Values.opentelemetry.collector.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "otel" .Values.opentelemetry.collector.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
OpenTelemetry Instrumentation name
*/}}
{{- define "mlrun-ce.otel.instrumentation.name" -}}
{{- default "otel-instrumentation" .Values.opentelemetry.instrumentation.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
OpenTelemetry Instrumentation fullname
*/}}
{{- define "mlrun-ce.otel.instrumentation.fullname" -}}
{{- if .Values.opentelemetry.instrumentation.fullnameOverride }}
{{- .Values.opentelemetry.instrumentation.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "otel-instrumentation" .Values.opentelemetry.instrumentation.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
OpenTelemetry common labels
*/}}
{{- define "mlrun-ce.otel.labels" -}}
{{ include "mlrun-ce.common.labels" . }}
{{ include "mlrun-ce.otel.selectorLabels" . }}
{{- end }}

{{/*
OpenTelemetry selector labels
*/}}
{{- define "mlrun-ce.otel.selectorLabels" -}}
{{ include "mlrun-ce.common.selectorLabels" . }}
app.kubernetes.io/component: opentelemetry
{{- end }}

{{/*
OpenTelemetryCollector CR manifest for use in the CRD readiness job
*/}}
{{- define "mlrun-ce.otel.collector.manifest" -}}
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: {{ include "mlrun-ce.otel.collector.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "mlrun-ce.otel.labels" . | nindent 4 }}
spec:
  mode: {{ .Values.opentelemetry.collector.mode }}
  upgradeStrategy: {{ .Values.opentelemetry.collector.upgradeStrategy }}
  managementState: managed
  image: {{ (index .Values "opentelemetry-operator").manager.collectorImage.repository }}:{{ (index .Values "opentelemetry-operator").manager.collectorImage.tag }}
  resources:
    {{- toYaml .Values.opentelemetry.collector.resources | nindent 4 }}
  config:
    {{- toYaml .Values.opentelemetry.collector.config | nindent 4 }}
{{- end }}

{{/*
Instrumentation CR manifest for use in the CRD readiness job
*/}}
{{- define "mlrun-ce.otel.instrumentation.manifest" -}}
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: {{ include "mlrun-ce.otel.instrumentation.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "mlrun-ce.otel.labels" . | nindent 4 }}
spec:
  exporter:
    endpoint: http://{{ include "mlrun-ce.otel.collector.fullname" . }}-collector:{{ .Values.opentelemetry.collector.otlp.httpPort }}
  propagators:
    {{- toYaml .Values.opentelemetry.instrumentation.propagators | nindent 4 }}
  sampler:
    type: {{ .Values.opentelemetry.instrumentation.sampler.type }}
    argument: {{ .Values.opentelemetry.instrumentation.sampler.argument | quote }}
  env:
    - name: OTEL_SERVICE_NAME
      valueFrom:
        fieldRef:
          fieldPath: metadata.name
    - name: OTEL_METRICS_EXPORTER
      value: otlp
    - name: OTEL_TRACES_EXPORTER
      value: otlp
    - name: OTEL_LOGS_EXPORTER
      value: none
  {{- if .Values.opentelemetry.instrumentation.python.enabled }}
  python:
    image: {{ .Values.opentelemetry.instrumentation.python.image.repository }}:{{ .Values.opentelemetry.instrumentation.python.image.tag }}
    resourceRequirements:
      {{- toYaml .Values.opentelemetry.instrumentation.python.resources | nindent 6 }}
    env:
      - name: OTEL_PYTHON_LOG_CORRELATION
        value: "true"
      - name: OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED
        value: "false"
      - name: OTEL_PYTHON_DISABLED_INSTRUMENTATIONS
        value: "aws_lambda"
  {{- end }}
  {{- if .Values.opentelemetry.instrumentation.java.enabled }}
  java:
    image: {{ .Values.opentelemetry.instrumentation.java.image.repository }}:{{ .Values.opentelemetry.instrumentation.java.image.tag }}
    resourceRequirements:
      {{- toYaml .Values.opentelemetry.instrumentation.java.resources | nindent 6 }}
    env:
      - name: OTEL_INSTRUMENTATION_COMMON_DEFAULT_ENABLED
        value: "true"
  {{- end }}
{{- end }}
{{/*
OTel pod label — marks a pod as OTel-monitored for metric enrichment and discovery.
Namespace-level instrumentation annotation (set by namespace-label job) handles Python auto-instrumentation.
Wrap usage with: {{- if and .Values.opentelemetry.collector.enabled .Values.opentelemetry.instrumentation.enabled }}
*/}}
{{- define "mlrun-ce.otel.podLabels" -}}
mlrun.io/otel: "true"
{{- end }}
