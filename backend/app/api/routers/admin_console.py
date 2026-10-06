"""Standalone admin console — served by the backend on its own origin.

Replaces the Grafana HTML control card: the old approach required
GF_PANELS_DISABLE_SANITIZE_HTML=true, which weakened XSS protection for every
dashboard. This page needs no Grafana exceptions — the token is held in
browser memory for the tab's lifetime only, and every mutation still goes
through the admin-token-checked /api/admin/* endpoints.

Reachable only inside the tailnet (the funnel exposes 8002, not 8000).
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pathlib import Path

router = APIRouter()

_PAGE = (Path(__file__).parent / "admin_console.html").read_text()


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_console() -> str:
    return _PAGE
