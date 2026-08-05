"""Default-deny enforcement: no route ships unguarded by accident.

This walks the real mounted app and asserts every route either resolves
`get_access_claims` somewhere in its dependency tree, or is explicitly listed in
`PUBLIC_PATHS`. Adding an endpoint and forgetting the guard turns into a failing
test, not a silent hole in production.

The target is `get_access_claims` rather than `get_principal` because it is the
narrower of the two: `get_principal` depends on it, so every route that
authorizes also authenticates, while `/auth/logout` authenticates without
needing a `Principal` at all. Checking the outer one would miss that route.

Needs no database, no Redis, no network — it inspects the dependency graph
FastAPI builds at import time, so it runs in milliseconds and can gate CI.
"""

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import get_access_claims
from app.api.routes import PUBLIC_PATHS, ROUTERS
from app.main import create_app


def _resolves(dependant: Dependant, target) -> bool:
    """True if `target` appears anywhere in this route's dependency tree."""
    if dependant.call is target:
        return True
    return any(_resolves(sub, target) for sub in dependant.dependencies)


def _app_routes() -> list[APIRoute]:
    """Every APIRoute in the API, including app-level ones like /health.

    FastAPI mounts included routers lazily, so `app.routes` is not flat — it
    holds opaque include markers rather than the routes inside them. We read the
    routers directly instead. `api_router` is built from the same `ROUTERS`
    tuple, so this cannot drift from what is actually served, and it does not
    depend on FastAPI's private inclusion internals.

    A router's own `dependencies=[...]` are baked into each route at decoration
    time, so the guards are visible here.
    """
    routes = [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]
    routes += [r for r in create_app().routes if isinstance(r, APIRoute)]
    return routes


def test_every_route_is_guarded_or_explicitly_public() -> None:
    unguarded = sorted(
        f"{sorted(r.methods)} {r.path}"
        for r in _app_routes()
        if r.path not in PUBLIC_PATHS and not _resolves(r.dependant, get_access_claims)
    )
    assert not unguarded, (
        "These routes are neither authenticated nor listed in PUBLIC_PATHS:\n  "
        + "\n  ".join(unguarded)
        + "\n\nGuard the router with require(...) — see app/api/routes/admin.py — "
        "or add the path to PUBLIC_PATHS with a comment saying why."
    )


def test_public_paths_has_no_stale_entries() -> None:
    """A renamed route must not leave a dead allow-list entry behind.

    Stale entries are how an allow-list quietly starts covering a *different*
    route that later reuses the path.
    """
    live = {r.path for r in _app_routes()}
    # The docs routes are mounted by Starlette, not as APIRoutes.
    docs = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
    stale = sorted(PUBLIC_PATHS - live - docs)
    assert not stale, f"PUBLIC_PATHS lists routes that no longer exist: {stale}"


def test_guarded_routes_declare_bearer_auth_in_openapi() -> None:
    """Guarded routes must advertise their security scheme in openapi.json.

    Clients generate from that document. A route that authenticates but does not
    say so produces a client that omits the header and 401s at runtime.
    """
    app = create_app()
    schema = app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        if path not in PUBLIC_PATHS
        for method, op in ops.items()
        if method in {"get", "post", "put", "patch", "delete"} and not op.get("security")
    ]
    assert not missing, f"Guarded but no security scheme in openapi.json: {missing}"
