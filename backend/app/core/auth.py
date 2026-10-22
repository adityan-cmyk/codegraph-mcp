"""Simple bearer token auth middleware for API routes."""

import hmac
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)

_PUBLIC_PATHS = {"/health", "/", "/docs", "/openapi.json", "/redoc"}
_PUBLIC_PREFIXES = ("/assets/",)
# the admin router enforces its own scoped ADMIN_PANEL_TOKEN (with a
# brute-force bucket) — the outer API_AUTH_TOKEN gate must not shadow it
_SELF_AUTHED_PREFIXES = ("/api/admin/",)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = settings.api_auth_token or ""
        if not token:
            # Fail closed outside local: a misconfigured .env must not silently
            # expose the whole API surface.
            if settings.environment == "local":
                return await call_next(request)
            logger.error("API_AUTH_TOKEN unset in environment=%s — refusing (fail-closed)", settings.environment)
            return JSONResponse(status_code=503, content={"detail": "API auth not configured"})

        path = request.url.path

        if path in _PUBLIC_PATHS:
            return await call_next(request)

        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        if path.startswith(_SELF_AUTHED_PREFIXES):
            return await call_next(request)

        if path.startswith("/api/"):
            auth_header = request.headers.get("Authorization", "")
            provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
            if provided and hmac.compare_digest(provided, token):
                return await call_next(request)
            logger.warning("Unauthorized API access attempt: %s %s", request.method, path)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        return await call_next(request)
