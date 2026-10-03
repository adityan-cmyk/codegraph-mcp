"""Redis-backed query cache — tagged, compressed, generation-keyed.

Hot read-only tools (blast radius, symbol search, hotspots) recompute the
same answers between agent sessions. The cache is strictly an optimization:
any Redis failure degrades to a direct compute — it can never fail a request.

Design:
  - Keys embed the current graph generation: a rebuild invalidates every
    graph-derived answer with no explicit flush.
  - Tag index: every entry registers its tags (Redis SETs) so a data-domain
    change (git pull, semantic rebuild, feedback surge) can invalidate exactly
    the affected entries via invalidate_tags().
  - Compression: payloads are zlib-compressed (JSON of graph neighborhoods
    compresses ~5-8x) with a magic prefix, transparently decompressed on read.
  - TTL: caller-suggested, hard-capped at MAX_TTL (4h). Entries also register
    in a per-tag "expires" sorted set so the tag index self-cleans instead of
    leaking keys forever.
  - Metrics: hits/misses/errors/sizes exported to Prometheus for the admin
    dashboard; stats() returns the same numbers for the REST surface.
"""

import hashlib
import json
import logging
import threading
import zlib

logger = logging.getLogger(__name__)

_CLIENT = None
_CLIENT_LOCK = threading.Lock()
_ENABLED = True  # degrades to no-op automatically when Redis is unreachable

MAX_TTL = 4 * 3600  # hard cap — nothing lives longer than 4 hours
_PREFIX = "cg"
_MAGIC_COMPRESSED = b"Z1:"

# in-process stats (mirrored to prometheus lazily)
_STATS = {"hits": 0, "misses": 0, "errors": 0, "sets": 0, "tag_invalidations": 0, "bytes_stored": 0}
_STATS_LOCK = threading.Lock()


def _get_client():
    global _CLIENT
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


def _bump(stat: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[stat] += n
    try:
        from app.core.prom_metrics import (
            QUERY_CACHE_HITS, QUERY_CACHE_MISSES, QUERY_CACHE_ERRORS, QUERY_CACHE_BYTES,
        )

        if stat == "hits":
            QUERY_CACHE_HITS.inc()
        elif stat == "misses":
            QUERY_CACHE_MISSES.inc()
        elif stat == "errors":
            QUERY_CACHE_ERRORS.inc()
        QUERY_CACHE_BYTES.set(_STATS["bytes_stored"])
    except Exception:
        pass


def _current_gen() -> int:
    try:
        from app.rag.retrieval.graph import graph_index

        return getattr(graph_index._active(), "_gen", 0)
    except Exception:
        return 0


def _args_key(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _entry_key(tool: str, args: dict) -> str:
    return f"{_PREFIX}:gen{_current_gen()}:{tool}:{_args_key(args)}"


def _tag_key(tag: str) -> str:
    return f"{_PREFIX}:tag:{_current_gen()}:{tag}"


def cache_get(tool: str, args: dict) -> dict | None:
    client = _get_client()
    if client is None:
        return None
    key = _entry_key(tool, args)
    try:
        raw = client.get(key)
        if raw is None:
            _bump("misses")
            return None
        if raw.startswith(_MAGIC_COMPRESSED):
            raw = zlib.decompress(raw[len(_MAGIC_COMPRESSED):])
        _bump("hits")
        return json.loads(raw)
    except Exception:
        _bump("errors")
        return None


def cache_set(tool: str, args: dict, payload: dict, ttl: int = 300, tags: list[str] | None = None) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        ttl = max(1, min(ttl, MAX_TTL))
        key = _entry_key(tool, args)
        data = json.dumps(payload, default=str).encode()
        compressed = _MAGIC_COMPRESSED + zlib.compress(data, level=6)
        # store compressed only when it actually wins
        blob = compressed if len(compressed) < len(data) else data
        client.set(key, blob, ex=ttl)
        for tag in tags or []:
            client.sadd(_tag_key(tag), key)
            client.expire(_tag_key(tag), ttl)  # tag index dies with its entries
        with _STATS_LOCK:
            _STATS["sets"] += 1
            _STATS["bytes_stored"] += len(blob)
    except Exception:
        _bump("errors")


def invalidate_tags(*tags: str) -> int:
    """Delete every entry registered under any of the given tags, across ALL
    generations (a swap makes the old gen's tag index the stale one)."""
    client = _get_client()
    if client is None or not tags:
        return 0
    removed = 0
    try:
        for tag in tags:
            tag_keys = list(client.scan_iter(match=f"{_PREFIX}:tag:*:{tag}", count=200))
            for tag_key in tag_keys:
                members = client.smembers(tag_key)
                if members:
                    removed += client.delete(*members)
                client.delete(tag_key)
        with _STATS_LOCK:
            _STATS["tag_invalidations"] += removed
    except Exception:
        _bump("errors")
    return removed


def stats() -> dict:
    out = dict(_STATS)
    client = _get_client()
    if client is not None:
        try:
            out["redis_keys"] = client.dbsize()
            out["redis_info"] = {
                "used_memory_human": client.info("memory").get("used_memory_human"),
                "maxmemory_human": client.info("memory").get("maxmemory_human"),
            }
        except Exception:
            pass
    hits, misses = out.get("hits", 0), out.get("misses", 0)
    out["hit_rate"] = round(hits / (hits + misses), 4) if (hits + misses) else None
    out["max_ttl_seconds"] = MAX_TTL
    return out


def flush_all() -> int:
    """Drop every cache entry (keeps other Redis users' keys intact —
    only deletes keys with our prefix)."""
    client = _get_client()
    if client is None:
        return 0
    removed = 0
    try:
        for key in client.scan_iter(match=f"{_PREFIX}:*", count=500):
            removed += client.delete(key)
    except Exception:
        _bump("errors")
    return removed
