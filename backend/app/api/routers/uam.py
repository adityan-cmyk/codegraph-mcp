"""UAM administration — users, roles, tiers, quotas, audit.

Guarded by the REST API token (admin-only surface). MCP keys themselves are
never accepted here: UAM manages the MCP layer, this manages UAM.
"""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/uam", tags=["uam"])


def _require_admin(request: Request) -> None:
    import hmac

    from app.core.config import settings

    # Either server-side admin credential works: the REST API token
    # (programmatic) or the panel token (Grafana control card). They are
    # separate secrets on purpose — one leak never unlocks both surfaces.
    tokens = [t for t in (settings.api_auth_token, settings.admin_panel_token) if t]
    if not tokens:
        raise HTTPException(status_code=503, detail="admin auth not configured")
    provided = request.headers.get("authorization", "")
    if not provided.startswith("Bearer ") or not any(
        hmac.compare_digest(provided[7:], t) for t in tokens
    ):
        raise HTTPException(status_code=401, detail="admin token required")


@router.get("/users")
def list_users(request: Request):
    _require_admin(request)
    from app.core import uam

    try:
        return {"users": uam.list_users()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"UAM store unavailable: {exc}")


@router.post("/users")
def create_user(request: Request, name: str, tier: str = "free", roles: str = "viewer"):
    _require_admin(request)
    from app.core import uam

    try:
        return uam.create_user(name=name, tier=tier, roles=[r.strip() for r in roles.split(",") if r.strip()])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"UAM store unavailable: {exc}")


@router.patch("/users/{name}")
def update_user(request: Request, name: str, tier: str | None = None,
                roles: str | None = None, active: bool | None = None):
    _require_admin(request)
    from app.core import uam

    role_list = [r.strip() for r in roles.split(",") if r.strip()] if roles is not None else None
    try:
        return uam.update_user(name, tier=tier, roles=role_list, active=active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"UAM store unavailable: {exc}")


@router.get("/usage")
def usage(request: Request, name: str):
    _require_admin(request)
    from app.core import uam

    try:
        uam.bootstrap()
        with uam._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key_hash, tier FROM uam_users WHERE name = %s", (name,))
                row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"no such user: {name}")
        user = uam.UAMUser(name, row["key_hash"], [], row["tier"], True)
        quota = uam.quota_state(user)
        return {"name": name, "tier": row["tier"], **quota}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"UAM store unavailable: {exc}")


@router.get("/tiers")
def tiers(request: Request):
    _require_admin(request)
    from app.core import uam

    return {
        "tiers": {
            name: {"daily_calls": spec["daily_calls"], "roles_allowed": sorted(spec["roles"])}
            for name, spec in uam.TIERS.items()
        },
        "role_policies": uam.ROLE_POLICIES,
    }
