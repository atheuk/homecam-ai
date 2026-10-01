"""Shared helper for tests that call authenticated endpoints."""


async def auth_headers(client, email: str = "reviewer@example.com") -> dict[str, str]:
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code in (201, 409), register.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["access_token"]}
