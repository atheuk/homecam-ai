"""Google sign-in routes (OpenID Connect, see ``app/auth/google.py``).

Flow and the decisions it enforces:

* ``GET /google/start`` (top-level navigation) stores a single-use,
  10-minute state row and sets an HttpOnly browser-binding cookie, then
  redirects to Google. The binding cookie stops "login CSRF": a callback URL
  minted in an attacker's browser cannot be completed in a victim's.
* ``GET /google/callback`` atomically deletes the state row (a replay finds
  nothing), checks the binding cookie and expiry, exchanges the code with the
  PKCE verifier and verifies the ID token. Users are matched on Google's
  stable ``sub`` only.
* An email address that already belongs to a local account is never silently
  linked or taken over: the owner must sign in with the password first and
  use ``POST /google/link`` (authenticated, CSRF-protected), and the link
  callback additionally requires that same user's session cookie.
* New Google users get the least-privileged ``pending`` role and no session,
  unless their verified email is on the exact ``GOOGLE_ADMIN_EMAILS``
  allowlist. Roles are never lowered here, the allowlist never re-enables a
  disabled account, and it can only ever affect the account whose verified
  Google identity matches.
* Every outcome redirects to the configured web app with a fixed code; no
  caller-supplied redirect target exists, so there is no open redirect.
"""
from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import google
from ..auth.dependencies import COOKIE_NAME, get_current_user
from ..auth.roles import ROLE_ADMIN, ROLE_PENDING, has_access
from ..auth.security import generate_session_token, hash_token
from ..config import settings
from ..db import get_db
from ..models.db import AuthSession, OAuthLoginState, User
from ..services import audit as audit_service
from .auth_routes import set_session_cookie

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth/google", tags=["auth"])

BINDING_COOKIE = "homecam_google_oauth"
BINDING_COOKIE_PATH = "/api/v1/auth/google"
INTENT_LOGIN = "login"
INTENT_LINK = "link"
INTENT_LINK_TICKET = "link_ticket"
LINK_TICKET_TTL_SECONDS = 120
# Unauthenticated callers can create state rows, so bound the table.
MAX_PENDING_STATES = 1000
# Unusable password hash for Google-only accounts: ``verify_password``
# rejects anything that is not a well-formed PBKDF2 record.
GOOGLE_ONLY_PASSWORD_HASH = "!google-only"

NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _production() -> bool:
    return settings.app_env.lower() == "production"


def _web_redirect(params: dict[str, str] | None = None) -> RedirectResponse:
    target = f"{google.web_base_url()}/"
    if params:
        target = f"{target}?{urlencode(params)}"
    response = RedirectResponse(target, status_code=303, headers=NO_STORE_HEADERS)
    response.delete_cookie(BINDING_COOKIE, path=BINDING_COOKIE_PATH, secure=_production(), httponly=True, samesite="lax")
    return response


def _error(code: str) -> RedirectResponse:
    return _web_redirect({"google_error": code})


def _require_enabled() -> None:
    problems = google.configuration_problems()
    if problems:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured on this server: " + "; ".join(problems)
            + ". See docs/google-auth.md.",
        )


@router.get("/status")
async def google_status():
    """Public: lets the sign-in page decide whether to show the button.
    Deliberately reveals nothing beyond enabled/disabled."""
    return {"enabled": google.is_enabled()}


async def _active_session_user(session: AsyncSession, request: Request) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    auth_session = await session.get(AuthSession, hash_token(token))
    if auth_session is None or _aware(auth_session.expires_at) <= _now():
        return None
    user = await session.get(User, auth_session.user_id)
    if user is None or user.disabled_at is not None or not has_access(user.role):
        return None
    return user


@router.post("/link")
async def google_link(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    """Begin linking Google to the signed-in account.

    A state-changing, authenticated POST (so the cookie CSRF middleware
    applies) that returns a short-lived, single-use start URL. The ticket
    alone cannot link anything: the callback also requires this user's
    session cookie."""
    _require_enabled()
    ticket = google.new_secret()
    now = _now()
    session.add(OAuthLoginState(
        state_hash=hash_token(ticket),
        binding_hash="-",
        nonce="-",
        code_verifier="-",
        intent=INTENT_LINK_TICKET,
        user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(seconds=LINK_TICKET_TTL_SECONDS),
    ))
    await session.commit()
    return {"url": f"{BINDING_COOKIE_PATH}/start?{urlencode({'intent': INTENT_LINK, 'ticket': ticket})}"}


async def _consume_state(session: AsyncSession, raw: str) -> OAuthLoginState | None:
    """Atomically delete and return the state row (single use across
    replicas: two concurrent callbacks cannot both receive it)."""
    result = await session.execute(
        delete(OAuthLoginState)
        .where(OAuthLoginState.state_hash == hash_token(raw))
        .returning(
            OAuthLoginState.binding_hash,
            OAuthLoginState.nonce,
            OAuthLoginState.code_verifier,
            OAuthLoginState.intent,
            OAuthLoginState.user_id,
            OAuthLoginState.expires_at,
        )
    )
    row = result.first()
    await session.commit()
    if row is None:
        return None
    return OAuthLoginState(
        binding_hash=row.binding_hash,
        nonce=row.nonce,
        code_verifier=row.code_verifier,
        intent=row.intent,
        user_id=row.user_id,
        expires_at=row.expires_at,
    )


@router.get("/start")
async def google_start(
    request: Request,
    intent: Literal["login", "link"] = Query(INTENT_LOGIN),
    ticket: str | None = Query(None, max_length=128),
    session: AsyncSession = Depends(get_db),
):
    _require_enabled()
    now = _now()
    await session.execute(delete(OAuthLoginState).where(OAuthLoginState.expires_at < now))
    pending = await session.scalar(select(func.count()).select_from(OAuthLoginState))
    if (pending or 0) >= MAX_PENDING_STATES:
        await session.commit()
        raise HTTPException(status_code=429, detail="Too many sign-in attempts in progress; try again shortly")

    user_id: str | None = None
    if intent == INTENT_LINK:
        if not ticket:
            await session.commit()
            return _error("link_expired")
        consumed = await _consume_state(session, ticket)
        if consumed is None or consumed.intent != INTENT_LINK_TICKET or _aware(consumed.expires_at) <= now:
            return _error("link_expired")
        user_id = consumed.user_id
        # The browser starting the link must be signed in as that user.
        current = await _active_session_user(session, request)
        if current is None or current.id != user_id:
            return _error("link_requires_session")

    state = google.new_secret()
    nonce = google.new_secret()
    verifier = google.new_code_verifier()
    binding = google.new_secret()
    session.add(OAuthLoginState(
        state_hash=hash_token(state),
        binding_hash=hash_token(binding),
        nonce=nonce,
        code_verifier=verifier,
        intent=intent,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.google_oauth_state_ttl_seconds),
    ))
    await session.commit()

    response = RedirectResponse(
        google.authorization_url(state=state, nonce=nonce, verifier=verifier),
        status_code=302,
        headers=NO_STORE_HEADERS,
    )
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        max_age=settings.google_oauth_state_ttl_seconds,
        path=BINDING_COOKIE_PATH,
        httponly=True,
        secure=_production(),
        # Lax is sent on Google's top-level redirect back to the callback.
        samesite="lax",
    )
    return response


async def _issue_session(session: AsyncSession, request: Request, user: User) -> RedirectResponse:
    now = _now()
    # Session fixation: any session the browser already carried is retired
    # and a fresh, server-generated token is issued.
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        await session.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(previous)))
    token = generate_session_token()
    expires_at = now + timedelta(minutes=settings.session_ttl_minutes)
    session.add(AuthSession(token_hash=hash_token(token), user_id=user.id, created_at=now, expires_at=expires_at))
    await audit_service.record(
        session, "auth.login", actor_user_id=user.id, target_type="user", target_id=user.id,
        details={"method": "google"},
    )
    await session.commit()
    response = _web_redirect()
    set_session_cookie(response, token, expires_at)
    return response


async def _login(session: AsyncSession, request: Request, identity: google.GoogleIdentity) -> RedirectResponse:
    is_allowlisted_admin = identity.email in google.admin_emails()
    user = (await session.execute(select(User).where(User.google_sub == identity.sub))).scalars().first()

    if user is None:
        clash = await session.execute(select(User.id).where(func.lower(User.email) == identity.email).limit(1))
        if clash.scalar_one_or_none() is not None:
            # Never auto-link on email: the owner must prove control of the
            # existing account (password sign-in) and link explicitly.
            await audit_service.record(
                session, "auth.google_login_blocked", target_type="user",
                details={"reason": "email_belongs_to_existing_account"},
            )
            await session.commit()
            return _error("account_exists")
        user = User(
            id=str(uuid.uuid4()),
            email=identity.email,
            password_hash=GOOGLE_ONLY_PASSWORD_HASH,
            role=ROLE_ADMIN if is_allowlisted_admin else ROLE_PENDING,
            google_sub=identity.sub,
            google_email=identity.email,
            created_at=_now(),
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return _error("account_conflict")
        await audit_service.record(
            session, "auth.google_user_created", actor_user_id=user.id, target_type="user", target_id=user.id,
            details={"role": user.role},
        )
        await session.commit()
    else:
        if user.disabled_at is not None:
            return _error("account_disabled")
        user.google_email = identity.email
        if is_allowlisted_admin and user.role != ROLE_ADMIN:
            user.role = ROLE_ADMIN
            await audit_service.record(
                session, "auth.google_admin_promoted", actor_user_id=user.id, target_type="user", target_id=user.id,
            )
        await session.commit()

    if user.disabled_at is not None:
        return _error("account_disabled")
    if not has_access(user.role):
        return _error("pending_approval")
    return await _issue_session(session, request, user)


async def _link(
    session: AsyncSession, request: Request, state_user_id: str | None, identity: google.GoogleIdentity
) -> RedirectResponse:
    user = await _active_session_user(session, request)
    if user is None or state_user_id is None or user.id != state_user_id:
        return _error("link_requires_session")
    if user.google_sub == identity.sub:
        return _web_redirect({"tab": "system", "google": "linked"})
    if user.google_sub is not None:
        return _error("already_linked_other")
    other = await session.execute(select(User.id).where(User.google_sub == identity.sub).limit(1))
    if other.scalar_one_or_none() is not None:
        return _error("google_account_in_use")
    user.google_sub = identity.sub
    user.google_email = identity.email
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return _error("google_account_in_use")
    await audit_service.record(
        session, "auth.google_linked", actor_user_id=user.id, target_type="user", target_id=user.id,
    )
    await session.commit()
    return _web_redirect({"tab": "system", "google": "linked"})


@router.get("/callback")
async def google_callback(
    request: Request,
    state: str | None = Query(None, max_length=256),
    code: str | None = Query(None, max_length=2048),
    error: str | None = Query(None, max_length=256),
    session: AsyncSession = Depends(get_db),
):
    if not google.is_enabled():
        # No web base may be known; fail closed without redirecting anywhere.
        _require_enabled()
    if not state:
        return _error("invalid_state")
    stored = await _consume_state(session, state)
    if stored is None or stored.intent not in {INTENT_LOGIN, INTENT_LINK}:
        return _error("invalid_state")
    if _aware(stored.expires_at) <= _now():
        return _error("expired_state")
    binding = request.cookies.get(BINDING_COOKIE)
    if not binding or not hmac.compare_digest(hash_token(binding), stored.binding_hash):
        return _error("invalid_state")
    if error:
        return _error("access_denied")
    if not code:
        return _error("invalid_state")

    try:
        id_token = await google.exchange_code(code, stored.code_verifier)
        identity = await google.verify_id_token(id_token, expected_nonce=stored.nonce)
    except google.GoogleAuthError as exc:
        logger.info("Google sign-in rejected: %s", exc.code)
        return _error(exc.code)

    if stored.intent == INTENT_LINK:
        return await _link(session, request, stored.user_id, identity)
    return await _login(session, request, identity)
