from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")

def now_madrid() -> datetime:
    return datetime.now(tz=TZ)

def week_start_sunday(dt: datetime) -> datetime:
    # Sunday 00:00 in Europe/Madrid
    dt = dt.astimezone(TZ)
    # weekday(): Monday=0 ... Sunday=6
    days_since_sunday = (dt.weekday() + 1) % 7  # Sunday -> 0, Monday -> 1, ...
    sunday = dt - timedelta(days=days_since_sunday)
    return sunday.replace(hour=0, minute=0, second=0, microsecond=0)

def next_sunday_midnight(dt: datetime) -> datetime:
    dt = dt.astimezone(TZ)
    this_sunday = week_start_sunday(dt)
    if dt >= this_sunday:
        return this_sunday + timedelta(days=7)
    return this_sunday

def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()

def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(TZ)
