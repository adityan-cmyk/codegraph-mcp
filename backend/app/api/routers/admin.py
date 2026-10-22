"""Admin control plane — the mutation surface behind the Grafana panel.

Everything visual lives in Grafana (Prometheus/Loki/Postgres datasources);
these endpoints are the small mutation set the panel's control card calls:
runtime rate-limit tuning and cache inspection/flush.

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


def _connect():
    """Postgres connection for the runtime-settings store."""
    import psycopg
    from psycopg.rows import dict_row

    from app.core.config import settings

    return psycopg.connect(settings.postgres_dsn, row_factory=dict_row, connect_timeout=3)


def _settings_store():
    """Tiny key/value store in Postgres for runtime-tunable settings."""
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


# --- Per-user keys -----------------------------------------------------------
# One credential per person (user_name + user_email attribution) instead of
# the shared bearer: per-user rate limits, feedback attribution, revocation
# without rotating everyone. The plaintext key is returned ONCE at creation.


@router.post("/users")
def create_user_key(request: Request, user_name: str = "", user_email: str = ""):
    _require_admin_token(request)
    from app.core.client_tokens import create_user_key as _create

    user_name = user_name.strip()
    user_email = user_email.strip()
    if not user_name or len(user_name) > 120:
        raise HTTPException(status_code=422, detail="user_name required (max 120 chars)")
    if "@" not in user_email or len(user_email) > 254:
        raise HTTPException(status_code=422, detail="user_email required (valid address)")
    row = _create(user_name, user_email)
    # row["token"] is the only time the plaintext exists outside the client.
    return {
        "created": True,
        "id": row["id"],
        "user_name": row["user_name"],
        "user_email": row["user_email"],
        "token_prefix": row["token_prefix"],
        "token": row["token"],
        "note": "store this key now — it is not retrievable later",
    }


@router.get("/users")
def list_user_keys(request: Request):
    _require_admin_token(request)
    from app.core.client_tokens import list_user_keys as _list

    return {"users": _list()}


@router.post("/users/{key_id}/revoke")
def revoke_user_key(request: Request, key_id: int):
    _require_admin_token(request)
    from app.core.client_tokens import revoke_user_key as _revoke

    if not _revoke(key_id):
        raise HTTPException(status_code=404, detail="no such key id")
    return {"revoked": True, "id": key_id}
