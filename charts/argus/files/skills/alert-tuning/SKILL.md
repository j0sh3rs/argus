---
name: alert-tuning
description: >
  Use after investigating a Prometheus alert, when assessing whether the
  alert is well-tuned. Queries Prometheus firing history, classifies the
  noise pattern, and proposes a concrete PrometheusRule edit (severity
  bump, threshold change, `for:` duration, or silence). The proposed
  change lands as a PR via the github toolset against the repo mapped
  for the alert's namespace.
---

# Alert tuning

After triaging an alert, assess whether the alert rule itself is
well-calibrated. Alerting is a tuning process: rules can be too noisy
(firing on transient/cascade events that don't need a human), too quiet
(masking real incidents), or stale (the underlying condition changed
but the rule didn't).

## Goal

Produce a **concrete tuning recommendation** — either "no change" or a
specific PrometheusRule edit — based on firing history and triage
verdict pattern.

## Workflow

1. **Get the alert name and rule file**

   From the alert payload, capture `labels.alertname`. Find the
   PrometheusRule that defines it:

   ```bash
   kubectl get prometheusrule -A -o json | jq -r '.items[] | select(.spec.groups[].rules[]?.alert=="<ALERTNAME>") | "\(.metadata.namespace)/\(.metadata.name)"'
   ```

2. **Query 7-day firing history**

   ```promql
   # Firing count over 7d (resolution 1h)
   sum(increase(ALERTS_FOR_STATE{alertname="<ALERTNAME>",alertstate="firing"}[7d]))

   # Firing instances right now
   ALERTS{alertname="<ALERTNAME>",alertstate="firing"}

   # Time-series of firing state (last 7d, 1h step)
   sum_over_time(ALERTS{alertname="<ALERTNAME>",alertstate="firing"}[7d:1h])
   ```

3. **Classify the pattern**

   Combine the firing history with the verdict you just produced:

   | Pattern | Signal | Tuning direction |
   |---|---|---|
   | High firing count, verdicts mostly `transient` | Noisy — alerts that self-heal | Increase `for:` duration, downgrade severity, or silence |
   | High firing count, verdicts mostly `cascade` | Symptom of another alert | Add Alertmanager `inhibit_rule`, or silence in favor of the parent |
   | Low firing count, verdicts `real`, high impact | Properly tuned or under-tuned | Keep or upgrade severity |
   | Zero firings in 7d, rule looks stale | Possibly dead | Verify the metric still exists; consider removal |

4. **Propose the edit (or "no change")**

   If tuning is warranted, produce the minimal diff:

   - **Severity bump**: change `labels.severity` in the rule.
   - **Threshold**: change the `expr` threshold (e.g. `> 0.9` → `> 0.95`).
   - **`for:` duration**: change `for: 1m` → `for: 10m` to ride out transients.
   - **Silence**: propose an Alertmanager `silence` (time-boxed) if the
     alert is firing on a known condition that will be resolved by a
     planned change.
   - **Inhibit**: propose an Alertmanager `inhibit_rule` if this alert
     is consistently downstream of another.

5. **Open a PR (when repo is mapped)**

   If the alert's namespace maps to a repo (check the `REPO_MAPPINGS`
   env the forwarder passes via the prompt — it appears as "open a pull
   request against `<owner>/<repo>`"), use the github toolset to:

   - Create a branch `argus/alert-tuning/<alertname>-<short-reason>`
   - Edit the PrometheusRule YAML (find it under `modules/k8s/monitoring/`
     for nixlab, or wherever the repo keeps rules)
   - Open a PR with:
     - Title: `tune(<alertname>): <one-line reason>`
     - Body: firing count, verdict pattern, proposed change, expected effect
   - Return the PR URL in your triage under **Tuning**.

   If no repo is mapped, skip the PR and just state the recommendation.

## Guardrails

- **Never delete a rule.** Propose severity downgrade or silence, not
  removal — removal is a human decision.
- **Never lower severity below `info`** without explicit human approval.
- **One PR per alert.** Don't batch tuning changes; each alert's tuning
  is independently reviewable.
- **Cite the data.** Every tuning recommendation must reference the
  firing count and verdict pattern that justified it.
- **Prefer `for:` duration over threshold changes** when the issue is
  transient spikes — it's the least-disruptive knob.
