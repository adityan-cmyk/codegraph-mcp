"""Detailed SMTP email notifications for build lifecycle, nightly sync, and client activity."""

import logging
import smtplib
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    return bool(
        settings.smtp_host
        and settings.smtp_port
        and settings.smtp_user
        and settings.smtp_password
        and settings.smtp_to
    )


def send_email(subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True if sent, False otherwise."""
    if not _smtp_configured():
        logger.debug("SMTP not configured, skipping notification: %s", subject)
        return False

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = ", ".join(settings.smtp_to)

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)

        logger.info("Notification sent: %s", subject)
        return True
    except Exception:
        logger.warning("Failed to send notification: %s", subject, exc_info=True)
        return False


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def _estimate_completion(total_symbols: int) -> str:
    """Estimate build duration from historical data stored in Redis."""
    try:
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        history = r.lrange("build:duration_history", -5, -1)  # last 5 builds
        if not history:
            return "unknown (no history yet)"
        import json
        rates = []
        for h in history:
            entry = json.loads(h)
            if entry.get("symbols", 0) > 0 and entry.get("duration_sec", 0) > 0:
                rates.append(entry["duration_sec"] / entry["symbols"])
        if not rates:
            return "unknown"
        avg_rate = sum(rates) / len(rates)
        est_sec = avg_rate * total_symbols
        if est_sec > 3600:
            return f"~{est_sec / 3600:.1f} hours"
        return f"~{est_sec / 60:.0f} minutes"
    except Exception:
        return "unknown"


def _record_build_duration(total_symbols: int, duration_sec: float) -> None:
    """Store build duration for future estimates."""
    try:
        import json
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        import time
        r.lpush("build:duration_history", json.dumps({
            "symbols": total_symbols,
            "duration_sec": duration_sec,
            "timestamp": time.time(),
        }))
        r.ltrim("build:duration_history", 0, 19)  # keep last 20
    except Exception:
        logger.debug("Failed to record build duration", exc_info=True)


def notify_build_started(
    build_type: str,
    total_symbols: int,
    from_commit: str | None = None,
    to_commit: str | None = None,
    files_changed: int | None = None,
    trigger: str = "manual",
) -> None:
    lines = [
        f"Build type: {build_type}",
        f"Trigger: {trigger}",
        f"Symbols to index: {total_symbols:,}",
    ]
    if from_commit or to_commit:
        lines.append(f"Commit range: {_short(from_commit)} → {_short(to_commit)}")
    if files_changed is not None:
        lines.append(f"Files changed: {files_changed}")
    lines.append(f"Estimated completion: {_estimate_completion(total_symbols)}")
    send_email(
        subject=f"[codegraph] Build started — {total_symbols:,} symbols ({trigger})",
        body="\n".join(lines),
    )


def notify_build_completed(
    build_type: str,
    graph_nodes: int,
    graph_edges: int,
    semantic_documents: int,
    duration_sec: float | None = None,
    from_commit: str | None = None,
    to_commit: str | None = None,
    trigger: str = "manual",
) -> None:
    lines = [
        f"Build type: {build_type}",
        f"Trigger: {trigger}",
        f"Graph nodes: {graph_nodes:,}",
        f"Graph edges: {graph_edges:,}",
        f"Semantic documents: {semantic_documents:,}",
    ]
    if from_commit or to_commit:
        lines.append(f"Commit range: {_short(from_commit)} → {_short(to_commit)}")
    if duration_sec is not None:
        if duration_sec > 3600:
            lines.append(f"Duration: {duration_sec:.0f}s ({duration_sec / 3600:.1f} hours)")
        else:
            lines.append(f"Duration: {duration_sec:.0f}s ({duration_sec / 60:.1f} min)")
        _record_build_duration(semantic_documents, duration_sec)
    send_email(
        subject=f"[codegraph] Build completed — {graph_nodes:,} nodes, {graph_edges:,} edges ({duration_sec and f'{duration_sec / 60:.0f}m' or 'unknown'})",
        body="\n".join(lines),
    )


def notify_build_failed(build_type: str, error: str, trigger: str = "manual") -> None:
    send_email(
        subject=f"[codegraph] Build FAILED — {build_type}",
        body="\n".join([
            f"Build type: {build_type}",
            f"Trigger: {trigger}",
            f"Error: {error[:2000]}",
            "",
            "Check backend logs: docker logs oncall-backend --tail 100",
        ]),
    )


def notify_nightly_sync_failed(stage: str, error: str, detail: str = "") -> None:
    send_email(
        subject=f"[codegraph] Nightly sync FAILED — {stage}",
        body="\n".join([
            f"Failed stage: {stage}",
            f"Error: {error[:1000]}",
            detail[:2000] if detail else "",
            "",
            f"Host: {settings.readonly_mcp_host}",
            "Check logs: ~/repos/on-call-assistance/logs/nightly-sync.log",
        ]),
    )


def notify_new_client(client_ip: str, path: str, user_agent: str = "") -> None:
    send_email(
        subject=f"[codegraph] New MCP client: {client_ip}",
        body="\n".join([
            f"Client IP: {client_ip}",
            f"Path: {path}",
            f"User-Agent: {user_agent[:200]}",
            "",
            "This is the first time this IP has successfully authenticated.",
            "If you don't recognize this IP, rotate MCP_AUTH_TOKEN immediately.",
        ]),
    )
