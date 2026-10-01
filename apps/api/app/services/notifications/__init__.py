"""Outbound incident notifications.

The dashboard already shows incidents to whoever is looking at it. This
package is the part that reaches someone who *is not* looking: a push
notification, an ntfy message, a Telegram message, or a webhook POST.

Design rules that the rest of the codebase relies on:

* **Opt-in.** Nothing is sent until a human creates and enables a channel.
  There is no default destination and no implicit fallback.
* **Never blocking.** :func:`notify_incident` is a synchronous, fire-and-
  forget scheduler. Event ingestion never waits for a push service.
* **Secrets stay secret.** Channel secrets are encrypted at rest with the
  same helper as provider credentials, are never returned by the API, and
  are scrubbed out of every error string before it is logged or stored.
* **No identity claims.** Payloads describe what was detected (motion,
  person, vehicle, package) on which camera - never *who* it was. See
  docs/ai-features.md.
"""

from .dispatch import dispatch_incident, notify_incident, send_test  # noqa: F401
from .payload import NotificationPayload, build_payload  # noqa: F401
from .senders import SENDERS, NotificationError  # noqa: F401
