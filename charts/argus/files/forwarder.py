"""Argus forwarder — Alertmanager -> HolmesGPT -> Discord.

Receives Alertmanager webhook POSTs, asks HolmesGPT (via its /api/chat
endpoint) to investigate each firing alert, and posts the triage summary
to a Discord webhook.

Why this exists: HolmesGPT has no Discord sink — Robusta deliberately gates
Slack/Teams behind their SaaS. This forwarder is the thin glue that wires
Holmes's investigation to Discord, replacing the 470-line custom tool-loop
in nixlab's old alert-responder (whose entire ALLOWED_PREFIXES / tool_shell
machinery was dead code — only the Paperclip delegation path was live).

Safety: the forwarder has NO cluster access. HolmesGPT owns the read-only
RBAC (view ClusterRole via its Helm subchart). This service only speaks
HTTP to Holmes and Discord.
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
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
INVESTIGATE_SEVERITIES = {
    s.strip().lower()
    for s in os.environ.get("INVESTIGATE_SEVERITIES", "critical,warning").split(",")
    if s.strip()
}
DEDUPE_TTL = int(os.environ.get("DEDUPE_TTL_SEC", "3600"))
DISCORD_MAX = 1900

_recent: dict[str, float] = {}


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
    return (
        "A Prometheus alert is firing in this Kubernetes cluster. "
        "Investigate the root cause using your tools (kubectl, logs, "
        "Prometheus metrics) and produce a terse triage.\n\n"
        f"Alertname: {labels.get('alertname', 'unknown')}\n"
        f"Severity:  {labels.get('severity', 'unknown')}\n"
        f"Started:   {alert.get('startsAt', 'n/a')}\n\n"
        f"Labels:\n{json.dumps(labels, indent=2)}\n\n"
        f"Annotations:\n{json.dumps(annotations, indent=2)}\n\n"
        "Output format (keep under ~600 chars):\n"
        "- **Verdict**: real / cascade / transient -- one sentence.\n"
        "- **Root cause**: one or two sentences with concrete evidence "
        "(log lines, metric values).\n"
        "- **Action**: specific -- a command to run or the next diagnostic step.\n"
        "- **Confidence**: low / medium / high."
    )


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


async def _post_discord(session: ClientSession, content: str) -> None:
    pos = 0
    while pos < len(content):
        chunk = content[pos : pos + DISCORD_MAX]
        pos += DISCORD_MAX
        for attempt in range(3):
            try:
                resp = await session.post(
                    DISCORD_WEBHOOK,
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

    if _is_duplicate(fp):
        log.info("dedupe %s %s", alertname, fp)
        return

    log.info("triage start: %s severity=%s fp=%s", alertname, severity, fp)
    await _post_discord(
        session,
        f":mag: **Investigating** `{alertname}` ({severity}) `{fp[:12]}`",
    )

    analysis = await _investigate(session, alert)
    await _post_discord(
        session,
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
    if not DISCORD_WEBHOOK:
        raise SystemExit("DISCORD_WEBHOOK not set")

    app = web.Application()
    app["session"] = ClientSession()
    app.router.add_post("/webhook", _handle_webhook)
    app.router.add_get("/healthz", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info(
        "listening :8080 holmes=%s model=%s severities=%s dedupe=%ds timeout=%ds",
        HOLMES_URL,
        HOLMES_MODEL,
        INVESTIGATE_SEVERITIES,
        DEDUPE_TTL,
        HOLMES_TIMEOUT,
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
