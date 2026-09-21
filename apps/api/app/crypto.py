"""Reversible local encryption for provider secrets (Dahua password, Eufy
adapter token) stored in the database.

Design tradeoff: the standard ``cryptography`` package requires a native
Rust/C extension build. It fails to install on some supported HomeCam host
platforms (e.g. Windows on ARM64 without a working Rust/OpenSSL toolchain),
mirroring the reason ``app/auth/security.py`` already uses stdlib PBKDF2
instead of bcrypt/argon2 for password hashing rather than requiring a
compiled dependency. To keep the admin plane installable everywhere without
adding a native dependency, this module implements a small stdlib-only
"encrypt-then-MAC" construction:

- A key is derived once from ``SECRET_KEY`` via SHA-256.
- A fresh random 16-byte nonce is generated per secret.
- A keystream is derived from ``SHA256(key || nonce || counter)`` blocks
  (a SHA-256-based counter-mode PRF) and XORed with the plaintext.
- An HMAC-SHA256 tag over the nonce + ciphertext provides integrity, so a
  tampered or corrupted stored value is rejected instead of silently
  producing garbage.

This is intentionally documented as a local-only, non-standard construction
(not NIST-validated AES-GCM/Fernet). If ``cryptography`` becomes installable
in the target environment, this module can be swapped for ``Fernet``
without changing any caller. Rotating ``SECRET_KEY`` invalidates every
previously stored secret (see docs/dahua.md and docs/eufy.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from .config import settings

_VERSION_PREFIX = "hcv1"
_NONCE_LEN = 16
_MAC_LEN = 32


class SecretDecryptionError(ValueError):
    """Raised when a stored secret cannot be decrypted/verified."""


def _derive_key() -> bytes:
    return hashlib.sha256(settings.secret_key.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        blocks.append(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
        produced += len(blocks[-1])
        counter += 1
    return b"".join(blocks)[:length]


def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret value for storage. Never log or return the result
    alongside the plaintext."""
    key = _derive_key()
    nonce = os.urandom(_NONCE_LEN)
    data = plaintext.encode("utf-8")
    ciphertext = _xor(data, _keystream(key, nonce, len(data)))
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    payload = nonce + mac + ciphertext
    return f"{_VERSION_PREFIX}${base64.urlsafe_b64encode(payload).decode('ascii')}"


def decrypt_secret(token: str) -> str:
    """Decrypt a value previously produced by :func:`encrypt_secret`.

    Raises :class:`SecretDecryptionError` if the token is malformed, was
    tampered with, or was encrypted under a different ``SECRET_KEY``.
    """
    version, _, encoded = token.partition("$")
    if version != _VERSION_PREFIX or not encoded:
        raise SecretDecryptionError("unrecognized stored secret encoding")
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretDecryptionError("stored secret is not valid base64") from exc
    if len(raw) < _NONCE_LEN + _MAC_LEN:
        raise SecretDecryptionError("stored secret is truncated")
    nonce, mac, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:_NONCE_LEN + _MAC_LEN], raw[_NONCE_LEN + _MAC_LEN:]
    key = _derive_key()
    expected_mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise SecretDecryptionError("stored secret failed integrity check (SECRET_KEY may have changed)")
    return _xor(ciphertext, _keystream(key, nonce, len(ciphertext))).decode("utf-8")
