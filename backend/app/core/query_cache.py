"""Redis-backed query cache, keyed by graph generation.

Hot read-only tools (blast radius, symbol search, hotspots) recompute the
same answers between agent sessions. The cache is strictly an optimization:
any Redis failure degrades to a direct compute — it can never fail a request.

Keys embed the current graph generation, so a rebuild invalidates every
cached answer without any explicit flush.
"""

import hashlib
import json
import logging
import threading

logger = logging.getLogger(__name__)

_CLIENT = None
_CLIENT_LOCK = threading.Lock()
_ENABLED = True  # degrades to no-op automatically when Redis is unreachable


def _get_client():
    global _CLIENT, _ENABLED
    if not _ENABLED:
        return None
    with _CLIENT_LOCK:
        if _CLIENT is None:
            try:
                import redis  # type: ignore

                from app.core.config import settings

                _CLIENT = redis.Redis.from_url(
                    settings.redis_url, socket_timeout=2, socket_connect_timeout=2
                )
                _CLIENT.ping()
            except Exception:
                _CLIENT = None  # Redis down/absent — cache simply stays off
    return _CLIENT


def cache_get(tool: str, args: dict) -> dict | None:
    client = _get_client()
    if client is None:
        return None
    try:
        from app.rag.retrieval.graph import graph_index

        gen = getattr(graph_index._active(), "_gen", 0)
        raw = client.get(f"cg:gen{gen}:{tool}:{_args_key(args)}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def cache_set(tool: str, args: dict, payload: dict, ttl: int = 300) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        from app.rag.retrieval.graph import graph_index

        gen = getattr(graph_index._active(), "_gen", 0)
        client.set(
            f"cg:gen{gen}:{tool}:{_args_key(args)}",
            json.dumps(payload, default=str),
            ex=ttl,
        )
    except Exception:
        pass


def _args_key(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
