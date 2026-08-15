"""Simple bearer token auth middleware for API routes."""

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)

_PUBLIC_PATHS = {"/health", "/", "/docs", "/openapi.json", "/redoc"}
_PUBLIC_PREFIXES = ("/assets/",)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.api_auth_token:
            return await call_next(request)

        path = request.url.path

        if path in _PUBLIC_PATHS:
            return await call_next(request)

        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        if path.startswith("/api/"):
            auth_header = request.headers.get("Authorization", "")
            if auth_header == f"Bearer {settings.api_auth_token}":
                return await call_next(request)
            logger.warning("Unauthorized API access attempt: %s %s", request.method, path)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        return await call_next(request)
