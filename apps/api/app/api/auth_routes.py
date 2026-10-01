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
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import case, or_, select, text, update
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
# check-then-act (read ``failed_attempts``/``locked_until``, decide, write
# it back - for both the failure path and, importantly, the success path
# that resets them). An in-process, per-email ``asyncio.Lock`` fully
# serializes that for two concurrent attempts handled by the *same*
# process, and is kept below as a cheap fast-path that avoids DB
# contention in the common case. It is not sufficient on its own: the API
# can run as up to two Container Apps replicas sharing only the database
# (see infra/modules/api.bicep's ``scale.maxReplicas: 2``), and a lock in
# one replica's memory does nothing to serialize an attempt handled by the
# other. The actual safety guarantee comes from ``_register_failed_attempt``
# and ``_finalize_successful_login`` below, which each perform their
# respective read-decide-write as a single atomic, conditional ``UPDATE``
# evaluated by the database against the current row - safe regardless of
# which replica issues it or whether the in-process lock is even held. In
# particular, a correct password guess can never complete a login once a
# concurrent, independently-arriving failed attempt has committed the
# lockout, because the success path's ``UPDATE`` re-checks ``locked_until``
# against the database's current value, not a stale in-memory read.
@lru_cache(maxsize=256)
def _lock_for_email(email: str) -> asyncio.Lock:
    return asyncio.Lock()


async def _lock_initial_registration(session: AsyncSession) -> None:
    """Serialize one-time account enrollment across API replicas."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "homecam-ai:initial-account"},
        )
    elif dialect == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise HTTPException(status_code=503, detail="Account setup is unavailable")


async def _register_failed_attempt(session: AsyncSession, user_id: str, now: datetime) -> tuple[int, datetime | None]:
    """Atomically increment ``failed_attempts`` and, if the new count
    reaches the configured threshold, set ``locked_until`` - all in one
    ``UPDATE ... RETURNING`` statement.

    Doing this as a single statement (rather than reading the row in
    Python, deciding, then writing it back) matters because two concurrent
    failed attempts for the *same* account can be handled by two different
    replicas of this API sharing only the database. The database evaluates
    ``failed_attempts + 1`` against the current, row-locked value itself,
    so concurrent UPDATEs for the same ``user_id`` are always serialized by
    the database's own row-level locking - neither can read a stale count
    or silently lose the other's increment, on SQLite or Postgres alike.

    Returns the post-increment ``(failed_attempts, locked_until)``.
    """
    new_locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
    threshold = settings.auth_max_failed_attempts
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(
            failed_attempts=User.failed_attempts + 1,
            locked_until=case(
                (User.failed_attempts + 1 >= threshold, new_locked_until),
                else_=User.locked_until,
            ),
        )
        .returning(User.failed_attempts, User.locked_until)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    row = result.first()
    await session.commit()
    if row is None:
        return (0, None)
    return (row[0], row[1])


async def _finalize_successful_login(session: AsyncSession, user_id: str, now: datetime) -> bool:
    """Atomically reset the lockout counter for a *verified-correct* password,
    but only if the account is still unlocked at write time.

    Verifying the password happens against a snapshot read taken earlier in
    the request, with no lock held across the ``await`` - so a second,
    concurrent request (possibly on the other API replica) can be in the
    middle of a failed attempt for the same account at the same moment.
    Without this guard, two simultaneous guesses starting from
    ``failed_attempts == threshold - 1`` can both pass their own snapshot's
    "is this account locked?" check before either one's increment has
    committed, letting a correct guess complete the login even though a
    sibling wrong guess independently reaches the lockout threshold in the
    same instant.

    This single ``UPDATE ... WHERE (locked_until IS NULL OR locked_until <=
    now) ... RETURNING`` closes that window: like
    ``_register_failed_attempt``, the database serializes concurrent
    UPDATEs to the same row, so if a sibling failed-attempt UPDATE that sets
    ``locked_until`` commits first, this statement's WHERE clause is
    re-evaluated against that new value and matches no row - the login is
    then rejected as locked instead of succeeding. Returns ``True`` only if
    the reset was actually applied (i.e. the account was not locked at the
    moment this statement executed).
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .where(or_(User.locked_until.is_(None), User.locked_until <= now))
        .values(failed_attempts=0, locked_until=None)
        .returning(User.id)
        # ``synchronize_session=False``: this UPDATE's correctness comes
        # entirely from the database re-evaluating the WHERE clause against
        # the current row, not from SQLAlchemy's in-Python ORM-session
        # sync. The default "evaluate" strategy would otherwise try to
        # compare the WHERE clause's tz-aware ``now`` against whatever
        # possibly-naive ``locked_until`` is cached on any already-loaded
        # ``User`` instance, which can raise on SQLite; we don't rely on
        # any loaded instance's attributes here anyway (the caller only
        # uses this function's boolean return value).
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    row = result.first()
    await session.commit()
    return row is not None


def _is_locked(user: User, now: datetime) -> bool:
    locked_until = user.locked_until
    if locked_until is None:
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > now


@router.post("/register", response_model=UserOut, status_code=201)
async def register(
    payload: RegisterIn,
    bootstrap_secret: str | None = Header(default=None, alias="X-HomeCam-Bootstrap-Secret"),
    session: AsyncSession = Depends(get_db),
):
    if settings.app_env.lower() == "production":
        configured_secret = settings.auth_bootstrap_secret
        if (
            not configured_secret
            or bootstrap_secret is None
            or not hmac.compare_digest(
                bootstrap_secret.encode("utf-8"),
                configured_secret.encode("utf-8"),
            )
        ):
            raise HTTPException(status_code=403, detail="Account setup is unavailable")
        await _lock_initial_registration(session)
        first_user = await session.execute(select(User.id).limit(1))
        if first_user.scalar_one_or_none() is not None:
            raise HTTPException(status_code=403, detail="Account setup is unavailable")

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
                new_failed_attempts, new_locked_until = await _register_failed_attempt(session, user.id, now)
                if new_locked_until is not None and new_failed_attempts >= settings.auth_max_failed_attempts:
                    await audit_service.record(
                        session, "auth.account_locked", actor_user_id=user.id, target_type="user", target_id=user.id,
                        details={"failed_attempts": new_failed_attempts},
                    )
                    await session.commit()
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Password verified against a snapshot read taken above, with no
        # cross-replica lock held while awaiting it - so re-check (and
        # reset) the lockout state as a single atomic, conditional UPDATE
        # rather than trusting that snapshot. This is what actually
        # prevents a correct guess from completing a login that raced a
        # concurrent failed attempt past the lockout threshold; see
        # ``_finalize_successful_login`` for the full explanation.
        if not await _finalize_successful_login(session, user.id, now):
            raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed sign-ins")

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
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.app_env.lower() == "production",
        samesite="none" if settings.app_env.lower() == "production" else "lax",
        path="/",
        expires=int(expires_at.timestamp()),
    )
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
