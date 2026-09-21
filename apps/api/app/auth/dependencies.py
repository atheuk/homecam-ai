"""FastAPI dependency for resolving the current authenticated user from a
bearer token or session cookie (SPEC section 27)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models.db import AuthSession, User
from .security import hash_token

COOKIE_NAME = "homecam_session"


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.cookies.get(COOKIE_NAME)


async def get_current_auth_session(
    request: Request, session: AsyncSession = Depends(get_db)
) -> AuthSession:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token_hash = hash_token(token)
    auth_session = await session.get(AuthSession, token_hash)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    expires_at = auth_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        await session.delete(auth_session)
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return auth_session


async def get_current_user(
    auth_session: AuthSession = Depends(get_current_auth_session),
    session: AsyncSession = Depends(get_db),
) -> User:
    user = await session.get(User, auth_session.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user

