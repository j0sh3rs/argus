{{/*
Expand the forwarder's service account name.
*/}}
{{- define "argus.forwarderServiceAccountName" -}}
{{- if .Values.forwarder.serviceAccount.create -}}
{{- default (printf "%s-forwarder" .Release.Name) .Values.forwarder.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.forwarder.serviceAccount.name -}}
{{- end -}}
{{- end -}}
