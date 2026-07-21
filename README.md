# argus

HolmesGPT-based SRE triage agent for Kubernetes. Receives Prometheus
Alertmanager webhooks, investigates firing alerts via
[HolmesGPT](https://github.com/robusta-dev/holmesgpt) (pointed at an
OpenAI-compatible LLM gateway), and posts concise triage summaries to
Discord.

## Why

HolmesGPT is the strongest open-source k8s-native SRE investigator
(ReAct loop over kubectl, logs, Prometheus, Grafana, Loki), but it has
**no Discord sink** — Robusta deliberately gates Slack/Teams behind their
SaaS. This chart fills that gap with a ~200-line forwarder that wires
Holmes's `/api/chat` to Discord, replacing the custom tool-loop cruft
that previously lived in nixlab's `alert-responder`.

## Architecture

```
Alertmanager ──POST /webhook──> argus-forwarder ──POST /api/chat──> holmes
                                        │                              │
                                        │  ┌───────────────────────────┘
                                        │  ▼  (analysis markdown)
                                        └─▶ Discord webhook
```

- **holmes** (subchart `robusta/holmes`): owns the investigation. Read-only
  k8s RBAC (`view` ClusterRole), pointed at the LLM gateway via
  `OPENAI_API_BASE` + `modelList`.
- **forwarder** (this chart): webhook receiver, dedupe by fingerprint,
  severity filter, calls holmes, posts to Discord. **No cluster RBAC.**

## Install

```bash
helm dependency build ./charts/argus
helm install argus ./charts/argus -n monitoring -f my-values.yaml
```

The forwarder needs a Kubernetes Secret containing the Discord webhook:

```bash
kubectl create secret generic argus-forwarder -n monitoring \
  --from-literal=DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

Point Alertmanager at `http://argus-forwarder.monitoring.svc/webhook`.

## Values

See [`values.yaml`](charts/argus/values.yaml). Key knobs:

| Value | Purpose |
|-------|---------|
| `forwarder.holmesUrl` | Holmes `/api/chat` base URL |
| `forwarder.investigateSeverities` | Comma list; others ignored |
| `forwarder.discordSecret` | Secret holding `DISCORD_WEBHOOK` |
| `holmes.additionalEnvVars.OPENAI_API_BASE` | LLM gateway URL |
| `holmes.modelList` | Models Holmes can use |

## Consumed by

[nixlab](https://github.com/olivecasazza/nixlab) via a Flux `HelmRelease`
pointing at this repo's `charts/argus` directory.
