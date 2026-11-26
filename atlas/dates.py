"""Relative date expressions -> concrete dates, computed by code (never by the LLM).

Claude only says *which words* the user used, as a small expression; this module turns
the expression into a date in Europe/Rome, relative to the moment the message arrived.

Expressions (case-insensitive):
  today | tomorrow | day_after_tomorrow
  weekday:mon..sun        next such day strictly after today ("в суботу" said on Saturday
                          is ambiguous -> AmbiguousDate, Atlas asks)
  this_week               Saturday of the current week (warehouse works Mon-Sat)
  next_week               Saturday of next week; reminder goes on that Monday
  end_of_month            last day of the current month
  in_days:N | in_weeks:N
  date:YYYY-MM-DD | date:DD.MM | date:DD.MM.YYYY
  day:DD                  next occurrence of that day of month
Optional time suffix: "tomorrow@14:00".
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
EVENING_BEFORE = time(18, 0)
MORNING = time(7, 0)


class DateError(ValueError):
    """Expression cannot be understood."""


class AmbiguousDate(DateError):
    """Expression understood but has more than one reasonable meaning."""


@dataclass(frozen=True)
class Due:
    day: date
    at: time | None      # exact time if the user said one
    span: str            # "day" | "week" | "month" - drives the reminder policy

    def iso(self) -> str:
        return self.day.isoformat() if self.at is None else f"{self.day.isoformat()}T{self.at:%H:%M}"


def local_now(utc_iso: str | None = None) -> datetime:
    if utc_iso is None:
        return datetime.now(TZ)
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise DateError(f"timestamp without timezone: {utc_iso!r}")
    return dt.astimezone(TZ)


def _saturday_of(d: date) -> date:
    return d + timedelta(days=5 - d.weekday()) if d.weekday() <= 5 else d + timedelta(days=6)


def resolve(expr: str, now: datetime) -> Due:
    """Resolve an expression relative to `now` (aware datetime, converted to Europe/Rome)."""
    if not expr or not isinstance(expr, str):
        raise DateError("empty date expression")
    now = now.astimezone(TZ)
    today = now.date()
    raw = expr.strip().lower()
    at = None
    if "@" in raw:
        raw, _, t = raw.partition("@")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t.strip())
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise DateError(f"bad time in {expr!r}")
        at = time(int(m[1]), int(m[2]))
    kind, _, arg = raw.partition(":")
    span = "day"

    if kind == "today":
        d = today
    elif kind == "tomorrow":
        d = today + timedelta(days=1)
    elif kind == "day_after_tomorrow":
        d = today + timedelta(days=2)
    elif kind == "weekday":
        if arg not in WEEKDAYS:
            raise DateError(f"bad weekday in {expr!r}")
        delta = (WEEKDAYS[arg] - today.weekday()) % 7
        if delta == 0:
            raise AmbiguousDate(f"'{arg}' is today: today or in a week?")
        d = today + timedelta(days=delta)
    elif kind == "this_week":
        d, span = _saturday_of(today), "week"
        if today.weekday() == 6:
            raise AmbiguousDate("'this week' said on Sunday")
    elif kind == "next_week":
        monday = today + timedelta(days=7 - today.weekday())
        d, span = monday + timedelta(days=5), "week"
    elif kind == "end_of_month":
        d = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        span = "month"
    elif kind in ("in_days", "in_weeks"):
        if not arg.isdigit() or int(arg) == 0 or int(arg) > 366:
            raise DateError(f"bad number in {expr!r}")
        d = today + timedelta(days=int(arg) * (7 if kind == "in_weeks" else 1))
    elif kind == "date":
        d = _parse_date(arg, today, expr)
    elif kind == "day":
        if not arg.isdigit() or not 1 <= int(arg) <= 31:
            raise DateError(f"bad day in {expr!r}")
        d = _next_day_of_month(int(arg), today)
    else:
        raise DateError(f"unknown date expression {expr!r}")
    if d < today:
        raise AmbiguousDate(f"{d.isoformat()} is in the past")
    return Due(d, at, span)


def _parse_date(arg: str, today: date, expr: str) -> date:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
            return date.fromisoformat(arg)
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", arg)
        if m:
            day, month = int(m[1]), int(m[2])
            if m[3]:
                return date(int(m[3]), month, day)
            d = date(today.year, month, day)
            return d if d >= today else date(today.year + 1, month, day)
    except ValueError as exc:
        raise DateError(f"impossible date {expr!r}") from exc
    raise DateError(f"bad date {expr!r}")


def _next_day_of_month(day: int, today: date) -> date:
    y, m = today.year, today.month
    for _ in range(13):
        if day <= calendar.monthrange(y, m)[1]:
            d = date(y, m, day)
            if d >= today:
                return d
        m += 1
        if m == 13:
            y, m = y + 1, 1
    raise DateError(f"no month has day {day}")  # pragma: no cover


def remind_at(due: Due, now: datetime) -> str | None:
    """When to remind (ISO, Europe/Rome offset). None = already too late, overdue digest handles it.

    day   -> 18:00 the evening before; if that has passed, 07:00 on the day
    week  -> 07:00 on Monday of that week, else 18:00 the evening before
    month -> 07:00 three days before, else 07:00 on the day
    """
    now = now.astimezone(TZ)
    d = due.day
    if due.span == "week":
        plan = [(d - timedelta(days=d.weekday()), MORNING), (d - timedelta(days=1), EVENING_BEFORE)]
    elif due.span == "month":
        plan = [(d - timedelta(days=3), MORNING), (d, MORNING)]
    else:
        plan = [(d - timedelta(days=1), EVENING_BEFORE), (d, MORNING)]
    candidates = [datetime.combine(day, t, TZ) for day, t in plan]
    if due.at is not None:
        candidates.append(datetime.combine(d, due.at, TZ) - timedelta(hours=2))
    for at in sorted(candidates):
        if at > now:
            return at.isoformat(timespec="minutes")
    return None
