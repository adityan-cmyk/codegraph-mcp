"""Per-user access keys — one credential per person instead of one shared bearer.

The shared MCP/API token treats 15 people as one identity: no per-user rate
limits, no attribution in feedback or usage data, and no revocation without
rotating everyone at once. Per-user keys fix all three. The shared token keeps
working (agents, scripts); these keys layer on top.

Storage: only the SHA-256 hash of the key is persisted. The plaintext key is
returned exactly once, at creation. user_name and user_email are stored as
attribution (who to ping, who to revoke).

Backends mirror the rest of the store family: in-memory for tests/local,
Postgres for real deploys (CLIENT_TOKENS_BACKEND).
"""

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_KEY_PREFIX = "cgu_"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryClientTokenRepository:
    """Process-local store for tests and local dev — resets on restart."""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._by_hash: dict[str, int] = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def create(self, user_name: str, user_email: str) -> dict:
        token = _KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            key_id = self._next_id
            self._next_id += 1
            row = {
                "id": key_id,
                "user_name": user_name,
                "user_email": user_email,
                "token_hash": _digest(token),
                "token_prefix": token[:12],
                "active": True,
                "created_at": _now_iso(),
                "last_used_at": None,
            }
            self._rows[key_id] = row
            self._by_hash[row["token_hash"]] = key_id
        return {**row, "token": token}

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in row.items() if k != "token_hash"}
                for row in sorted(self._rows.values(), key=lambda r: r["id"])
            ]

    def revoke(self, key_id: int) -> bool:
        with self._lock:
            row = self._rows.get(key_id)
            if not row:
                return False
            row["active"] = False
            return True

    def resolve(self, token: str) -> dict | None:
        if not token:
            return None
        digest = _digest(token)
        with self._lock:
            key_id = self._by_hash.get(digest)
            row = self._rows.get(key_id) if key_id else None
            if not row or not row["active"]:
                return None
            if not hmac.compare_digest(row["token_hash"], digest):
                return None
            row["last_used_at"] = _now_iso()
            return {"id": row["id"], "user_name": row["user_name"], "user_email": row["user_email"]}


class PostgresClientTokenRepository:
    """Persistent store — same shape as the in-memory one."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._schema_ready = False

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=3)

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS client_tokens (
                        id          SERIAL PRIMARY KEY,
                        user_name   TEXT NOT NULL,
                        user_email  TEXT NOT NULL,
                        token_hash  TEXT NOT NULL UNIQUE,
                        token_prefix TEXT NOT NULL,
                        active      BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_used_at TIMESTAMPTZ
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS client_tokens_email_idx ON client_tokens (user_email)")
        self._schema_ready = True

    def create(self, user_name: str, user_email: str) -> dict:
        self._ensure_schema()
        token = _KEY_PREFIX + secrets.token_urlsafe(32)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO client_tokens (user_name, user_email, token_hash, token_prefix)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (user_name, user_email, _digest(token), token[:12]),
                )
                row = cur.fetchone()
        return {
            "id": row["id"],
            "user_name": user_name,
            "user_email": user_email,
            "token_prefix": token[:12],
            "token": token,
            "active": True,
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
            "last_used_at": None,
        }

    def list(self) -> list[dict]:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_name, user_email, token_prefix, active, created_at, last_used_at
                    FROM client_tokens ORDER BY id
                    """
                )
                rows = cur.fetchall()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            for field in ("created_at", "last_used_at"):
                if item.get(field) is not None and hasattr(item[field], "isoformat"):
                    item[field] = item[field].isoformat()
            out.append(item)
        return out

    def revoke(self, key_id: int) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE client_tokens SET active = FALSE WHERE id = %s", (key_id,))
                return cur.rowcount > 0

    def resolve(self, token: str) -> dict | None:
        if not token:
            return None
        digest = _digest(token)
        try:
            self._ensure_schema()
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, user_name, user_email, token_hash, active
                        FROM client_tokens WHERE token_hash = %s
                        """,
                        (digest,),
                    )
                    row = cur.fetchone()
                    if not row or not row["active"] or not hmac.compare_digest(row["token_hash"], digest):
                        return None
                    cur.execute("UPDATE client_tokens SET last_used_at = NOW() WHERE id = %s", (row["id"],))
                return {"id": row["id"], "user_name": row["user_name"], "user_email": row["user_email"]}
        except Exception:
            # Fail closed: a token store outage must not authenticate anyone.
            logger.warning("client token lookup failed — rejecting token", exc_info=True)
            return None


_memory_repo: InMemoryClientTokenRepository | None = None


def get_client_token_repository():
    global _memory_repo
    from app.core.config import settings

    if settings.client_tokens_backend == "postgres":
        return PostgresClientTokenRepository(settings.postgres_dsn)
    if _memory_repo is None:
        _memory_repo = InMemoryClientTokenRepository()
    return _memory_repo


def create_user_key(user_name: str, user_email: str) -> dict:
    return get_client_token_repository().create(user_name.strip(), user_email.strip())


def list_user_keys() -> list[dict]:
    return get_client_token_repository().list()


def revoke_user_key(key_id: int) -> bool:
    return get_client_token_repository().revoke(key_id)


def resolve_user_token(token: str) -> dict | None:
    """Authenticate a per-user key. Returns {id, user_name, user_email} or None.

    Unknown, revoked, or malformed keys resolve to None — fail closed. A tiny
    negative cache absorbs brute-force probes against the hash lookup without
    hammering Postgres.
    """
    if not token or len(token) < 8 or len(token) > 256:
        return None
    probe = _digest(token)
    now = time.monotonic()
    with _neg_cache_lock:
        expiry = _neg_cache.get(probe)
        if expiry and now < expiry:
            return None
    user = get_client_token_repository().resolve(token)
    if user is None:
        with _neg_cache_lock:
            _neg_cache[probe] = now + 60.0
            if len(_neg_cache) > 4096:
                stale = [k for k, exp in _neg_cache.items() if exp <= now]
                for k in stale:
                    _neg_cache.pop(k, None)
    return user


_neg_cache: dict[str, float] = {}
_neg_cache_lock = threading.Lock()
