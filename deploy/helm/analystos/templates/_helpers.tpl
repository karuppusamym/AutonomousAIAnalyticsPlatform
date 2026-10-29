{{- define "analystos.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "analystos.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "analystos.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "analystos.image" -}}
{{- $reg := .Values.image.registry -}}
{{- $repo := .Values.image.repository -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if $reg }}{{ printf "%s/%s:%s" $reg $repo $tag }}{{ else }}{{ printf "%s:%s" $repo $tag }}{{ end -}}
{{- end -}}

{{- define "analystos.webImage" -}}
{{- $reg := .Values.image.registry -}}
{{- $tag := default .Chart.AppVersion .Values.webImage.tag -}}
{{- if $reg }}{{ printf "%s/%s:%s" $reg .Values.webImage.repository $tag }}{{ else }}{{ printf "%s:%s" .Values.webImage.repository $tag }}{{ end -}}
{{- end -}}

{{- define "analystos.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}{{ default (include "analystos.fullname" .) .Values.serviceAccount.name }}{{- else -}}{{ default "default" .Values.serviceAccount.name }}{{- end -}}
{{- end -}}

{{/* Shared pod plumbing for every AnalystOS (Python) container: config + secrets as env, writable
     emptyDirs under a read-only root filesystem, optional models / OIDC mapping mounts. */}}
{{- define "analystos.appEnv" -}}
envFrom:
  - configMapRef:
      name: {{ include "analystos.fullname" . }}-config
  - secretRef:
      name: {{ required "secrets.existingSecret is required (credentials are never chart values)" .Values.secrets.existingSecret }}
volumeMounts:
  - {name: tmp, mountPath: /tmp}
  - {name: var, mountPath: /app/var}
  {{- if .Values.models.customConfig }}
  - {name: models, mountPath: /etc/analystos/models.yaml, subPath: models.yaml, readOnly: true}
  {{- end }}
  {{- if .Values.oidc.enabled }}
  - {name: oidc, mountPath: /etc/analystos/oidc.yaml, subPath: oidc.yaml, readOnly: true}
  {{- end }}
securityContext:
  {{- toYaml .Values.containerSecurityContext | nindent 2 }}
{{- end -}}

{{/* The artifact store as isolated workers reach it: the API Service (ADR-0022, P7-06). */}}
{{- define "analystos.artifactUrl" -}}
{{- printf "http://%s-api:%v" (include "analystos.fullname" .) .Values.api.service.port -}}
{{- end -}}

{{/* Comma-separated isolated pools this release deploys (replicas > 0): ANALYSTOS_ISOLATED_POOLS for the API. */}}
{{- define "analystos.isolatedPools" -}}
{{- $out := list -}}
{{- range $name, $pool := .Values.workers -}}
{{- if and $pool $pool.isolated (gt (int $pool.replicas) 0) -}}
{{- $out = append $out $pool.queues -}}
{{- end -}}
{{- end -}}
{{- join "," $out -}}
{{- end -}}

{{- define "analystos.appVolumes" -}}
- {name: tmp, emptyDir: {}}
- {name: var, emptyDir: {}}
{{- if .Values.models.customConfig }}
- name: models
  configMap: {name: {{ include "analystos.fullname" . }}-models}
{{- end }}
{{- if .Values.oidc.enabled }}
- name: oidc
  configMap: {name: {{ include "analystos.fullname" . }}-oidc}
{{- end }}
{{- end -}}

{{/* Pod security context; under OpenShift (restricted-v2 SCC) without fixed UID/GID/fsGroup. */}}
{{- define "analystos.podSecurityContext" -}}
{{- $psc := default .root.Values.podSecurityContext .podSecurityContext -}}
{{- if .root.Values.openshift.enabled -}}
{{- $psc = omit $psc "runAsUser" "runAsGroup" "fsGroup" -}}
{{- end -}}
{{- toYaml $psc -}}
{{- end -}}

{{- define "analystos.podCommon" -}}
serviceAccountName: {{ default (include "analystos.serviceAccountName" .root) .serviceAccountName }}
automountServiceAccountToken: {{ .root.Values.serviceAccount.automountToken }}
securityContext:
  {{- include "analystos.podSecurityContext" . | nindent 2 }}
{{- with .runtimeClassName }}
runtimeClassName: {{ . }}
{{- end }}
{{- with .root.Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .root.Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .root.Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .root.Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if .root.Values.topologySpread }}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "analystos.selector" . | nindent 8 }}
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "analystos.selector" . | nindent 8 }}
{{- end }}
{{- end -}}

{{- define "analystos.pdb" -}}
{{- if .pdb.enabled }}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "analystos.fullname" .root }}-{{ .component }}
  labels:
    {{- include "analystos.labels" .root | nindent 4 }}
spec:
  minAvailable: {{ default 1 .pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "analystos.selector" . | nindent 6 }}
{{- end }}
{{- end -}}

{{/* The runtimeClassName of a worker pool: its own, else gVisor for `gvisor: true` pools when gvisor.enabled. */}}
{{- define "analystos.poolRuntimeClass" -}}
{{- if .pool.runtimeClassName -}}
{{- .pool.runtimeClassName -}}
{{- else if and .root.Values.gvisor.enabled .pool.gvisor -}}
{{- .root.Values.gvisor.runtimeClassName -}}
{{- end -}}
{{- end -}}

{{/* The sandbox an isolated worker must find itself in (ANALYSTOS_WORKER_REQUIRE_RUNTIME, S07-T1): the pool's
     `requireRuntime` when set ("" turns the check off), else `gvisor` when the pool runs under the gVisor class.
     The worker exits 78 at start when its kernel says otherwise (workers/isolation.py::check_runtime). */}}
{{- define "analystos.poolRequiredRuntime" -}}
{{- if hasKey .pool "requireRuntime" -}}
{{- .pool.requireRuntime -}}
{{- else if and .runtime (eq .runtime .root.Values.gvisor.runtimeClassName) -}}
gvisor
{{- end -}}
{{- end -}}

{{/* "true" when KEDA scales this pool (then no HPA and no fixed replicas). */}}
{{- define "analystos.kedaActive" -}}
{{- if and .root.Values.keda.enabled .pool.keda .pool.keda.enabled -}}true{{- end -}}
{{- end -}}

{{- define "analystos.hpa" -}}
{{- if and .autoscaling .autoscaling.enabled }}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "analystos.fullname" .root }}-{{ .component }}
  labels:
    {{- include "analystos.labels" .root | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "analystos.fullname" .root }}-{{ .component }}
  minReplicas: {{ .autoscaling.minReplicas }}
  maxReplicas: {{ .autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: {type: Utilization, averageUtilization: {{ .autoscaling.targetCPUUtilizationPercentage }}}
{{- end }}
{{- end -}}

{{/* The non-secret ANALYSTOS_* settings: the release ConfigMap and the migration Job's hook copy (migrate-job.yaml). */}}
{{- define "analystos.configData" -}}
ANALYSTOS_ENV: {{ .Values.env | quote }}
ANALYSTOS_ORCHESTRATOR: {{ .Values.config.orchestrator | quote }}
ANALYSTOS_TEMPORAL_ADDRESS: {{ .Values.config.temporalAddress | quote }}
ANALYSTOS_TEMPORAL_NAMESPACE: {{ .Values.config.temporalNamespace | quote }}
ANALYSTOS_TEMPORAL_QUEUE_PREFIX: {{ .Values.config.temporalQueuePrefix | quote }}
ANALYSTOS_SUPERSET_URL: {{ .Values.config.supersetUrl | quote }}
ANALYSTOS_WEB_URL: {{ .Values.config.webUrl | quote }}
ANALYSTOS_CORS_ORIGINS: {{ .Values.config.corsOrigins | quote }}
ANALYSTOS_GRAPH_ENABLED: {{ .Values.config.graphEnabled | quote }}
ANALYSTOS_VECTOR_BACKEND: {{ .Values.config.vectorBackend | default "pgvector" | quote }}
ANALYSTOS_SANDBOX_ISOLATION: {{ .Values.config.sandboxIsolation | quote }}
ANALYSTOS_AIR_GAPPED: {{ .Values.airGapped.enabled | quote }}
# Observability (S12 §2): workers/scheduler serve /metrics on this port; a pod's processes merge in the dir.
ANALYSTOS_METRICS_PORT: {{ .Values.monitoring.metricsPort | quote }}
ANALYSTOS_METRICS_DIR: /tmp/analystos-metrics
ANALYSTOS_OTEL_ENABLED: {{ .Values.monitoring.otel.enabled | quote }}
{{- if .Values.monitoring.otel.endpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT: {{ .Values.monitoring.otel.endpoint | quote }}
{{- end }}
# Isolated compute pools (ADR-0022, P7-06): the API dispatches to those this release deploys.
ANALYSTOS_ISOLATED_POOLS: {{ include "analystos.isolatedPools" . | quote }}
ANALYSTOS_WORKER_ARTIFACT_URL: {{ include "analystos.artifactUrl" . | quote }}
{{- with .Values.database.pools }}
ANALYSTOS_DB_POOL_MODE: {{ .mode | quote }}
ANALYSTOS_DB_POOL_RECYCLE: {{ .recycleSeconds | quote }}
ANALYSTOS_DB_POOL_SIZE: {{ .control.size | quote }}
ANALYSTOS_DB_MAX_OVERFLOW: {{ .control.maxOverflow | quote }}
ANALYSTOS_DB_POOL_TIMEOUT: {{ .control.timeoutSeconds | quote }}
ANALYSTOS_ANALYTICS_POOL_SIZE: {{ .analytics.size | quote }}
ANALYSTOS_ANALYTICS_MAX_OVERFLOW: {{ .analytics.maxOverflow | quote }}
ANALYSTOS_ANALYTICS_POOL_TIMEOUT: {{ .analytics.timeoutSeconds | quote }}
ANALYSTOS_LOADER_POOL_SIZE: {{ .loader.size | quote }}
ANALYSTOS_LOADER_MAX_OVERFLOW: {{ .loader.maxOverflow | quote }}
ANALYSTOS_LOADER_POOL_TIMEOUT: {{ .loader.timeoutSeconds | quote }}
{{- end }}
ANALYSTOS_DB_TRANSACTION_POOLER: {{ or .Values.database.transactionPooler .Values.pgbouncer.enabled | quote }}
{{- if .Values.models.customConfig }}
ANALYSTOS_MODELS_CONFIG: /etc/analystos/models.yaml
{{- else if .Values.models.modelsConfig }}
ANALYSTOS_MODELS_CONFIG: {{ printf "/app/config/%s" .Values.models.modelsConfig | quote }}
{{- else if .Values.airGapped.enabled }}
ANALYSTOS_MODELS_CONFIG: /app/config/models.airgapped.yaml
{{- end }}
{{- if .Values.airGapped.localModel.url }}
ANALYSTOS_LOCAL_LLM_URL: {{ .Values.airGapped.localModel.url | quote }}
{{- end }}
ANALYSTOS_PASSWORD_LOGIN: {{ .Values.oidc.passwordLogin | quote }}
{{- if .Values.oidc.enabled }}
ANALYSTOS_OIDC_ISSUER: {{ required "oidc.issuer is required when oidc.enabled" .Values.oidc.issuer | quote }}
ANALYSTOS_OIDC_CLIENT_ID: {{ required "oidc.clientId is required when oidc.enabled" .Values.oidc.clientId | quote }}
ANALYSTOS_OIDC_REDIRECT_URI: {{ required "oidc.redirectUri is required when oidc.enabled" .Values.oidc.redirectUri | quote }}
ANALYSTOS_OIDC_SCOPES: {{ .Values.oidc.scopes | quote }}
ANALYSTOS_OIDC_GROUPS_CLAIM: {{ .Values.oidc.groupsClaim | quote }}
ANALYSTOS_OIDC_PROVIDER_NAME: {{ .Values.oidc.providerName | quote }}
ANALYSTOS_OIDC_MAPPING_FILE: /etc/analystos/oidc.yaml
{{- end }}
{{- range $k, $v := .Values.config.extra }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}
