"""Argus forwarder — Alertmanager -> HolmesGPT -> Discord (multi-channel).

Phase 2: Routes triage to Discord channels by severity
  (critical/warning/info/deals) instead of a single firehose.

Phase 3: Resolves the alert's namespace to a GitHub repo via REPO_MAPPINGS
  and instructs Holmes to open a PR if it can identify a concrete fix.
  Holmes's github MCP addon (enabled in chart values) does the actual PR
  creation; this forwarder only injects the target repo into the prompt.

Phase 5: Asks Holmes to also assess alert tuning (firing frequency, false-
  positive pattern) and propose PrometheusRule edits if warranted. The
  detailed procedure lives in the mounted alert-tuning SKILL.md.

Phase 4 (HITL approve/disapprove via Discord buttons) is deferred until a
  Discord bot token is provisioned — see argus.nix comment.

Safety: the forwarder has NO cluster access. HolmesGPT owns the read-only
RBAC (view ClusterRole via argus's SA). This service only speaks HTTP to
Holmes and Discord.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("argus-forwarder")

HOLMES_URL = os.environ.get("HOLMES_URL", "http://argus-holmes:80")
HOLMES_MODEL = os.environ.get("HOLMES_MODEL", "auto")
HOLMES_TIMEOUT = int(os.environ.get("HOLMES_TIMEOUT_SEC", "300"))
INVESTIGATE_SEVERITIES = {
    s.strip().lower()
    for s in os.environ.get("INVESTIGATE_SEVERITIES", "critical,warning").split(",")
    if s.strip()
}
DEDUPE_TTL = int(os.environ.get("DEDUPE_TTL_SEC", "3600"))
DISCORD_MAX = 1900

# --- Phase 2: multi-channel routing --------------------------------------- #
# Severity → Discord webhook URL. Unmapped severities fall back to DEFAULT.
# All sourced from env (rendered from values.yaml discordChannels map).
_DEFAULT_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_DEFAULT", "")
CHANNEL_WEBHOOKS: dict[str, str] = {
    "critical": os.environ.get("DISCORD_WEBHOOK_CRITICAL", _DEFAULT_WEBHOOK),
    "warning": os.environ.get("DISCORD_WEBHOOK_WARNING", _DEFAULT_WEBHOOK),
    "info": os.environ.get("DISCORD_WEBHOOK_INFO", ""),
    "deals": os.environ.get("DISCORD_WEBHOOK_DEALS", ""),
}

# --- Phase 3: project-agnostic repo mapping ------------------------------- #
# JSON: {"namespace": "owner/repo", ..., "default": "owner/repo"}
# Rendered from values.yaml repoMappings. Holmes uses this to know where
# to open fix PRs.
REPO_MAPPINGS: dict[str, str] = json.loads(os.environ.get("REPO_MAPPINGS", "{}"))

_recent: dict[str, float] = {}


def _resolve_webhook(severity: str) -> str:
    """Pick the Discord webhook for a given alert severity."""
    return CHANNEL_WEBHOOKS.get(severity, _DEFAULT_WEBHOOK)


def _resolve_repo(namespace: str) -> str | None:
    """Map a k8s namespace to a GitHub repo (owner/repo)."""
    if not REPO_MAPPINGS:
        return None
    return REPO_MAPPINGS.get(namespace) or REPO_MAPPINGS.get("default")


# --------------------------------------------------------------------------- #
#  Dedupe
# --------------------------------------------------------------------------- #


def _is_duplicate(fingerprint: str) -> bool:
    now = time.time()
    for k in [k for k, v in _recent.items() if now - v > DEDUPE_TTL]:
        _recent.pop(k, None)
    if fingerprint in _recent:
        return True
    _recent[fingerprint] = now
    return False


def _fingerprint(alert: dict[str, Any]) -> str:
    if fp := alert.get("fingerprint"):
        return fp
    labels = alert.get("labels", {})
    return hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
#  Holmes interaction
# --------------------------------------------------------------------------- #


def _build_prompt(alert: dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    namespace = labels.get("namespace", "")
    repo = _resolve_repo(namespace)

    prompt = (
        "A Prometheus alert is firing in this Kubernetes cluster. "
        "Investigate the root cause using your tools (kubectl, logs, "
        "Prometheus metrics) and produce a terse triage.\n\n"
        f"Alertname: {labels.get('alertname', 'unknown')}\n"
        f"Severity:  {labels.get('severity', 'unknown')}\n"
        f"Namespace: {namespace or 'n/a'}\n"
        f"Started:   {alert.get('startsAt', 'n/a')}\n\n"
        f"Labels:\n{json.dumps(labels, indent=2)}\n\n"
        f"Annotations:\n{json.dumps(annotations, indent=2)}\n\n"
        "Output format (keep the triage under ~600 chars):\n"
        "- **Verdict**: real / cascade / transient -- one sentence.\n"
        "- **Root cause**: one or two sentences with concrete evidence "
        "(log lines, metric values).\n"
        "- **Action**: specific -- a command to run or the next diagnostic step.\n"
        "- **Confidence**: low / medium / high.\n"
    )

    # Phase 3: if we know which repo owns this namespace, tell Holmes to
    # open a PR for concrete fixes. Holmes's github MCP addon does the work.
    if repo:
        prompt += (
            f"\n**Proposed fix**: If you can identify a specific code or config "
            f"fix, open a pull request against `{repo}` using your GitHub tools. "
            f"Target the repo's default branch. Keep the change minimal and "
            f"focused. Add a clear PR description linking back to this alert. "
            f"Include the PR URL in your triage under **Proposed fix**. "
            f"If the fix is unclear or risky, skip this — do not open "
            f"speculative PRs.\n"
        )

    # Phase 5: alert-tuning assessment. Holmes queries Prometheus for firing
    # history and proposes tuning if the pattern warrants it.
    prompt += (
        "\n**Alert tuning**: Also assess whether this alert is well-tuned. "
        "Query Prometheus for its firing history over the last 7 days "
        '(use metric `ALERTS{alertname="<name>"}`). If it fires frequently '
        "with transient or cascade verdicts, propose a severity downgrade, "
        "threshold adjustment, or `for:` duration increase. If it rarely "
        "fires but represents real impact, consider a severity upgrade. "
        "State your tuning recommendation under **Tuning** (or 'no change')."
    )

    return prompt


def _extract_analysis(data: Any) -> str:
    """Holmes /api/chat response shape varies by version. Be defensive."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("response", "analysis", "answer", "content", "text", "output"):
            if val := data.get(key):
                return val if isinstance(val, str) else json.dumps(val, indent=2)
    return json.dumps(data, indent=2)


async def _investigate(session: ClientSession, alert: dict[str, Any]) -> str:
    try:
        resp = await session.post(
            f"{HOLMES_URL}/api/chat",
            json={"ask": _build_prompt(alert), "model": HOLMES_MODEL},
            timeout=ClientTimeout(total=HOLMES_TIMEOUT),
        )
        if resp.status >= 400:
            body = await resp.text()
            return f"_Holmes returned HTTP {resp.status}: {body[:300]}_"
        return _extract_analysis(await resp.json())
    except asyncio.TimeoutError:
        return f"_Holmes timed out after {HOLMES_TIMEOUT}s_"
    except Exception as exc:  # noqa: BLE001
        return f"_Holmes unreachable: {exc}_"


# --------------------------------------------------------------------------- #
#  Discord
# --------------------------------------------------------------------------- #


async def _post_discord(session: ClientSession, webhook: str, content: str) -> None:
    if not webhook:
        log.debug("no webhook configured for this channel, skipping post")
        return
    pos = 0
    while pos < len(content):
        chunk = content[pos : pos + DISCORD_MAX]
        pos += DISCORD_MAX
        for attempt in range(3):
            try:
                resp = await session.post(
                    webhook,
                    json={"content": chunk, "username": "argus"},
                    timeout=ClientTimeout(total=30),
                )
                if resp.status == 429:
                    retry = float(resp.headers.get("Retry-After", "5"))
                    await asyncio.sleep(retry)
                    continue
                if resp.status >= 400:
                    log.warning("discord %s: %s", resp.status, await resp.text())
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("discord post failed (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(2)


async def _triage(session: ClientSession, alert: dict[str, Any]) -> None:
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "unknown")
    severity = labels.get("severity", "unknown")
    fp = _fingerprint(alert)
    webhook = _resolve_webhook(severity)

    if _is_duplicate(fp):
        log.info("dedupe %s %s", alertname, fp)
        return

    log.info("triage start: %s severity=%s fp=%s", alertname, severity, fp)
    await _post_discord(
        session,
        webhook,
        f":mag: **Investigating** `{alertname}` ({severity}) `{fp[:12]}`",
    )

    analysis = await _investigate(session, alert)
    await _post_discord(
        session,
        webhook,
        f":white_check_mark: **Triage** `{alertname}`\n{analysis}",
    )
    log.info("triage done: %s (%d chars)", alertname, len(analysis))


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #


async def _handle_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad json")

    session = request.app["session"]
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        severity = alert.get("labels", {}).get("severity", "").lower()
        if severity not in INVESTIGATE_SEVERITIES:
            log.debug(
                "skip %s (severity=%s not in %s)",
                alert.get("labels", {}).get("alertname"),
                severity,
                INVESTIGATE_SEVERITIES,
            )
            continue
        asyncio.create_task(_triage(session, alert))

    return web.Response(text="queued")


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _main() -> None:
    if not _DEFAULT_WEBHOOK and not any(CHANNEL_WEBHOOKS.values()):
        raise SystemExit(
            "no Discord webhook configured — set DISCORD_WEBHOOK_DEFAULT "
            "or DISCORD_WEBHOOK_{CRITICAL,WARNING,INFO,DEALS}"
        )

    app = web.Application()
    app["session"] = ClientSession()
    app.router.add_post("/webhook", _handle_webhook)
    app.router.add_get("/healthz", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info(
        "listening :8080 holmes=%s model=%s severities=%s dedupe=%ds timeout=%ds "
        "channels=%s repos=%d",
        HOLMES_URL,
        HOLMES_MODEL,
        INVESTIGATE_SEVERITIES,
        DEDUPE_TTL,
        HOLMES_TIMEOUT,
        {k: bool(v) for k, v in CHANNEL_WEBHOOKS.items()},
        len(REPO_MAPPINGS),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await app["session"].close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(_main())
