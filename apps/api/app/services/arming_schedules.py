"""Automatic arming schedules.

A schedule is a recurring local-time window that arms the household into a
mode ("night 23:00-07:00 every day", "away weekdays 09:00-17:00"). The rows
live in :class:`~app.models.db.ArmingSchedule`; this module turns them into
*boundaries* - the instants at which the desired mode changes - and applies
the most recent one.

Three properties drive the design:

**Multi-replica safety.** Every replica ticks, so the transition must be
claimed, not computed-then-written. ``security_states.last_transition_at``
records the boundary already applied, and the applier issues a single
conditional ``UPDATE ... WHERE last_transition_at IS NULL OR
last_transition_at < boundary``. The database serializes that statement, so
exactly one replica sees ``rowcount == 1`` and writes the audit entry - the
same pattern as ``app.services.ingestion_lease``.

**Manual override until the next transition.** A manual change through
``security_modes.set_mode`` deliberately does *not* advance
``last_transition_at``. The override therefore survives every tick until the
schedule's next boundary arrives and takes the household back onto its
schedule, which is what "override until the next scheduled transition"
means. No expiry timestamp, no second clock.

**Wall-clock stability.** Times are stored as ``"HH:MM"`` text and resolved
in the household timezone on each occurrence, so 23:00 stays 23:00 across a
DST shift rather than drifting an hour twice a year.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import ArmingSchedule, SecurityState
from . import audit as audit_service
from .security_modes import DEFAULT_MODE, MODES, STATE_ID, get_state

logger = logging.getLogger(__name__)

_DAY_SECONDS = 24 * 60 * 60


def schedule_timezone() -> ZoneInfo:
    """Timezone the schedule's wall-clock times are interpreted in.

    ``arming_schedule_timezone`` overrides ``home_timezone``; an unknown
    name falls back to UTC with a warning rather than taking the API down,
    since a typo in a timezone must not stop the household from arming.
    """
    name = (settings.arming_schedule_timezone or settings.home_timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown arming schedule timezone %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def default_mode() -> str:
    """Mode applied when no window covers the current moment."""
    mode = settings.arming_schedule_default_mode
    return mode if mode in MODES else DEFAULT_MODE


def parse_hhmm(value: str) -> time:
    """Parse a ``"HH:MM"`` wall-clock string, raising ``ValueError`` if bad."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time {value!r}, expected HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time {value!r}, expected HH:MM")
    return time(hour=hour, minute=minute)


def normalize_days(days: list | tuple | None) -> list[int]:
    """Validate ISO weekday numbers (0 = Monday .. 6 = Sunday)."""
    if not days:
        raise ValueError("A schedule needs at least one day of the week")
    out: set[int] = set()
    for day in days:
        if isinstance(day, bool) or not isinstance(day, int):
            raise ValueError(f"Invalid day of week: {day!r}")
        if not (0 <= day <= 6):
            raise ValueError(f"Invalid day of week: {day!r}")
        out.add(day)
    return sorted(out)


def validate(mode: str, days_of_week: list, start_time: str, end_time: str) -> tuple[list[int], str, str]:
    """Validate the user-supplied parts of a schedule."""
    if mode not in MODES:
        raise ValueError(f"Unknown security mode: {mode!r}")
    days = normalize_days(days_of_week)
    start = parse_hhmm(start_time)
    end = parse_hhmm(end_time)
    return days, start.strftime("%H:%M"), end.strftime("%H:%M")


@dataclass(frozen=True)
class Window:
    """One concrete occurrence of a schedule, resolved to absolute time."""

    start: datetime
    end: datetime
    mode: str
    priority: int
    schedule_id: str
    schedule_name: str

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


def _local_to_utc(day: date, moment: time, tz: ZoneInfo) -> datetime:
    """Absolute instant of a local wall-clock time.

    During a DST "spring forward" the named wall clock does not exist;
    ``zoneinfo`` resolves it to the shifted instant, which keeps a nightly
    window firing on the day the hour disappears instead of skipping it.
    """
    return datetime.combine(day, moment).replace(tzinfo=tz).astimezone(timezone.utc)


def windows_between(
    schedules: list[ArmingSchedule],
    start: datetime,
    end: datetime,
    tz: ZoneInfo | None = None,
) -> list[Window]:
    """Every schedule occurrence that overlaps ``[start, end]``."""
    tz = tz or schedule_timezone()
    # One extra day on each side so a window that began yesterday (or
    # wraps past midnight into tomorrow) is still produced.
    first_day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date() + timedelta(days=1)
    out: list[Window] = []
    for schedule in schedules:
        if not schedule.enabled:
            continue
        try:
            start_time = parse_hhmm(schedule.start_time)
            end_time = parse_hhmm(schedule.end_time)
            days = set(normalize_days(schedule.days_of_week))
        except ValueError:
            logger.warning("skipping malformed arming schedule %s", schedule.id)
            continue
        duration = (
            datetime.combine(date(2000, 1, 2), end_time) - datetime.combine(date(2000, 1, 2), start_time)
        ).total_seconds()
        if duration <= 0:
            # Wraps past midnight; equal times mean "the whole day".
            duration += _DAY_SECONDS
        day = first_day
        while day <= last_day:
            if day.weekday() in days:
                window_start = _local_to_utc(day, start_time, tz)
                window = Window(
                    start=window_start,
                    end=window_start + timedelta(seconds=duration),
                    mode=schedule.mode,
                    priority=schedule.priority,
                    schedule_id=schedule.id,
                    schedule_name=schedule.name,
                )
                if window.end >= start and window.start <= end:
                    out.append(window)
            day += timedelta(days=1)
    return out


def resolve(schedules: list[ArmingSchedule], instant: datetime, tz: ZoneInfo | None = None) -> tuple[str, Window | None]:
    """Desired mode at ``instant``, and the window that decided it.

    Overlapping windows are resolved by highest priority, then by the most
    recent start, so a nightly window layered over a weekday window is
    unambiguous without the household having to think about ordering.
    """
    candidates = [w for w in windows_between(schedules, instant, instant, tz) if w.contains(instant)]
    if not candidates:
        return default_mode(), None
    winner = max(candidates, key=lambda w: (w.priority, w.start))
    return winner.mode, winner


def boundaries_between(
    schedules: list[ArmingSchedule],
    start: datetime,
    end: datetime,
    tz: ZoneInfo | None = None,
) -> list[datetime]:
    """Sorted, de-duplicated instants in ``(start, end]`` where the mode may change."""
    out = {
        instant
        for window in windows_between(schedules, start, end, tz)
        for instant in (window.start, window.end)
        if start < instant <= end
    }
    return sorted(out)


def previous_boundary(
    schedules: list[ArmingSchedule],
    now: datetime,
    tz: ZoneInfo | None = None,
    lookback_days: int | None = None,
) -> datetime | None:
    """Most recent boundary at or before ``now``, within the lookback window."""
    days = lookback_days if lookback_days is not None else settings.arming_schedule_lookback_days
    start = now - timedelta(days=days)
    found = boundaries_between(schedules, start, now, tz)
    return found[-1] if found else None


def next_boundary(
    schedules: list[ArmingSchedule],
    now: datetime,
    tz: ZoneInfo | None = None,
    lookahead_days: int = 8,
) -> datetime | None:
    """Earliest boundary strictly after ``now``, if any is scheduled."""
    found = boundaries_between(schedules, now, now + timedelta(days=lookahead_days), tz)
    return found[0] if found else None


async def list_schedules(session: AsyncSession, *, enabled_only: bool = False) -> list[ArmingSchedule]:
    stmt = select(ArmingSchedule)
    if enabled_only:
        stmt = stmt.where(ArmingSchedule.enabled.is_(True))
    result = await session.execute(stmt.order_by(ArmingSchedule.priority.desc(), ArmingSchedule.name))
    return list(result.scalars().all())


async def create_schedule(
    session: AsyncSession,
    *,
    name: str,
    mode: str,
    days_of_week: list,
    start_time: str,
    end_time: str,
    enabled: bool = True,
    priority: int = 0,
    actor_user_id: str | None = None,
) -> ArmingSchedule:
    days, start, end = validate(mode, days_of_week, start_time, end_time)
    now = datetime.now(timezone.utc)
    schedule = ArmingSchedule(
        id=uuid.uuid4().hex,
        name=name.strip() or mode,
        mode=mode,
        days_of_week=days,
        start_time=start,
        end_time=end,
        enabled=enabled,
        priority=priority,
        created_at=now,
        updated_at=now,
    )
    session.add(schedule)
    await audit_service.record(
        session,
        "security.schedule_created",
        actor_user_id=actor_user_id,
        target_type="arming_schedule",
        target_id=schedule.id,
        details=to_dict(schedule),
    )
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def update_schedule(
    session: AsyncSession,
    schedule: ArmingSchedule,
    *,
    name: str | None = None,
    mode: str | None = None,
    days_of_week: list | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
    actor_user_id: str | None = None,
) -> ArmingSchedule:
    before = to_dict(schedule)
    days, start, end = validate(
        mode if mode is not None else schedule.mode,
        days_of_week if days_of_week is not None else schedule.days_of_week,
        start_time if start_time is not None else schedule.start_time,
        end_time if end_time is not None else schedule.end_time,
    )
    if name is not None:
        schedule.name = name.strip() or schedule.name
    if mode is not None:
        schedule.mode = mode
    schedule.days_of_week = days
    schedule.start_time = start
    schedule.end_time = end
    if enabled is not None:
        schedule.enabled = enabled
    if priority is not None:
        schedule.priority = priority
    schedule.updated_at = datetime.now(timezone.utc)
    await audit_service.record(
        session,
        "security.schedule_updated",
        actor_user_id=actor_user_id,
        target_type="arming_schedule",
        target_id=schedule.id,
        details={"from": before, "to": to_dict(schedule)},
    )
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def delete_schedule(
    session: AsyncSession,
    schedule: ArmingSchedule,
    *,
    actor_user_id: str | None = None,
) -> None:
    details = to_dict(schedule)
    await session.delete(schedule)
    await audit_service.record(
        session,
        "security.schedule_deleted",
        actor_user_id=actor_user_id,
        target_type="arming_schedule",
        target_id=details["id"],
        details=details,
    )
    await session.commit()


def to_dict(schedule: ArmingSchedule) -> dict:
    return {
        "id": schedule.id,
        "name": schedule.name,
        "mode": schedule.mode,
        "days_of_week": list(schedule.days_of_week or []),
        "start_time": schedule.start_time,
        "end_time": schedule.end_time,
        "enabled": bool(schedule.enabled),
        "priority": int(schedule.priority or 0),
    }


async def status(session: AsyncSession, now: datetime | None = None) -> dict:
    """Schedule context for ``GET /api/v1/security/mode``.

    Reports the mode the schedule *wants* right now, when it will next
    change, and whether the household is currently overriding it - the
    three things a human needs to understand why the house is armed the way
    it is.
    """
    now = now or datetime.now(timezone.utc)
    tz = schedule_timezone()
    schedules = await list_schedules(session, enabled_only=True)
    state = await get_state(session)
    scheduled_mode, window = resolve(schedules, now, tz)
    upcoming = next_boundary(schedules, now, tz)
    next_mode = resolve(schedules, upcoming, tz)[0] if upcoming else None
    return {
        "enabled": bool(schedules) and settings.arming_scheduler_enabled,
        "timezone": str(tz),
        "scheduled_mode": scheduled_mode,
        "active_schedule_id": window.schedule_id if window else None,
        "active_schedule_name": window.schedule_name if window else None,
        "next_transition_at": upcoming.isoformat() if upcoming else None,
        "next_transition_mode": next_mode,
        # An override is "the current mode is not what the schedule asks
        # for"; it lasts until ``next_transition_at`` by construction.
        "override_active": bool(schedules) and state.mode != scheduled_mode,
    }


async def apply_due_transition(session: AsyncSession, now: datetime | None = None) -> dict | None:
    """Apply the most recent scheduled boundary, at most once per cluster.

    Returns a summary when *this* replica won the transition, ``None`` when
    there was nothing to do or another replica got there first.
    """
    now = now or datetime.now(timezone.utc)
    tz = schedule_timezone()
    schedules = await list_schedules(session, enabled_only=True)
    if not schedules:
        return None
    boundary = previous_boundary(schedules, now, tz)
    if boundary is None:
        return None
    state = await get_state(session)
    if state.last_transition_at is not None:
        last = state.last_transition_at
        if last.tzinfo is None:  # SQLite returns naive datetimes
            last = last.replace(tzinfo=timezone.utc)
        if last >= boundary:
            return None
    previous_mode = state.mode
    desired, window = resolve(schedules, boundary, tz)
    result = await session.execute(
        update(SecurityState)
        .where(
            SecurityState.id == STATE_ID,
            (SecurityState.last_transition_at.is_(None)) | (SecurityState.last_transition_at < boundary),
        )
        .values(
            mode=desired,
            changed_by=None,
            changed_source="schedule",
            changed_at=now,
            last_transition_at=boundary,
        )
        # The claim must be evaluated by the database, not re-evaluated in
        # Python against an identity-map copy (whose SQLite-sourced naive
        # datetimes cannot be compared to an aware boundary at all).
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # Another replica claimed this boundary; it owns the audit entry.
        await session.rollback()
        return None
    await session.commit()
    session.expire_all()
    if previous_mode != desired:
        await audit_service.record(
            session,
            "security.mode_changed",
            actor_user_id=None,
            actor_label="schedule",
            target_type="security_state",
            target_id=STATE_ID,
            details={
                "from": previous_mode,
                "to": desired,
                "source": "schedule",
                "boundary": boundary.isoformat(),
                "schedule_id": window.schedule_id if window else None,
                "schedule_name": window.schedule_name if window else None,
            },
        )
    return {
        "mode": desired,
        "previous_mode": previous_mode,
        "boundary": boundary,
        "changed": previous_mode != desired,
        "schedule_id": window.schedule_id if window else None,
    }
