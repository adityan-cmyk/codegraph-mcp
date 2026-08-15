"""Rate limiting + request size limits middleware.

P0: Gate sensitive API routes with rate limits.
P1: Add request size limits and per-endpoint throttling.
"""

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB max request body
_RATE_LIMIT_WINDOW = 60  # 60 second window
_RATE_LIMIT_DEFAULT = 100  # 100 requests per window per IP
_RATE_LIMIT_MUTATE = 20  # 20 mutation requests per window per IP
_RATE_LIMIT_INDEX = 5  # 5 index/admin requests per window per IP

_MUTATE_PATHS = {"/api/incidents", "/api/model/chat"}
_INDEX_PATHS = {"/api/index/repository", "/api/index/semantic/rebuild", "/api/index/replay", "/api/kb/sync"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._counts: dict[str, deque] = defaultdict(deque)

    def _check_rate(self, client_ip: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        window = self._counts[client_ip]
        while window and window[0] < now - _RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= limit:
            return False, limit - len(window)
        window.append(now)
        return True, limit - len(window)

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        if client_ip in ("testclient", "127.0.0.1") and settings.environment == "local":
            response = await call_next(request)
            return response

        if "content-length" in request.headers:
            try:
                cl = int(request.headers["content-length"])
                if cl > _MAX_BODY_BYTES:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                pass

        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        if any(path.startswith(p) for p in _INDEX_PATHS):
            allowed, remaining = self._check_rate(f"index:{client_ip}", _RATE_LIMIT_INDEX)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded for index/admin operations"},
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
                )
        elif any(path.startswith(p) for p in _MUTATE_PATHS):
            allowed, remaining = self._check_rate(f"mutate:{client_ip}", _RATE_LIMIT_MUTATE)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded for mutation operations"},
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
                )
        else:
            allowed, remaining = self._check_rate(f"default:{client_ip}", _RATE_LIMIT_DEFAULT)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
                )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
