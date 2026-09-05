"""Detailed SMTP email notifications for build lifecycle, nightly sync, and client activity.

Sends HTML emails with tables for readability in Gmail/clients.
"""

import logging
import smtplib
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)

_STYLE = """
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; color: #1a1a2e; margin: 0; padding: 20px; background: #f4f5f7; }
  .card { background: #ffffff; border-radius: 8px; padding: 24px; max-width: 680px; margin: 0 auto;
          box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
  h2 { margin: 0 0 20px 0; font-size: 18px; color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 8px; }
  .stats { display: table; width: 100%; border-spacing: 8px 0; margin: 0 -8px 20px -8px; }
  .stat { display: table-cell; width: 25%; background: #f8f9fb; border-radius: 8px; padding: 14px 10px;
          text-align: center; border-top: 3px solid #0f3460; }
  .stat .num { font-size: 22px; font-weight: 700; color: #0f3460; line-height: 1.2; }
  .stat .label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: #8b8fa3; margin-top: 4px; }
  .stat .trend { font-size: 11px; margin-top: 4px; }
  .trend-up { color: #059669; } .trend-down { color: #dc2626; } .trend-flat { color: #8b8fa3; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0 20px 0; }
  th { text-align: left; background: #0f3460; color: #ffffff; padding: 8px 12px; font-size: 13px; }
  td { padding: 8px 12px; border-bottom: 1px solid #e4e6eb; font-size: 13px; }
  tr:nth-child(even) td { background: #f8f9fa; }
  .section { margin: 24px 0 8px 0; font-size: 14px; font-weight: 600; color: #0f3460;
             text-transform: uppercase; letter-spacing: 0.5px; }
  code { font-family: 'Cascadia Code', Consolas, monospace; font-size: 12px;
         background: #eef1f6; padding: 2px 6px; border-radius: 4px; }
  .commit { font-family: Consolas, monospace; font-size: 12px; padding: 4px 0 4px 12px;
            border-left: 3px solid #cbd5e1; margin: 2px 0; color: #334155; }
  .footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid #e4e6eb;
            font-size: 11px; color: #8b8fa3; }
  .warn { background: #fff7ed; border-left: 4px solid #f59e0b; padding: 10px 14px;
          border-radius: 4px; margin: 12px 0; font-size: 13px; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px;
           font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .badge-nightly { background: #dbeafe; color: #1d4ed8; }
  .badge-manual { background: #f1f5f9; color: #475569; }
  .badge-auto { background: #fae8ff; color: #a21caf; }
  .ok { color: #059669; font-weight: 600; }
  .fail { color: #dc2626; font-weight: 600; }
</style>
"""


def _smtp_configured() -> bool:
    return bool(
        settings.smtp_host
        and settings.smtp_port
        and settings.smtp_user
        and settings.smtp_password
        and settings.smtp_to
    )


def _badge(trigger: str) -> str:
    cls = "badge-nightly" if "sync" in trigger else ("badge-auto" if "auto" in trigger else "badge-manual")
    return f"<span class='badge {cls}'>{trigger}</span>"


def _trend(current: float, previous: float | None, higher_is_good: bool = True) -> str:
    """Small trend indicator comparing to previous value."""
    if previous is None or previous == 0:
        return ""
    pct = (current - previous) / previous * 100
    if abs(pct) < 1:
        return f"<div class='trend trend-flat'>&#8212; flat</div>"
    arrow = "&#9650;" if pct > 0 else "&#9660;"
    cls = "trend-up" if (pct > 0) == higher_is_good else "trend-down"
    return f"<div class='trend {cls}'>{arrow} {abs(pct):.0f}%</div>"


def _stat_cards(cards: list[dict]) -> str:
    """Row of stat cards: [{'num': '9,009', 'label': 'nodes', 'trend_html': '...'}, ...]"""
    cells = "".join(
        f"<div class='stat'><div class='num'>{c['num']}</div>"
        f"<div class='label'>{c['label']}</div>{c.get('trend_html', '')}</div>"
        for c in cards
    )
    return f"<div class='stats'>{cells}</div>"


def _html(subject: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{_STYLE}</head>
<body><div class="card">
<h2>{subject}</h2>
{body_html}
<div class="footer">codegraph-mcp · automated notification</div>
</div></body></html>"""


def _kv_table(rows: list[tuple[str, str]]) -> str:
    """Two-column key-value table."""
    cells = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
    return f"<table><tr><th style='width:35%'>Field</th><th>Value</th></tr>{cells}</table>"


def send_email(subject: str, body_html: str, retries: int = 3, retry_delay: float = 30.0) -> bool:
    """Send an HTML email with retries for transient DNS/network failures."""
    if not _smtp_configured():
        logger.debug("SMTP not configured, skipping notification: %s", subject)
        return False

    for attempt in range(1, retries + 1):
        try:
            msg = MIMEText(_html(subject, body_html), "html")
            msg["Subject"] = subject
            msg["From"] = settings.smtp_from or settings.smtp_user
            msg["To"] = ", ".join(settings.smtp_to)

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
                if settings.smtp_use_tls:
                    server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)

            logger.info("Notification sent: %s", subject)
            return True
        except Exception as exc:
            if attempt < retries:
                logger.warning(
                    "Email attempt %d/%d failed (%s), retrying in %.0fs: %s",
                    attempt, retries, type(exc).__name__, retry_delay, subject,
                )
                import time
                time.sleep(retry_delay)
            else:
                logger.warning("Failed to send notification after %d attempts: %s", retries, subject, exc_info=True)
    return False


def _short(commit: str | None) -> str:
    return f"<code>{commit[:12]}</code>" if commit else "unknown"


def _fmt_duration(sec: float) -> str:
    if sec > 3600:
        return f"{sec / 3600:.1f} hours"
    if sec > 60:
        return f"{sec / 60:.0f} min"
    return f"{sec:.0f}s"


def _estimate_completion(total_symbols: int) -> str:
    """Estimate build duration from historical data stored in Redis."""
    try:
        import json
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        history = r.lrange("build:duration_history", -5, -1)
        if not history:
            return "unknown <i>(no history yet — will improve)</i>"
        rates = []
        for h in history:
            entry = json.loads(h)
            if entry.get("symbols", 0) > 0 and entry.get("duration_sec", 0) > 0:
                rates.append(entry["duration_sec"] / entry["symbols"])
        if not rates:
            return "unknown"
        est_sec = sum(rates) / len(rates) * total_symbols
        return f"~{_fmt_duration(est_sec)}"
    except Exception:
        return "unknown"


def _record_build_duration(total_symbols: int, duration_sec: float) -> None:
    try:
        import json
        import time
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        r.lpush("build:duration_history", json.dumps({
            "symbols": total_symbols,
            "duration_sec": duration_sec,
            "timestamp": time.time(),
        }))
        r.ltrim("build:duration_history", 0, 19)
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
    cards = [
        {"num": f"{total_symbols:,}", "label": "symbols to index"},
    ]
    if files_changed is not None:
        cards.append({"num": str(files_changed), "label": "files changed"})

    rows = [
        ("Build type", f"<b>{build_type}</b>"),
        ("Trigger", _badge(trigger)),
    ]
    if from_commit or to_commit:
        rows.append(("Commit range", f"{_short(from_commit)} &rarr; {_short(to_commit)}"))
    rows.append(("Estimated completion", _estimate_completion(total_symbols)))

    send_email(
        subject=f"[codegraph] Build started — {total_symbols:,} symbols ({trigger})",
        body_html=_stat_cards(cards) + _kv_table(rows),
    )


def _get_previous_build_stats() -> dict:
    """Fetch previous build stats from Redis for trend comparison."""
    try:
        import json
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        raw = r.get("build:last_stats")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_build_stats(stats: dict) -> None:
    try:
        import json
        import redis as redis_lib
        r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        r.set("build:last_stats", json.dumps(stats))
    except Exception:
        pass


def notify_build_completed(
    build_type: str,
    graph_nodes: int,
    graph_edges: int,
    semantic_documents: int,
    duration_sec: float | None = None,
    from_commit: str | None = None,
    to_commit: str | None = None,
    trigger: str = "manual",
    commit_messages: list[str] | None = None,
    changed_files: list[str] | None = None,
    high_impact_symbols: list[dict] | None = None,
    symbols_added: int | None = None,
    symbols_removed: int | None = None,
) -> None:
    prev = _get_previous_build_stats()

    # Stat cards row — the headline numbers
    cards = [
        {"num": f"{graph_nodes:,}", "label": "graph nodes",
         "trend_html": _trend(graph_nodes, prev.get("nodes"))},
        {"num": f"{graph_edges:,}", "label": "graph edges",
         "trend_html": _trend(graph_edges, prev.get("edges"))},
        {"num": f"{semantic_documents:,}", "label": "semantic docs",
         "trend_html": _trend(semantic_documents, prev.get("docs"))},
    ]
    if duration_sec is not None:
        cards.append({"num": _fmt_duration(duration_sec), "label": "duration",
                      "trend_html": _trend(duration_sec, prev.get("duration"), higher_is_good=False)})

    html_parts = [
        _stat_cards(cards),
        _kv_table([
            ("Build type", f"<b>{build_type}</b>"),
            ("Trigger", _badge(trigger)),
        ] + ([("Commit range", f"{_short(from_commit)} &rarr; {_short(to_commit)}")] if from_commit or to_commit else [])),
    ]

    # Commits merged
    if commit_messages:
        commits_html = "".join(f"<div class='commit'>{msg}</div>" for msg in commit_messages[:20])
        more = f"<div style='color:#8b8fa3;font-size:12px'>&hellip; and {len(commit_messages) - 20} more</div>" if len(commit_messages) > 20 else ""
        html_parts.append(f"<div class='section'>Commits merged ({len(commit_messages)})</div>{commits_html}{more}")

    # Files changed — grouped by directory
    if changed_files:
        by_dir: dict[str, int] = {}
        for f in changed_files:
            parts = f.split("/")
            d = "/".join(parts[:3]) if len(parts) > 3 else f
            by_dir[d] = by_dir.get(d, 0) + 1
        rows = "".join(
            f"<tr><td><code>{d}</code></td><td style='text-align:right'><b>{c}</b></td></tr>"
            for d, c in sorted(by_dir.items(), key=lambda x: -x[1])[:10]
        )
        html_parts.append(
            f"<div class='section'>Files changed — by area ({len(changed_files)} total)</div>"
            f"<table><tr><th>Directory</th><th style='text-align:right'>Files</th></tr>{rows}</table>"
        )

    # High-impact symbols
    if high_impact_symbols:
        rows = "".join(
            f"<tr><td style='text-align:right'><b>{s['callers']}</b></td><td><code>{s['symbol_id']}</code></td></tr>"
            for s in high_impact_symbols[:10]
        )
        html_parts.append(
            f"<div class='section'>High-impact symbols changed</div>"
            f"<table><tr><th style='width:90px'>Callers</th><th>Symbol</th></tr>{rows}</table>"
        )

    # Symbol delta
    if symbols_added is not None or symbols_removed is not None:
        html_parts.append(
            f"<div class='section'>Symbol changes</div>"
            f"<table>"
            f"<tr><td><b>Added</b></td><td><span class='ok'>+{symbols_added or 0}</span></td></tr>"
            f"<tr><td><b>Removed</b></td><td><span class='fail'>-{symbols_removed or 0}</span></td></tr>"
            f"</table>"
        )

    if duration_sec is not None:
        _record_build_duration(semantic_documents, duration_sec)

    _save_build_stats({
        "nodes": graph_nodes,
        "edges": graph_edges,
        "docs": semantic_documents,
        "duration": duration_sec,
    })

    send_email(
        subject=f"[codegraph] {build_type} build done — {graph_nodes:,} nodes, {len(changed_files or [])} files"
                + (f" ({_fmt_duration(duration_sec)})" if duration_sec else ""),
        body_html="".join(html_parts),
    )


def notify_build_failed(build_type: str, error: str, trigger: str = "manual") -> None:
    send_email(
        subject=f"[codegraph] Build FAILED — {build_type}",
        body_html=(
            _kv_table([
                ("Build type", f"<b>{build_type}</b>"),
                ("Trigger", trigger),
                ("Status", "<span class='fail'>FAILED</span>"),
            ])
            + f"<div class='section'>Error</div><div class='warn' style='font-family:Consolas,monospace'>{error[:2000]}</div>"
            + "<div class='footer'>Check logs: <code>docker logs oncall-backend --tail 100</code></div>"
        ),
    )


def notify_nightly_sync_failed(stage: str, error: str, detail: str = "") -> None:
    body = _kv_table([
        ("Failed stage", f"<span class='fail'>{stage}</span>"),
        ("Time", "last night"),
    ]) + f"<div class='section'>Error</div><div class='warn' style='font-family:Consolas,monospace'>{error[:1000]}</div>"
    if detail:
        body += f"<div class='section'>Detail</div><div style='font-family:Consolas,monospace;font-size:12px'>{detail[:2000]}</div>"
    body += "<div class='footer'>Check logs: <code>~/repos/on-call-assistance/logs/nightly-sync.log</code></div>"
    send_email(
        subject=f"[codegraph] Nightly sync FAILED — {stage}",
        body_html=body,
    )


def notify_new_client(client_ip: str, path: str, user_agent: str = "") -> None:
    send_email(
        subject=f"[codegraph] New MCP client: {client_ip}",
        body_html=(
            _kv_table([
                ("Client IP", f"<code>{client_ip}</code>"),
                ("Path", path),
                ("User-Agent", f"<code>{user_agent[:200]}</code>"),
            ])
            + "<div class='warn'>First time this IP has successfully authenticated. "
              "If you don't recognize it, rotate <code>MCP_AUTH_TOKEN</code> immediately.</div>"
        ),
    )
