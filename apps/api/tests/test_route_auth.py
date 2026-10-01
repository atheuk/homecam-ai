"""Fail-closed authentication coverage for the versioned API surface."""
import pytest
from fastapi.routing import APIRoute

from app.auth.dependencies import get_current_auth_session, get_current_user
from app.main import app

PUBLIC_API_ROUTES = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register"),
}


def _has_auth_dependency(dependant) -> bool:
    if dependant.call in {get_current_user, get_current_auth_session}:
        return True
    return any(_has_auth_dependency(child) for child in dependant.dependencies)


def test_every_versioned_route_is_authenticated_except_login_and_bootstrap():
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
    ]
    assert routes
    missing_auth = []
    for route in routes:
        for method in route.methods or ():
            if (method, route.path) not in PUBLIC_API_ROUTES and not _has_auth_dependency(route.dependant):
                missing_auth.append(f"{method} {route.path}")
    assert not missing_auth, f"Unauthenticated API routes: {sorted(missing_auth)}"

    registered_public_routes = {
        (method, route.path)
        for route in routes
        for method in route.methods or ()
        if not _has_auth_dependency(route.dependant)
    }
    assert registered_public_routes == PUBLIC_API_ROUTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/cameras"),
        ("GET", "/api/v1/cameras/mock-front-door/snapshot"),
        ("GET", "/api/v1/cameras/mock-front-door/live"),
        ("GET", "/api/v1/cameras/mock-front-door/hls/index.m3u8"),
        ("GET", "/api/v1/events"),
        ("GET", "/api/v1/events/unknown"),
        ("GET", "/api/v1/events/unknown/photo"),
        ("GET", "/api/v1/events/unknown/photo/full"),
        ("GET", "/api/v1/persons"),
        ("GET", "/api/v1/persons/unknown/duplicates"),
        ("PATCH", "/api/v1/persons/unknown"),
        ("GET", "/api/v1/activities"),
        ("GET", "/api/v1/activities/unknown"),
        ("GET", "/api/v1/security/mode"),
        ("GET", "/api/v1/security/incidents"),
        ("GET", "/api/v1/admin/providers"),
        ("GET", "/api/v1/ws"),
    ],
)
async def test_anonymous_household_routes_are_denied(anonymous_client, method, path):
    response = await anonymous_client.request(method, path, json={} if method != "GET" else None)
    assert response.status_code == 401, f"{method} {path} returned {response.status_code}"


@pytest.mark.asyncio
async def test_authenticated_household_reads_remain_available(client):
    assert (await client.get("/api/v1/cameras")).status_code == 200
    assert (await client.get("/api/v1/events")).status_code == 200
    assert (await client.get("/api/v1/persons")).status_code == 200
    assert (await client.get("/api/v1/activities")).status_code == 200
    assert (await client.get("/api/v1/security/mode")).status_code == 200
