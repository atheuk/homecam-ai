"""Google sign-in: OpenID Connect authorization-code flow with PKCE.

Everything security-relevant happens server-side:

* the browser is sent to Google with a random ``state`` (CSRF), ``nonce``
  (ID-token replay) and an S256 PKCE challenge;
* the callback exchanges the code at Google's token endpoint over TLS using
  the confidential client secret plus the PKCE verifier;
* the returned ID token is verified locally: RS256 signature against
  Google's published JWKS, exact issuer, exact audience (our client id),
  ``exp``/``iat``, the nonce we issued, and ``email_verified``.

Nothing the browser sends (email, profile, claims) is ever trusted; the only
identity used is the verified ID token's ``sub`` and verified ``email``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from ..config import settings

logger = logging.getLogger(__name__)

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
# Google documents both forms as valid ``iss`` values for its ID tokens.
ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
CALLBACK_PATH = "/api/v1/auth/google/callback"
CLOCK_SKEW_SECONDS = 60


class GoogleAuthError(Exception):
    """A Google sign-in failed. ``code`` is a fixed, non-sensitive identifier
    safe to hand to the browser (it never contains token or claim data)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str  # normalized (stripped, lower-cased), Google-verified


def _strip(value: str | None) -> str:
    return (value or "").strip()


def redirect_uri() -> str | None:
    explicit = _strip(settings.google_oauth_redirect_uri)
    if explicit:
        return explicit
    base = _strip(settings.public_api_base_url).rstrip("/")
    return f"{base}{CALLBACK_PATH}" if base else None


def web_base_url() -> str | None:
    explicit = _strip(settings.web_app_base_url).rstrip("/")
    if explicit:
        return explicit
    for origin in settings.cors_origins.split(","):
        origin = origin.strip().rstrip("/")
        if origin:
            return origin
    return None


def _is_valid_absolute_url(url: str | None, *, require_https: bool) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme not in ({"https"} if require_https else {"https", "http"}):
        return False
    return bool(parts.netloc) and not parts.query and not parts.fragment


def configuration_problems() -> list[str]:
    """Human-readable, secret-free reasons Google sign-in is unavailable."""
    production = settings.app_env.lower() == "production"
    problems: list[str] = []
    if not _strip(settings.google_oauth_client_id):
        problems.append("GOOGLE_OAUTH_CLIENT_ID is not set")
    if not _strip(settings.google_oauth_client_secret):
        problems.append("GOOGLE_OAUTH_CLIENT_SECRET is not set")
    if not _is_valid_absolute_url(redirect_uri(), require_https=production):
        problems.append(
            "GOOGLE_OAUTH_REDIRECT_URI (or PUBLIC_API_BASE_URL) must be an absolute "
            + ("https " if production else "")
            + "URL"
        )
    if not _is_valid_absolute_url(web_base_url(), require_https=production):
        problems.append("WEB_APP_BASE_URL (or CORS_ORIGINS) must be an absolute URL of the web app")
    return problems


def is_enabled() -> bool:
    return not configuration_problems()


def admin_emails() -> frozenset[str]:
    """Exact, normalized admin allowlist. Domain-only or wildcard entries are
    rejected outright so a typo can never grant a whole domain admin."""
    allowed: set[str] = set()
    for raw in settings.google_admin_emails.split(","):
        email = raw.strip().lower()
        if not email:
            continue
        local, sep, domain = email.partition("@")
        if not sep or not local or not domain or "@" in domain or "*" in email or "." not in domain:
            logger.warning("Ignoring invalid GOOGLE_ADMIN_EMAILS entry (must be one exact address)")
            continue
        allowed.add(email)
    return frozenset(allowed)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    # RFC 7636: 43-128 characters from the unreserved set; 64 random bytes
    # base64url-encode to 86 characters.
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(*, state: str, nonce: str, verifier: str) -> str:
    query = urlencode({
        "client_id": _strip(settings.google_oauth_client_id),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "access_type": "online",
    })
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


async def exchange_code(code: str, verifier: str) -> str:
    """Exchange the authorization code; returns the raw ID token."""
    data = {
        "code": code,
        "client_id": _strip(settings.google_oauth_client_id),
        "client_secret": _strip(settings.google_oauth_client_secret),
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(TOKEN_ENDPOINT, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.warning("Google token exchange failed: %s", type(exc).__name__)
        raise GoogleAuthError("token_exchange_failed") from None
    if response.status_code != 200:
        # Only the status is logged; the body may echo request details.
        logger.warning("Google token exchange rejected with HTTP %s", response.status_code)
        raise GoogleAuthError("token_exchange_failed")
    try:
        id_token = response.json().get("id_token")
    except ValueError:
        id_token = None
    if not isinstance(id_token, str) or not id_token:
        raise GoogleAuthError("token_exchange_failed")
    return id_token


_jwks_client: jwt.PyJWKClient | None = None


def _signing_key(id_token: str):
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(JWKS_URI, cache_keys=True, lifespan=3600, timeout=10)
    return _jwks_client.get_signing_key_from_jwt(id_token).key


async def resolve_signing_key(id_token: str):
    try:
        return await asyncio.to_thread(_signing_key, id_token)
    except jwt.PyJWKClientError:
        raise GoogleAuthError("invalid_token") from None
    except Exception as exc:  # network failures while fetching the JWKS
        logger.warning("Google JWKS lookup failed: %s", type(exc).__name__)
        raise GoogleAuthError("invalid_token") from None


async def verify_id_token(id_token: str, *, expected_nonce: str) -> GoogleIdentity:
    key = await resolve_signing_key(id_token)
    client_id = _strip(settings.google_oauth_client_id)
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "aud", "sub", "exp", "iat"], "verify_signature": True},
        )
    except jwt.PyJWTError:
        raise GoogleAuthError("invalid_token") from None
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("invalid_token")
    # ``aud`` is a single string for Google ID tokens. If ``azp`` is present
    # it must also be us (the token was issued to this client).
    if claims.get("aud") != client_id or claims.get("azp", client_id) != client_id:
        raise GoogleAuthError("invalid_token")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), expected_nonce.encode()):
        raise GoogleAuthError("invalid_token")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise GoogleAuthError("invalid_token")
    email = claims.get("email")
    verified = claims.get("email_verified")
    if verified not in (True, "true"):
        raise GoogleAuthError("email_not_verified")
    if not isinstance(email, str) or "@" not in email or len(email) > 255:
        raise GoogleAuthError("email_not_verified")
    return GoogleIdentity(sub=sub, email=email.strip().lower())
