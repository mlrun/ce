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
Minio Service URL
*/}}
{{- define "mlrun-ce.minio.service.url" -}}
http://minio.{{.Release.Namespace}}.svc.cluster.local:{{ .Values.minio.service.port }}
{{- end -}}
{{- define "mlrun-ce.minio-pipeline.service.url" -}}
minio.{{.Release.Namespace}}.svc.cluster.local
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
Model monitoring DSN
*/}}
{{- define "mlrun-ce.mlrun.modelMonitoring.DSN" -}}
{{- if .Values.mlrun.modelMonitoring.dsn -}}
{{ .Values.mlrun.modelMonitoring.dsn }}
{{- else -}}
{{- if eq "mysql" .Values.mlrun.httpDB.dbType -}}
{{ .Values.mlrun.httpDB.dsn }}_model_monitoring
{{- end -}}
{{- end -}}
{{- end -}}

