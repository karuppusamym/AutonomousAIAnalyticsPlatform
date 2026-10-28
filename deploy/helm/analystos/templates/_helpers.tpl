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

{{- define "analystos.podCommon" -}}
serviceAccountName: {{ include "analystos.serviceAccountName" .root }}
automountServiceAccountToken: {{ .root.Values.serviceAccount.automountToken }}
securityContext:
  {{- toYaml (default .root.Values.podSecurityContext .podSecurityContext) | nindent 2 }}
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
