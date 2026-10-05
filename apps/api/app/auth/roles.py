"""Account roles.

HomeCam has a single privileged role: ``admin`` (the household owner(s)),
which may use every API route. ``pending`` is the least-privileged default
for accounts created by an external identity provider: the account exists
(so an admin can see who asked for access) but it grants no API access and
never receives a session.
"""
ROLE_ADMIN = "admin"
ROLE_PENDING = "pending"
ROLES = frozenset({ROLE_ADMIN, ROLE_PENDING})


def has_access(role: str | None) -> bool:
    return role == ROLE_ADMIN
