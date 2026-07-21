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

{{/*
Expand holmes's service account name.

The upstream holmes chart hardcodes automountServiceAccountToken: false
and ships no ClusterRoleBinding, so its kubernetes toolsets get force-
disabled at startup. Argus owns a properly-permissioned SA (automount
enabled, bound to the view ClusterRole) and tells holmes to use it via
customServiceAccountName.
*/}}
{{- define "argus.holmesServiceAccountName" -}}
{{- default (printf "%s-holmes-sa" .Release.Name) .Values.holmes.rbac.serviceAccountName -}}
{{- end -}}
