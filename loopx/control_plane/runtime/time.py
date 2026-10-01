from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any


_MIN_TIMESTAMP = datetime.min.replace(tzinfo=timezone.utc)
_DATE_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}|[0-9]{4}-W[0-9]{2}(?:-[1-7])?|[0-9]{4}W[0-9]{2}[1-7]?)"
    r"(?:[^Zz](.+))?"
)
_WEEK_DATE = re.compile(r"([0-9]{4})-?W([0-9]{2})(?:-?([1-7]))?")
_CLOCK = re.compile(r"([0-9]{2})(?:(:?)([0-9]{2})(?:\2([0-9]{2}))?)?(?:[.,]([0-9]+))?")
_TIME_OFFSET = re.compile(r"(.*?)(Z|z|[+-].*)?")


def _clock_microseconds(raw: str, *, offset: bool) -> int | None:
    match = _CLOCK.fullmatch(raw)
    if match is None:
        return None
    hour, _, minute, second, fraction = match.groups()
    hours, minutes, seconds = int(hour), int(minute or 0), int(second or 0)
    if not offset and (hours > 23 or minutes > 59 or seconds > 59):
        return None
    total_seconds = hours * 3600 + minutes * 60 + seconds
    micros = total_seconds * 1000000 + int((fraction or "").ljust(6, "0")[:6])
    if offset and micros >= 86400000000:
        return None
    # Retain the protocol's zero-offset rule, independent of Python's version.
    return 0 if offset and total_seconds == 0 else micros


def parse_timestamp(value: Any) -> datetime | None:
    """Parse the stable Todo/runtime ISO grammar (see monitor-configuration).

    Missing zones mean UTC; fractions are seconds, truncated to microseconds.
    Do not use fromisoformat: its input language changes between Python minors.
    """
    if not value:
        return None
    text = str(value).strip()
    match = _DATE_TIME.fullmatch(text)
    if match is None:
        return None
    date, time = match.groups()
    try:
        week = _WEEK_DATE.fullmatch(date)
        if week is not None:
            year, number, day = week.groups()
            calendar = datetime.fromisocalendar(int(year), int(number), int(day or 1))
        else:
            digits = date.replace("-", "")
            calendar = datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        calendar = calendar.replace(tzinfo=timezone.utc)
        if time is None:
            return calendar
        parts = _TIME_OFFSET.fullmatch(time)
        if parts is None:
            return None
        clock, zone = parts.groups()
        local = _clock_microseconds(clock, offset=False)
        if local is None:
            return None
        offset = 0
        if zone and zone not in {"Z", "z"}:
            parsed_offset = _clock_microseconds(zone[1:], offset=True)
            if parsed_offset is None:
                return None
            offset = -parsed_offset if zone[0] == "-" else parsed_offset
        return calendar + timedelta(microseconds=local - offset)
    except (ValueError, OverflowError):
        return None


def chronology_key(value: Any) -> tuple[int, datetime, str]:
    """Order timestamps by UTC instant with deterministic legacy fallbacks."""

    raw = str(value or "")
    try:
        parsed = parse_timestamp(value)
    except OverflowError:
        # UTC conversion can overflow at datetime's representable boundaries.
        parsed = None
    if parsed is None:
        return (0, _MIN_TIMESTAMP, raw)
    return (1, parsed, raw)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_utc_iso() -> str:
    return utc_isoformat(now_utc())


def now_local_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def utc_isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_timestamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")
