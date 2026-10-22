"""Rate limiting + request size limits middleware.

P0: Gate sensitive API routes with rate limits.
P1: Add request size limits and per-endpoint throttling.
"""

import logging
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10MB max request body
_RATE_LIMIT_WINDOW = 60  # 60 second window
_RATE_LIMIT_DEFAULT = 100  # 100 requests per window per IP
_RATE_LIMIT_MUTATE = 20  # 20 mutation requests per window per IP
_RATE_LIMIT_INDEX = 5  # 5 index/admin requests per window per IP

_MUTATE_PATHS = {"/api/feedback", "/api/index/ingest", "/api/index/digest", "/api/index/notify"}
_INDEX_PATHS = {"/api/index/repository", "/api/index/semantic/rebuild", "/api/index/replay"}


class BodySizeLimitMiddleware:
    """Pure-ASGI 10MB body cap for ALL framings.

    The old check only read Content-Length, so chunked transfer-encoding
    bodies skipped the cap entirely. Buffer the request stream (bounded by
    the cap + one chunk) and 413 past it regardless of framing.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > _MAX_BODY_BYTES:
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"detail": "Request body too large"}',
                })
                return

        async def replay_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


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
                logger.debug("Unparsable Content-Length header", exc_info=True)

        path = request.url.path

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
