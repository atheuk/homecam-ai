"""Household arming modes (disarmed / home / away / night).

Deliberately deterministic and independent of any AI/ML component: the mode
is a plain human decision persisted in :class:`~app.models.db.SecurityState`,
and every other module in this feature (``incidents.py``, ``camera_health``)
only ever *reads* it. Nothing in this module infers a mode from camera
content; the only non-human writer is ``app.services.arming_schedules``,
which applies the household's own configured, audited time windows.

Per-zone behaviour is derived from the zone's ``kind`` (see
``app.ai.zones.ZONE_KINDS``) rather than a new per-zone override field, to
keep this change additive and low-risk: perimeter/vehicle-facing zone kinds
(driveway/parking/street) are treated as alert-worthy even while the
household is marked "home", since a car in the driveway is still worth
knowing about; all other kinds (and zoneless events) are alert-worthy only
in "away"/"night". "disarmed" never raises an alert-worthy intrusion
incident for any zone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import SecurityState
from . import audit as audit_service

MODES: tuple[str, ...] = ("disarmed", "home", "away", "night")
DEFAULT_MODE = "disarmed"
STATE_ID = "default"

# Zone kinds whose activity is treated as alert-worthy even in "home" mode,
# in addition to "away"/"night" (see module docstring).
_HOME_ALERT_KINDS = frozenset({"driveway", "parking", "street"})
_ALWAYS_ALERT_MODES = ("away", "night")


def alert_modes_for_zone(zone_kind: str | None) -> tuple[str, ...]:
    """Which arming modes make this zone kind alert-worthy."""
    if zone_kind in _HOME_ALERT_KINDS:
        return ("home",) + _ALWAYS_ALERT_MODES
    return _ALWAYS_ALERT_MODES


def is_alert_armed(mode: str, zone_kind: str | None) -> bool:
    """Whether an intrusion-candidate event in this zone/mode is actionable.

    Only gates *incident* creation (see ``app.services.incidents``); events
    are always detected, stored, and searchable regardless of mode.
    """
    if mode == "disarmed":
        return False
    return mode in alert_modes_for_zone(zone_kind)


async def get_state(session: AsyncSession) -> SecurityState:
    """Fetch the singleton arming state, creating the default row if needed."""
    state = await session.get(SecurityState, STATE_ID)
    if state is None:
        state = SecurityState(id=STATE_ID, mode=DEFAULT_MODE, changed_by=None, changed_at=datetime.now(timezone.utc))
        session.add(state)
        await session.commit()
        await session.refresh(state)
    return state


async def get_mode(session: AsyncSession) -> str:
    state = await get_state(session)
    return state.mode


async def set_mode(
    session: AsyncSession,
    mode: str,
    changed_by: str | None,
    *,
    source: str = "manual",
    actor_label: str | None = None,
) -> SecurityState:
    """Set the arming mode explicitly (a human, or an external automation).

    Deliberately leaves ``last_transition_at`` alone. That column records
    the last *scheduled* boundary applied, so an explicit change here
    stands until the next scheduled transition arrives and takes the
    household back onto its schedule - "override until the next scheduled
    transition" with no separate expiry to keep in sync. See
    :mod:`app.services.arming_schedules`.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown security mode: {mode!r}")
    state = await get_state(session)
    previous_mode = state.mode
    state.mode = mode
    state.changed_by = changed_by
    state.changed_source = source
    state.changed_at = datetime.now(timezone.utc)
    await audit_service.record(
        session,
        "security.mode_changed",
        actor_user_id=changed_by,
        actor_label=actor_label,
        target_type="security_state",
        target_id=STATE_ID,
        details={"from": previous_mode, "to": mode, "source": source},
    )
    await session.commit()
    await session.refresh(state)
    return state


def to_dict(state: SecurityState) -> dict:
    return {
        "mode": state.mode,
        "changed_by": state.changed_by,
        "changed_at": state.changed_at.isoformat(),
        "changed_source": state.changed_source or "manual",
    }
