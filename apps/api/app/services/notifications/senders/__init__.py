"""Channel senders.

Each sender is an ``async def send(spec, payload, image, timeout) -> str``
coroutine that either delivers the notification or raises
:class:`NotificationError` with a message that is safe to log and store
(secrets scrubbed). Senders never retry: a missed alert is recoverable,
a notification storm is not, and the dispatcher already rate-limits.

Web Push is handled separately (see :mod:`.webpush`) because it fans out
over per-user subscriptions rather than a single destination.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

#: Channel types this build knows how to send to.
CHANNEL_TYPES = ("webpush", "ntfy", "telegram", "webhook")

#: Channels whose delivery is authenticated/private enough to carry a
#: snapshot image. Web push is excluded on purpose: its payload is
#: delivered via a third-party push service and rendered on a lock screen,
#: so imagery stays behind the app's own authentication.
IMAGE_CAPABLE_TYPES = ("telegram", "ntfy")


class NotificationError(Exception):
    """A delivery failed. The message is already scrubbed of secrets."""


@dataclass
class ChannelSpec:
    """A channel flattened for sending: config plus the decrypted secret.

    Instances are short-lived and never serialised; ``secret`` only exists
    for the duration of one send.
    """

    id: str
    type: str
    name: str
    config: dict = field(default_factory=dict)
    secret: str | None = None
    attach_images: bool = False
    min_severity: str = "low"

    def supports_images(self) -> bool:
        """Images need both an admin opt-in *and* private delivery.

        For ntfy that means a token is configured: an unauthenticated
        public topic is readable by anyone who guesses the topic name, so
        a snapshot must never be published to one.
        """
        if not self.attach_images or self.type not in IMAGE_CAPABLE_TYPES:
            return False
        if self.type == "ntfy":
            return bool(self.secret)
        return True


def scrub(text: str, *secrets: str | None) -> str:
    """Remove any configured secret from a message before it is logged."""
    cleaned = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


def _is_blocked_address(address) -> bool:
    """Anything that is not routable on the public internet is off limits."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolved_addresses(host: str) -> list:
    """Every address ``host`` currently resolves to."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def assert_public_host(raw: str, *, field_name: str = "url") -> None:
    """Resolve ``raw`` and refuse to talk to a non-public address.

    Called immediately before every outbound request rather than only at
    save time, because a hostname that resolved publicly when an admin
    saved it can later resolve to 169.254.169.254 (DNS rebinding). A host
    that does not resolve at all is refused too: the request could not
    have succeeded anyway, and failing closed keeps the resolver from
    being a bypass.
    """
    host = urlparse(raw or "").hostname
    if not host:
        raise ValueError(f"{field_name} is not a valid URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = _resolved_addresses(host)
        except OSError:
            raise ValueError(f"{field_name} host does not resolve") from None
        if not addresses or any(_is_blocked_address(item) for item in addresses):
            raise ValueError(f"{field_name} must not resolve to a private address")
        return
    if _is_blocked_address(address):
        raise ValueError(f"{field_name} must not point at a private address")


def validate_https_url(raw: str, *, field_name: str) -> str:
    """Accept only an https URL to a public host.

    Blocking loopback/private/link-local literals keeps an authenticated
    admin from accidentally turning the notifier into a probe of the
    cluster's own internal network (SSRF). Redirects are disabled at the
    HTTP client level for the same reason, and the host is re-checked at
    send time by :func:`assert_public_host`.

    Embedded credentials are rejected outright: ``config`` is stored in
    the clear and returned to the admin UI, so a password belongs in the
    encrypted secret field instead of in the URL.
    """
    value = (raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be an https:// URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not embed credentials; use the token field")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError(f"{field_name} must not point at localhost") from None
        # A name is resolved here as a courtesy so misconfiguration is
        # caught at save time, but an unresolvable name is allowed through
        # (DNS may simply be unavailable); the send-time check is strict.
        try:
            addresses = _resolved_addresses(host)
        except OSError:
            return value.rstrip("/")
        if any(_is_blocked_address(item) for item in addresses):
            raise ValueError(f"{field_name} must not resolve to a private address")
        return value.rstrip("/")
    if _is_blocked_address(address):
        raise ValueError(f"{field_name} must not point at a private address")
    return value.rstrip("/")


def validate_config(channel_type: str, config: dict) -> dict:
    """Normalise and validate the non-secret config for a channel type.

    Raises ``ValueError`` with a user-facing message; the API turns that
    into a 400 so misconfiguration is caught at save time rather than at
    3am when an alert silently fails.
    """
    config = dict(config or {})
    if channel_type == "ntfy":
        server = validate_https_url(config.get("server") or "https://ntfy.sh", field_name="server")
        topic = (config.get("topic") or "").strip()
        if not topic or "/" in topic:
            raise ValueError("topic is required and must not contain '/'")
        return {"server": server, "topic": topic}
    if channel_type == "telegram":
        chat_id = str(config.get("chat_id") or "").strip()
        if not chat_id:
            raise ValueError("chat_id is required")
        return {"chat_id": chat_id}
    if channel_type == "webhook":
        return {"url": validate_https_url(config.get("url") or "", field_name="url")}
    if channel_type == "webpush":
        # Web push has no per-channel addressing: it fans out to the
        # subscriptions registered by signed-in browsers.
        return {}
    raise ValueError(f"unsupported channel type: {channel_type}")


def requires_secret(channel_type: str) -> bool:
    """Telegram cannot send at all without a bot token."""
    return channel_type == "telegram"


from . import ntfy, telegram, webhook  # noqa: E402  (circular-free, needs ChannelSpec)

SENDERS = {
    "ntfy": ntfy.send,
    "telegram": telegram.send,
    "webhook": webhook.send,
}

__all__ = [
    "CHANNEL_TYPES",
    "IMAGE_CAPABLE_TYPES",
    "SENDERS",
    "ChannelSpec",
    "NotificationError",
    "assert_public_host",
    "requires_secret",
    "scrub",
    "validate_config",
    "validate_https_url",
]
