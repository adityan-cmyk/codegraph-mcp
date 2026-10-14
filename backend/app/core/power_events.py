"""Power-event detection and recovery notification.

A machine that loses power can't send email while it's down — so the email
sends on RECOVERY: when the backend starts and the host booted recently
(uptime below the window), the shutdown was almost certainly a power loss or
crash reboot, not a routine deploy. The email carries the current index stats
so the reader knows what came back.

Dedup via boot_id: a file in logs/ records the last alerted boot, so deploys
and backend restarts within the same boot never re-send.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_POWER_EVENT_WINDOW_SEC = 30 * 60
_BOOT_MARKER_PATH = "logs/power_event_last_boot.txt"


def _host_uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except Exception:
        return -1.0


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _already_alerted_for_boot(boot_id: str) -> bool:
    try:
        with open(_BOOT_MARKER_PATH) as fh:
            return fh.read().strip() == boot_id
    except Exception:
        return False


def _mark_boot_alerted(boot_id: str) -> None:
    try:
        os.makedirs(os.path.dirname(_BOOT_MARKER_PATH), exist_ok=True)
        with open(_BOOT_MARKER_PATH, "w") as fh:
            fh.write(boot_id)
    except Exception:
        logger.debug("power-event marker write failed", exc_info=True)


def _newest_backup_age_hours() -> str:
    try:
        import time
        from pathlib import Path

        backups = sorted(
            (p for p in Path("/backups/postgres").glob("oncall-*.sql.gz") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            return "none found"
        age_h = (time.time() - backups[0].stat().st_mtime) / 3600
        return f"{backups[0].name} ({age_h:.1f}h old)"
    except Exception:
        return "unavailable (backups not mounted?)"


def _collect_recovery_stats() -> list[tuple[str, str]]:
    """(label, value) rows for the recovery email."""
    rows: list[tuple[str, str]] = []

    # backends reachable
    from app.core.config import settings

    try:
        import psycopg

        with psycopg.connect(settings.postgres_dsn, connect_timeout=3):
            rows.append(("postgres", "reachable"))
    except Exception as exc:
        rows.append(("postgres", f"UNREACHABLE: {type(exc).__name__}"))
    try:
        import neo4j as _n

        driver = _n.GraphDatabase.driver(settings.neo4j_uri)
        driver.verify_connectivity()
        driver.close()
        rows.append(("neo4j", "reachable"))
    except Exception as exc:
        rows.append(("neo4j", f"UNREACHABLE: {type(exc).__name__}"))
    try:
        import httpx

        r = httpx.get(f"{settings.weaviate_url}/v1/.well-known/ready", timeout=5)
        rows.append(("weaviate", "ready" if r.status_code == 200 else f"status {r.status_code}"))
    except Exception as exc:
        rows.append(("weaviate", f"UNREACHABLE: {type(exc).__name__}"))
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(settings.redis_url)
        r.ping()
        rows.append(("redis", "reachable"))
    except Exception as exc:
        rows.append(("redis", f"UNREACHABLE: {type(exc).__name__}"))

    # graph stats
    try:
        from app.rag.retrieval.graph import graph_index

        stats = graph_index.get_stats()
        rows.append(("graph nodes", f"{stats.get('graph_nodes', 0):,}"))
        rows.append(("graph edges", f"{stats.get('graph_edges', 0):,} "
                                    f"(scip: {stats.get('scip_edges', 0):,} / regex: {stats.get('regex_edges', 0):,})"))
    except Exception as exc:
        rows.append(("graph", f"stats unavailable: {type(exc).__name__}"))

    # semantic stats
    try:
        from app.core.index_store import index_metadata_store

        snapshot = index_metadata_store.load_snapshot()
        if snapshot:
            rows.append(("semantic documents", f"{len(snapshot.chunks):,}"))
            rows.append(("snapshot created", str(snapshot.created_at)))
            rows.append(("last indexed commit", (snapshot.last_indexed_commit or "")[:12] or "unknown"))
        else:
            rows.append(("semantic", "no snapshot loaded"))
    except Exception as exc:
        rows.append(("semantic", f"stats unavailable: {type(exc).__name__}"))

    # index freshness vs HEAD
    try:
        from app.rag.ingestion.git_ingestor import get_current_head, get_last_indexed_commit

        head = get_current_head()
        last = get_last_indexed_commit()
        rows.append(("index up-to-date", "yes" if head == last else f"NO — behind {str(head)[:12]}"))
    except Exception:
        pass

    rows.append(("newest backup", _newest_backup_age_hours()))
    return rows


def check_and_notify_power_event() -> None:
    """Send the recovery email if this boot looks like a power event.

    Called shortly after backend startup (delayed so backends settle).
    Idempotent per boot via the marker file; never raises into startup.
    """
    try:
        boot_id = _boot_id()
        uptime = _host_uptime_seconds()
        if uptime < 0 or uptime > _POWER_EVENT_WINDOW_SEC:
            return  # routine start on an already-running host
        if boot_id and _already_alerted_for_boot(boot_id):
            return

        _mark_boot_alerted(boot_id)
        rows = _collect_recovery_stats()

        from app.core.notifications import send_email

        html_rows = "".join(
            f"<tr><td style='padding:4px 12px'><b>{label}</b></td>"
            f"<td style='padding:4px 12px'>{value}</td></tr>"
            for label, value in rows
        )
        send_email(
            "[codegraph] Host power event — services recovered",
            f"<p>The host appears to have lost power and rebooted "
            f"(uptime at backend start: {uptime / 60:.0f} minutes). "
            f"All services came back up with the backend. Current state:</p>"
            f"<table style='border-collapse:collapse'>{html_rows}</table>"
            f"<p>If this reboot was not expected, check the machine — and note the "
            f"backup age above before trusting the newest data.</p>",
        )
        logger.info("Power-event recovery email sent (boot %s, uptime %.0fs)", boot_id, uptime)
    except Exception:
        logger.warning("power-event check failed", exc_info=True)


def schedule_power_event_check(delay_seconds: float = 45.0) -> None:
    """Run the check in the background after the stack settles."""

    def _run() -> None:
        import time

        time.sleep(delay_seconds)
        check_and_notify_power_event()

    threading.Thread(target=_run, daemon=True, name="power-event-check").start()
