{{- define "sentinel.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sentinel.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "sentinel.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "sentinel.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "sentinel.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "sentinel.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sentinel.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "sentinel.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "sentinel.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
sentinel.envAllow (M11.3, ADR-035): comma-joined names that agentctl's default-on env-allowlist
would otherwise strip. Emitted into SENTINEL_ENV_ALLOW so chart-supplied extraEnv keys, secret-sourced
env (extraSecretEnv), and any custom llmApiKey.envName actually reach the brain. Curated families
(ANTHROPIC_API_KEY, CHECKPOINT_DSN, LLM_ OTEL_ PW_ prefixes, ...) already pass unaided; re-listing is harmless.
*/}}
{{- define "sentinel.envAllow" -}}
{{- $names := list -}}
{{- range $k, $v := .Values.extraEnv -}}{{- $names = append $names $k -}}{{- end -}}
{{- if .Values.secrets.enabled -}}
{{- range .Values.secrets.extraSecretEnv -}}{{- $names = append $names .name -}}{{- end -}}
{{- $names = append $names (.Values.secrets.llmApiKey.envName | default "ANTHROPIC_API_KEY") -}}
{{- end -}}
{{- join "," $names -}}
{{- end -}}
