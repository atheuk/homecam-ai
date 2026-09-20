"""Password hashing and session token helpers.

Uses the standard-library PBKDF2-HMAC-SHA256 implementation instead of a
native-extension hasher (bcrypt/argon2) so the local development scaffold
installs cleanly on every host, including platforms without a C toolchain.
This is intentionally conservative for a local-only dev scaffold; production
deployments should move to a managed identity provider (see SPEC section 27).
"""
import hashlib
import hmac
import secrets

_ITERATIONS = 260_000
_ALGORITHM = "sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return f"pbkdf2_{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations, salt, hex_digest = encoded_hash.split("$")
        iterations = int(iterations)
        alg_name = algorithm.removeprefix("pbkdf2_")
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac(alg_name, password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return hmac.compare_digest(candidate.hex(), hex_digest)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
