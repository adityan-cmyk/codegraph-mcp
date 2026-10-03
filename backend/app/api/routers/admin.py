"""Admin control plane — the mutation surface behind the Grafana panel.

Everything visual lives in Grafana (Prometheus/Loki/Postgres datasources);
these endpoints are the small mutation set the panel's control card calls:
runtime rate-limit tuning, cache inspection/flush, and UAM passthrough.

Auth: the ADMIN_PANEL_TOKEN bearer (constant-time compare). This token is
SEPARATE from the MCP token, the REST API token, and Grafana's own admin
password — one leaked credential must never unlock the other surfaces.
Login brute-force is bucket-limited per IP.
"""

import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.token_bucket import take_tokens

router = APIRouter(prefix="/api/admin", tags=["admin"])

_SETTING_BUCKETS = ["mcp_capacity", "mcp_refill"]  # known tunables


def _require_admin_token(request: Request) -> None:
    token = getattr(settings, "admin_panel_token", "") or ""
    if not token:
        raise HTTPException(status_code=503, detail="admin panel token not configured")
    provided = request.headers.get("authorization", "")
    key = request.headers.get("x-admin-key", "")
    ok = False
    if provided.startswith("Bearer "):
        ok = hmac.compare_digest(provided[7:], token)
    elif key:
        ok = hmac.compare_digest(key, token)
    # brute-force bucket: 10 attempts per 10 minutes per IP
    ip = request.client.host if request.client else "unknown"
    allowed, retry = take_tokens(f"admin-login:{ip}", 10, 10 / 600)
    if not allowed:
        raise HTTPException(status_code=429, detail="too many admin auth attempts",
                            headers={"Retry-After": str(int(retry))})
    if not ok:
        raise HTTPException(status_code=401, detail="admin token required")


def _settings_store():
    """Tiny key/value store in Postgres for runtime-tunable settings."""
    from app.core.uam import _connect

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    return _connect()


def get_setting(key: str, default):
    try:
        with _settings_store() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM admin_settings WHERE key = %s", (key,))
                row = cur.fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key: str, value) -> None:
    with _settings_store() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, json.dumps(value)),
            )


@router.get("/rate-limit")
def get_rate_limit(request: Request):
    _require_admin_token(request)
    from app.mcp.readonly_server import BearerTokenAuthMiddleware as _m

    return {
        "mcp_capacity": get_setting("mcp_capacity", _m._TOOL_CAPACITY),
        "mcp_refill_per_sec": get_setting("mcp_refill", _m._TOOL_REFILL_PER_SEC),
        "note": "applies to new token buckets; existing buckets keep their shape until swept (10 min idle)",
    }


@router.put("/rate-limit")
def set_rate_limit(request: Request, capacity: int = 60, refill_per_sec: float = 1.0):
    _require_admin_token(request)
    capacity = max(1, min(int(capacity), 1000))
    refill = max(0.1, min(float(refill_per_sec), 50.0))
    set_setting("mcp_capacity", capacity)
    set_setting("mcp_refill", refill)
    return {"updated": True, "mcp_capacity": capacity, "mcp_refill_per_sec": refill}


@router.get("/cache")
def cache_stats(request: Request):
    _require_admin_token(request)
    from app.core.query_cache import stats

    return stats()


@router.post("/cache/flush")
def cache_flush(request: Request, tag: str = ""):
    _require_admin_token(request)
    from app.core.query_cache import flush_all, invalidate_tags

    if tag:
        removed = invalidate_tags(tag)
    else:
        removed = flush_all()
    return {"removed": removed, "tag": tag or None}


@router.get("/usage")
def usage_summary(request: Request, days: int = 7):
    """Audit aggregates for the panel's usage tables (also queryable directly
    in Grafana via pg-oncall)."""
    _require_admin_token(request)
    from app.core.uam import _connect

    days = max(1, min(days, 90))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_name, count(*) FILTER (WHERE allowed) AS allowed,
                       count(*) FILTER (WHERE NOT allowed) AS denied
                FROM uam_audit WHERE created_at >= now() - (%s || ' days')::interval
                GROUP BY user_name ORDER BY allowed DESC LIMIT 50
                """,
                (str(days),),
            )
            by_user = cur.fetchall()
            cur.execute(
                """
                SELECT action AS tool, count(*) AS calls,
                       count(*) FILTER (WHERE NOT allowed) AS denied
                FROM uam_audit WHERE created_at >= now() - (%s || ' days')::interval
                GROUP BY action ORDER BY calls DESC LIMIT 50
                """,
                (str(days),),
            )
            by_tool = cur.fetchall()
            cur.execute(
                """
                SELECT user_name, action, reason, created_at
                FROM uam_audit WHERE NOT allowed
                ORDER BY created_at DESC LIMIT 25
                """,
            )
            recent_denied = cur.fetchall()
    return {
        "days": days,
        "by_user": by_user,
        "by_tool": by_tool,
        "recent_denials": [
            {"user": r["user_name"], "tool": r["action"], "reason": r["reason"],
             "at": str(r["created_at"])} for r in recent_denied
        ],
    }
