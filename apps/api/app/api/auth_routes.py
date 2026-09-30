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

import asyncio
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
from ..services import audit as audit_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# The failed-attempt counter / lockout threshold is a classic
# check-then-act (read ``failed_attempts``, decide, write it back) spread
# across two ``await``s (the SELECT and the COMMIT), which is enough for
# two concurrent login attempts for the *same* account to interleave and
# both read the same pre-increment count - silently losing one increment
# and letting the attacker get one extra guess past the configured
# threshold. The app runs as a single Python process (one event loop, no
# multi-worker deployment - see README), so a per-email in-process lock
# fully serializes the accounting for a given account without affecting
# concurrent logins for *other* accounts.
_login_locks: dict[str, asyncio.Lock] = {}


def _lock_for_email(email: str) -> asyncio.Lock:
    lock = _login_locks.get(email)
    if lock is None:
        lock = asyncio.Lock()
        _login_locks[email] = lock
    return lock


def _is_locked(user: User, now: datetime) -> bool:
    locked_until = user.locked_until
    if locked_until is None:
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > now


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
    email = payload.email.lower().strip()
    async with _lock_for_email(email):
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        now = datetime.now(timezone.utc)

        # Account lockout (OWASP ASVS V2.2 / brute-force guidance): once
        # locked, refuse the login *before* checking the password at all,
        # so a locked account never leaks whether a guessed password would
        # have been right.
        if user is not None and _is_locked(user, now):
            raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed sign-ins")

        if user is None or not verify_password(payload.password, user.password_hash):
            if user is not None:
                user.failed_attempts += 1
                if user.failed_attempts >= settings.auth_max_failed_attempts:
                    user.locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
                    await audit_service.record(
                        session, "auth.account_locked", actor_user_id=user.id, target_type="user", target_id=user.id,
                        details={"failed_attempts": user.failed_attempts},
                    )
                await session.commit()
            raise HTTPException(status_code=401, detail="Invalid email or password")

        user.failed_attempts = 0
        user.locked_until = None
        token = generate_session_token()
        expires_at = now + timedelta(minutes=settings.session_ttl_minutes)
        session.add(AuthSession(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=now,
            expires_at=expires_at,
        ))
        await audit_service.record(session, "auth.login", actor_user_id=user.id, target_type="user", target_id=user.id)
        await session.commit()
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", expires=int(expires_at.timestamp()))
    return TokenOut(access_token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.post("/logout")
async def logout(
    response: Response,
    auth_session: AuthSession = Depends(get_current_auth_session),
    session: AsyncSession = Depends(get_db),
):
    await audit_service.record(
        session, "auth.logout", actor_user_id=auth_session.user_id, target_type="user", target_id=auth_session.user_id,
    )
    await session.delete(auth_session)
    await session.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok"}


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Sign out of every device/session for the current user (e.g. after a
    suspected compromise). Deletes every stored session token, including the
    caller's own - the caller must sign in again afterwards."""
    result = await session.execute(select(AuthSession).where(AuthSession.user_id == user.id))
    sessions = list(result.scalars().all())
    for auth_session in sessions:
        await session.delete(auth_session)
    await audit_service.record(
        session, "auth.sessions_revoked_all", actor_user_id=user.id, target_type="user", target_id=user.id,
        details={"revoked_count": len(sessions)},
    )
    await session.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok", "revoked": len(sessions)}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
