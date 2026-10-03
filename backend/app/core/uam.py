"""UAM — User Action Model.

AWS-IAM-style access control for MCP tool calls, with paid-service tiers:

  key -> user -> roles -> policies (tool patterns)  +  tier -> daily quota

Every tool call is checked against the union of the user's role policies and
audited. Quotas count allowed calls per UTC day. The legacy MCP_AUTH_TOKEN
bootstraps as an enterprise admin, so existing agents keep working unchanged.

Resilience rule: if the UAM store is unreachable, the legacy token still
authenticates as admin — Postgres blips must never lock everyone out.
"""

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policies: role -> allowed tool patterns. "*" and "prefix_*" wildcards.
# Protocol-level methods are always allowed for authenticated users.
# ---------------------------------------------------------------------------

ROLE_POLICIES: dict[str, list[str]] = {
    "viewer": [
        "search_symbols", "search_symbols_enhanced", "get_blast_radius",
        "get_blast_radius_detailed", "batch_blast_radius", "get_symbol_content",
        "get_symbols_in_file", "get_graph_stats", "get_index_meta",
        "traverse_graph", "find_dependency_path", "get_tests_for_symbol",
        "recent_changes_near", "find_hotspots", "find_cycles", "find_dead_code",
        "resolve_stacktrace", "semantic_search",
    ],
    "reviewer": [
        "analyze_pr_diff", "find_warnings_in_blast_radius", "diff_modules",
        "submit_search_feedback", "submit_ai_feedback",
        "get_reinforcement_stats",
    ],
    "operator": [
        # expensive: model-inference decisions
        "make_decision",
    ],
    "admin": ["*"],
}

# What each tier may hold and spend per day. None = unlimited.
TIERS: dict[str, dict] = {
    "free": {"daily_calls": 200, "roles": {"viewer"}},
    "pro": {"daily_calls": 5000, "roles": {"viewer", "reviewer", "operator"}},
    "enterprise": {"daily_calls": None, "roles": {"viewer", "reviewer", "operator", "admin"}},
}

_ALWAYS_ALLOWED = {"tools/list", "tools/call", "ping", "prompts/list", "prompts/get",
                   "initialize", "notifications/initialized"}
_LEGACY_KEY_MARKER = "legacy-bootstrap-admin"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _pattern_match(pattern: str, action: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return action.startswith(pattern[:-1])
    return pattern == action


def _user_allowed_tools(roles: list[str]) -> list[str]:
    patterns: list[str] = []
    for role in roles:
        patterns.extend(ROLE_POLICIES.get(role, []))
    return patterns


class UAMUser:
    __slots__ = ("name", "key_hash", "roles", "tier", "active", "effective_roles")

    def __init__(self, name: str, key_hash: str, roles: list[str], tier: str, active: bool):
        self.name = name
        self.key_hash = key_hash
        self.roles = roles
        self.tier = tier
        self.active = active
        tier_roles = TIERS.get(tier, {}).get("roles", set())
        self.effective_roles = [r for r in roles if r in tier_roles] or ["viewer"]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_DSN = None
_CACHE: dict[str, tuple[float, UAMUser | None]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60.0
_BOOTSTRAP_DONE = threading.Event()


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    global _DSN
    if _DSN is None:
        from app.core.config import settings

        _DSN = settings.postgres_dsn
    return psycopg.connect(_DSN, row_factory=dict_row, connect_timeout=3)


def _ensure_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS uam_users (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    roles TEXT[] NOT NULL DEFAULT '{}',
                    tier TEXT NOT NULL DEFAULT 'free',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS uam_audit (
                    id BIGSERIAL PRIMARY KEY,
                    key_hash TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    allowed BOOLEAN NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS uam_audit_day ON uam_audit (key_hash, created_at)"
            )
            # Grafana's read-only datasource may read the audit trail (usage
            # panels) but NEVER uam_users — key hashes are server-side secrets.
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro'")
            if cur.fetchone():
                cur.execute("GRANT SELECT ON uam_audit TO grafana_ro")
                cur.execute("REVOKE ALL ON uam_users FROM grafana_ro")


def bootstrap() -> None:
    """Ensure the legacy MCP token exists as an enterprise admin (once)."""
    if _BOOTSTRAP_DONE.is_set():
        return
    try:
        _ensure_schema()
        from app.core.config import settings

        if settings.mcp_auth_token:
            key_hash = _hash_key(settings.mcp_auth_token)
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO uam_users (name, key_hash, roles, tier, active)
                        VALUES ('bootstrap-admin', %s, '{admin}', 'enterprise', TRUE)
                        ON CONFLICT (name) DO UPDATE
                            SET key_hash = EXCLUDED.key_hash,
                                roles = '{admin}', tier = 'enterprise', active = TRUE
                        """,
                        (key_hash,),
                    )
        _BOOTSTRAP_DONE.set()
        logger.info("UAM bootstrap complete")
    except Exception:
        logger.warning("UAM bootstrap deferred (store unreachable)", exc_info=True)


def lookup_user(api_key: str) -> UAMUser | None:
    """Resolve a bearer key to a user (60s cache). None = unknown key."""
    key_hash = _hash_key(api_key)
    now = datetime.now(UTC).timestamp()
    with _CACHE_LOCK:
        cached = _CACHE.get(key_hash)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
    try:
        bootstrap()
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT name, key_hash, roles, tier, active
                    FROM uam_users WHERE key_hash = %s
                    """,
                    (key_hash,),
                )
                row = cur.fetchone()
    except Exception:
        return None  # store unreachable — caller falls back to legacy auth
    user = UAMUser(row["name"], row["key_hash"], row["roles"], row["tier"], row["active"]) if row else None
    if user and not user.active:
        user = None  # revoked keys authenticate as nobody
    with _CACHE_LOCK:
        _CACHE[key_hash] = (now, user)
    return user


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Access decisions + audit
# ---------------------------------------------------------------------------

def check_access(user: UAMUser, action: str) -> tuple[bool, str]:
    """Policy decision for one action. Does not consume quota."""
    if action in _ALWAYS_ALLOWED:
        return True, "protocol"
    patterns = _user_allowed_tools(user.effective_roles)
    if any(_pattern_match(p, action) for p in patterns):
        return True, "policy"
    return False, f"no policy grants '{action}' to roles {user.effective_roles} (tier {user.tier})"


def quota_state(user: UAMUser) -> dict:
    """Remaining daily calls for the user's tier."""
    limit = TIERS.get(user.tier, {}).get("daily_calls")
    if limit is None:
        return {"limit": None, "used": None, "remaining": None, "unlimited": True}
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) AS n FROM uam_audit
                    WHERE key_hash = %s AND allowed = TRUE AND created_at >= current_date
                    """,
                    (user.key_hash,),
                )
                used = cur.fetchone()["n"]
    except Exception:
        return {"limit": limit, "used": 0, "remaining": limit, "unlimited": False, "degraded": True}
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "unlimited": False}


def audit(user: UAMUser | None, action: str, allowed: bool, reason: str) -> None:
    """Fire-and-forget audit write — must never fail a request."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO uam_audit (key_hash, user_name, action, allowed, reason)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (user.key_hash if user else "anonymous",
                     user.name if user else "anonymous",
                     action, allowed, reason),
                )
    except Exception:
        logger.debug("audit write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------

def create_user(name: str, tier: str, roles: list[str]) -> dict:
    tier_spec = TIERS.get(tier)
    if not tier_spec:
        raise ValueError(f"unknown tier: {tier} (one of {sorted(TIERS)})")
    invalid = [r for r in roles if r not in tier_spec["roles"]]
    if invalid:
        raise ValueError(f"tier '{tier}' cannot hold roles {invalid}")
    if not roles:
        roles = ["viewer"]
    api_key = f"cgm_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(api_key)
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO uam_users (name, key_hash, roles, tier)
                VALUES (%s, %s, %s, %s)
                """,
                (name, key_hash, roles, tier),
            )
    invalidate_cache()
    return {"name": name, "tier": tier, "roles": roles, "api_key": api_key,
            "note": "store this key now — it is not retrievable later"}


def list_users() -> list[dict]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, roles, tier, active, created_at
                FROM uam_users ORDER BY created_at
                """
            )
            return [
                {"name": r["name"], "roles": r["roles"], "tier": r["tier"],
                 "active": r["active"], "created_at": str(r["created_at"])}
                for r in cur.fetchall()
            ]


def update_user(name: str, *, tier: str | None = None, roles: list[str] | None = None,
                active: bool | None = None) -> dict:
    _ensure_schema()
    sets, args = [], []
    if tier is not None:
        if tier not in TIERS:
            raise ValueError(f"unknown tier: {tier}")
        sets.append("tier = %s")
        args.append(tier)
    if roles is not None:
        tier_final = tier
        if tier_final is None:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT tier FROM uam_users WHERE name = %s", (name,))
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"no such user: {name}")
                    tier_final = row["tier"]
        allowed_roles = TIERS[tier_final]["roles"]
        invalid = [r for r in roles if r not in allowed_roles]
        if invalid:
            raise ValueError(f"tier '{tier_final}' cannot hold roles {invalid}")
        sets.append("roles = %s")
        args.append(roles)
    if active is not None:
        sets.append("active = %s")
        args.append(active)
    if not sets:
        raise ValueError("nothing to update")
    args.append(name)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE uam_users SET {', '.join(sets)} WHERE name = %s", args)
            if cur.rowcount == 0:
                raise ValueError(f"no such user: {name}")
    invalidate_cache()
    return {"name": name, "updated": True}


def verify_legacy_key(api_key: str) -> bool:
    """Fallback auth when the store is unreachable."""
    from app.core.config import settings

    return bool(settings.mcp_auth_token) and hmac.compare_digest(api_key, settings.mcp_auth_token)
