# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Helm chart (`charts/argus`) that deploys a HolmesGPT-based SRE
triage agent for Kubernetes. It receives Prometheus Alertmanager webhooks,
investigates via HolmesGPT (the `holmes` subchart, pointed at an
OpenAI-compatible LLM gateway), and posts triage summaries to Discord
and/or Slack — optionally proposing fixes as GitHub PRs that a human
approves from chat. There is no application source tree outside the
chart: the only first-party code is `charts/argus/files/forwarder.py`
(a single-file aiohttp + discord.py service, ~900 lines), templated
straight into a ConfigMap and run via the `python:3.13-slim` image
(see `templates/forwarder-configmap.yaml` / `forwarder-deployment.yaml`)
rather than built into its own container image.

Consumed downstream by [nixlab](https://github.com/olivecasazza/nixlab)
via a Flux `HelmRelease` pointing at this repo's `charts/argus` directory.

## Commands

```bash
# Pull the holmes subchart dependency (required before lint/template/install)
helm dependency build ./charts/argus

# Validate chart correctness
helm lint ./charts/argus

# Render templates locally (catch templating errors before install)
helm template argus ./charts/argus

# Install/upgrade against a live cluster
helm upgrade --install argus ./charts/argus -n monitoring -f my-values.yaml

# Syntax-check the forwarder in isolation (no test suite exists)
python3 -m py_compile charts/argus/files/forwarder.py
```

There is no test suite, linter config, or CI pipeline in this repo —
`helm lint` + `helm template` are the only correctness checks available
before a real cluster deploy.

## Architecture

```
                    ┌──> Discord webhook (per severity)
Alertmanager        │
     │              ├──> Slack chat.postMessage (per severity)
     ▼              │
argus-forwarder ────┼──> #changelog  (activity feed + change ledger)
     │              │
     │              └──> GitHub PR ──> approve in chat ──> merge ──> Flux
     ▼
  holmes  /api/chat        (investigation: kubectl, logs, Prometheus, MCP)
     ▲
     │
holmes-operator ──> ScheduledHealthCheck CRs (proactive cron investigations)
```

- **holmes** (subchart `robusta/holmes`, pinned via `Chart.lock`): owns the
  investigation. Read-only k8s RBAC (`view` ClusterRole via
  `templates/holmes-rbac.yaml`), pointed at the LLM gateway via
  `OPENAI_API_BASE` + `modelList`. The subchart's own ServiceAccount is
  disabled (`createServiceAccount: false`) because it hardcodes
  `automountServiceAccountToken: false` with no ClusterRoleBinding, which
  would silently force-disable every kubernetes toolset — Argus supplies
  its own SA instead (`customServiceAccountName: argus-holmes-sa`).
- **forwarder** (`files/forwarder.py`): webhook receiver, fingerprint
  dedupe, severity filter, calls Holmes, fans out to Discord + Slack, owns
  the HITL proposal store (SQLite, on an RWO PVC). **No cluster RBAC.**
- **holmes-operator** (subchart, opt-in via `holmes.operator.enabled`):
  reconciles `scheduledhealthchecks` / `healthchecks` /
  `triggeredhealthchecks` CRs (`holmesgpt.dev`) for proactive checks on a
  cron, in addition to reactive alert triage. Read-only; no remediation.
  Its only sinks are `slack` (bot token) and `pagerduty` — no Discord, no
  webhook — which is the gap this chart's forwarder fills for reactive
  triage.

### forwarder.py phases (read the module docstring first)

The file is organized as accreted phases, referenced by comment banners
(`# --- Phase N: ... ---`) rather than separate modules — grep for
`^# --- Phase` to orient:

1. Discord webhook posting, per-severity channel routing
   (`CHANNEL_WEBHOOKS`, `_resolve_webhook`).
2. Multi-channel severity routing (this doubles as phase 1's banner).
3. Namespace → GitHub repo mapping (`_resolve_repo`) so Holmes knows
   where to open fix PRs.
4. Human-in-the-loop: `discord.py` gateway Client for button
   interactions, running in the same event loop as the aiohttp webhook
   server; SQLite proposal store with audit trail
   (`_create_proposal`/`_set_proposal_status`); approve merges via GitHub
   API or marks `approved-pending-merge` when no token; REST fallback at
   `POST /proposals/{id}/{approve,reject}` mirrors the Discord buttons.
5. Alert-tuning assessment folded into every triage
   (`files/skills/alert-tuning/SKILL.md`, mounted into Holmes via
   `customSkillPaths`/`additionalVolumes` in `values.yaml` and built into
   a ConfigMap by `templates/holmes-skills-configmap.yaml`).

Everything degrades gracefully per-credential — read `_resolve_webhook`,
`_slack_channel`, and the top-of-file env var defaults before assuming a
sink is active; an empty/unset webhook or token means that sink silently
no-ops rather than erroring.

### Slack dual-emit

When `forwarder.slackSecret` is set, every Discord post is mirrored to
Slack via `chat.postMessage` (see `_post_slack`). Slack needs
`chat:write` (+ `chat:write.public` for channels the bot hasn't joined).
Channel IDs, not names, in `SLACK_CHANNEL_*`.

### Reactive vs proactive

| | Reactive triage | Proactive healthchecks |
|---|---|---|
| Trigger | Alertmanager webhook | cron in a `ScheduledHealthCheck` |
| Runs in | forwarder | holmes-operator |
| Output | Discord + Slack + changelog | CR `.status`, plus `slack`/`pagerduty` on failure in `alert` mode |
| Needs | `discordSecret` / `slackSecret` | `holmes.operator.enabled` + a CR |

## Values gotchas (`charts/argus/values.yaml`)

- **`forwarder.discordEnabled` is the master Discord switch, and it must
  be a literal boolean.** Setting `forwarder.discordSecret: {}` does
  *not* disable Discord — Helm deep-merges values, so an empty map merges
  into the non-empty chart default and the webhook env survives. Only an
  explicit `discordEnabled: false` reliably turns Discord off (needed for
  Slack-only installs).
- **The forwarder Deployment uses `strategy: Recreate`.** Its proposal
  store is an RWO PVC; the default `RollingUpdate` surges a second pod
  that can't Multi-Attach the volume, deadlocking the rollout. Keep
  `Recreate` as long as the PVC exists.
- `forwarder.investigateSeverities` filters which alert severities are
  investigated at all — others are dropped before reaching Holmes.
- `forwarder.repoMappings` maps k8s namespace → `owner/repo` for PR
  targets; `default` is the fallback for unmapped namespaces.
- Credential values (`discordSecret`, `discordBotToken`,
  `changelogWebhookSecret`, `githubToken`) are all `{name, key}`
  secretKeyRef pairs, never raw values in `values.yaml` — see
  `values.schema.json` for the exact contract.
