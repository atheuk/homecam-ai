"""Authentication endpoints (SPEC section 27).

Minimal HomeCam-native email/password authentication suitable for local
development. Passwords are hashed with PBKDF2-HMAC-SHA256 (see
``app/auth/security.py``); sessions are opaque server-side tokens stored
hashed in the database with an expiry, so no secret material is persisted in
the clear and nothing sensitive is exposed to frontend JavaScript beyond the
bearer token the user's own login call returns.

This intentionally does not implement Microsoft Entra ID; the architecture
(a swappable dependency returning the current user) allows adding it later
without changing route signatures.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models.db import AuthSession, User
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..auth.dependencies import COOKIE_NAME, get_current_auth_session, get_current_user
from ..auth.security import generate_session_token, hash_password, hash_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: RegisterIn, session: AsyncSession = Depends(get_db)):
    existing = await session.execute(select(User).where(User.email == payload.email))
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(
        id=str(uuid.uuid4()),
        email=payload.email,
        password_hash=hash_password(payload.password),
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, response: Response, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(User).where(User.email == payload.email.lower().strip()))
    user = result.scalars().first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.session_ttl_minutes)
    session.add(AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    ))
    await session.commit()
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", expires=int(expires_at.timestamp()))
    return TokenOut(access_token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.post("/logout")
async def logout(
    response: Response,
    auth_session: AuthSession = Depends(get_current_auth_session),
    session: AsyncSession = Depends(get_db),
):
    await session.delete(auth_session)
    await session.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok"}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
