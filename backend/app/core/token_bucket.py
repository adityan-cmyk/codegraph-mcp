"""Token-bucket rate limiting.

Fixed windows burst at boundaries (60 calls at second 59 + 60 more at
second 61 ≈ 120 calls in ~2s). Sliding windows fix that but re-grant the
entire quota at once when the window drains. A token bucket does neither:

  - bucket holds up to `capacity` tokens
  - tokens refill continuously at `refill_per_sec`
  - each request consumes one; empty bucket = denied

Bursts are capped at capacity, sustained rate is the refill rate, and
Retry-After is exact: ceil(deficit / refill). O(1) memory per identity.
"""

import math
import threading
import time


class TokenBucket:
    __slots__ = ("capacity", "refill_per_sec", "tokens", "updated", "lock")

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.updated = now

    def take(self, n: int = 1) -> tuple[bool, float]:
        """Try to consume n tokens. Returns (allowed, retry_after_seconds)."""
        with self.lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True, 0.0
            deficit = n - self.tokens
            retry_after = math.ceil(deficit / self.refill_per_sec) if self.refill_per_sec > 0 else 60.0
            return False, max(1.0, float(retry_after))

    def peek_remaining(self) -> int:
        with self.lock:
            self._refill()
            return int(self.tokens)


_REGISTRY: dict[str, TokenBucket] = {}
_REGISTRY_LOCK = threading.Lock()
_SWEEP_INTERVAL = 600.0  # drop buckets idle for 10 minutes
_LAST_SWEEP = 0.0


def take_tokens(key: str, capacity: float, refill_per_sec: float, n: int = 1) -> tuple[bool, float]:
    """Consume from the named bucket, creating it full on first use."""
    global _LAST_SWEEP
    with _REGISTRY_LOCK:
        now = time.monotonic()
        if now - _LAST_SWEEP > _SWEEP_INTERVAL:
            stale = [k for k, b in _REGISTRY.items() if now - b.updated > _SWEEP_INTERVAL]
            for k in stale:
                del _REGISTRY[k]
            _LAST_SWEEP = now
        bucket = _REGISTRY.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity, refill_per_sec)
            _REGISTRY[key] = bucket
    return bucket.take(n)


def bucket_snapshot(key: str) -> dict | None:
    with _REGISTRY_LOCK:
        bucket = _REGISTRY.get(key)
        return {"remaining": bucket.peek_remaining()} if bucket else None
