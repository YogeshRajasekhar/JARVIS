"""
Deterministic natural-language time resolution.

Small LLMs are bad at date arithmetic ("next Friday" from a Saturday, month rollovers), so
the model only copies the user's phrase and this module turns it into a datetime. Fully
testable, no network, same answer every time.

resolve("tomorrow 5pm", now)  -> Resolved(start=..., has_time=True)
resolve("friday", now)        -> Resolved(start=friday 00:00, has_time=False)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Optional

import dateparser

def _sanitize_host_tz() -> None:
    """dateparser -> tzlocal crashes on legacy TZ names (e.g. 'Asia/Calcutta'). Normalise once."""
    import os
    aliases = {"Asia/Calcutta": "Asia/Kolkata", "Asia/Saigon": "Asia/Ho_Chi_Minh"}
    tz = os.environ.get("TZ")
    if tz in aliases:
        os.environ["TZ"] = aliases[tz]
    try:
        import tzlocal
        tzlocal.get_localzone()
    except Exception:
        os.environ["TZ"] = "UTC"


_sanitize_host_tz()

PARTS_OF_DAY = {
    "early morning": (7, 0), "morning": (9, 0), "noon": (12, 0), "midday": (12, 0),
    "afternoon": (14, 0), "evening": (18, 0), "tonight": (20, 0), "night": (20, 0),
    "eod": (17, 0), "end of day": (17, 0),
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_HOUR_ONLY = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$", re.I)
_TIME_IN = re.compile(r"(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$)", re.I)
_IN_DURATION = re.compile(r"^in\s+(\d+)\s*(minute|min|hour|hr|day)s?$", re.I)


@dataclass
class Resolved:
    start: datetime
    has_time: bool
    part_of_day: Optional[str] = None


def _ampm(hour: int, minute: int, suffix: Optional[str], now: datetime, same_day: bool) -> tuple[int, int]:
    if suffix:
        s = suffix.lower().replace(".", "")
        if s == "pm" and hour < 12:
            hour += 12
        if s == "am" and hour == 12:
            hour = 0
        return hour, minute
    # No am/pm: people mean working/waking hours. 1-7 -> afternoon/evening.
    if 1 <= hour <= 7:
        hour += 12
    return hour, minute




def resolve(phrase: Optional[str], now: datetime, prefer_morning: bool = False) -> Optional[Resolved]:
    """prefer_morning: a bare '6:30' means 06:30 (alarms), not 18:30 (meetings)."""
    if not phrase:
        return None
    text = phrase.strip().lower()
    text = re.sub(r"\s+", " ", text).rstrip(".?!")
    if not text:
        return None

    m = _IN_DURATION.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n), "hour": timedelta(hours=n),
                 "hr": timedelta(hours=n), "day": timedelta(days=n)}[unit]
        return Resolved((now + delta).replace(second=0, microsecond=0), True)

    absolute = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}", text)
    if absolute:
        return _absolute(phrase, now)

    # ---- day part ----
    day: Optional[datetime] = None
    rest = text
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\bday after tomorrow\b", rest):
        day, rest = base + timedelta(days=2), re.sub(r"\bday after tomorrow\b", "", rest)
    elif re.search(r"\b(tomorrow|tmrw|tmr)\b", rest):
        day, rest = base + timedelta(days=1), re.sub(r"\b(tomorrow|tmrw|tmr)\b", "", rest)
    elif re.search(r"\btoday\b", rest):
        day, rest = base, re.sub(r"\btoday\b", "", rest)
    elif re.search(r"\btonight\b", rest):
        day = base
    else:
        wd = re.search(r"\b(next|this|coming)?\s*(" + "|".join(WEEKDAYS) + r")\b", rest)
        if wd:
            target = WEEKDAYS.index(wd.group(2))
            days = (target - now.weekday()) % 7
            if wd.group(1) == "next" and days == 0:
                days = 7
            day = base + timedelta(days=days)
            rest = rest.replace(wd.group(0), "")
        elif re.search(r"\bnext week\b", rest):
            day = base + timedelta(days=(7 - now.weekday()))  # next Monday
            rest = re.sub(r"\bnext week\b", "", rest)
    rest = rest.strip(" ,")

    # ---- time part ----
    part = None
    for p in sorted(PARTS_OF_DAY, key=len, reverse=True):
        if re.search(r"\b" + re.escape(p) + r"\b", rest) or (p == "tonight" and "tonight" in text):
            part = p
            rest = re.sub(r"\b" + re.escape(p) + r"\b", "", rest).strip(" ,")
            break
    rest = re.sub(r"\b(in the|at|on|by|around|about)\b", " ", rest).strip(" ,")
    rest = re.sub(r"\s+", " ", rest)

    hm = None
    tm = None
    for cand in _TIME_IN.finditer(rest):
        if cand.group(3) or cand.group(2) or len(rest.strip()) == len(cand.group(0).strip()):
            tm = cand
            break
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2) or 0)
        if hour <= 23 and minute <= 59:
            suffix = tm.group(3)
            if not suffix and part in ("evening", "tonight", "night", "afternoon") and hour < 12:
                suffix = "pm"
            if not suffix and part in ("morning", "early morning"):
                suffix = "am"
            if not suffix and prefer_morning and hour < 12:
                suffix = "am"
            hm = _ampm(hour, minute, suffix, now, same_day=day is None or day == base)
            rest = rest.replace(tm.group(0), "").strip(" ,")
    elif part:
        hm = PARTS_OF_DAY[part]

    if day is not None or hm is not None:
        if day is None:
            day = base
            if hm and time(*hm) <= now.time():
                day = base + timedelta(days=1)  # "at 9" when it's already 10 -> tomorrow 9
        if hm:
            return Resolved(day.replace(hour=hm[0], minute=hm[1]), True, part)
        return Resolved(day, False, part)

    return _absolute(phrase, now)


def _absolute(phrase: str, now: datetime) -> Optional[Resolved]:
    """Absolute dates ("25 oct", "nov 3 at 2pm", "2026-11-03 14:00") via dateparser."""
    try:
        # Relative phrases are handled above, so only absolute parsers are enabled here
        # (this also avoids dateparser's dependency on the host's local timezone).
        parsed = dateparser.parse(
            phrase,
            languages=["en"],
            settings={"RELATIVE_BASE": now.replace(tzinfo=None), "PREFER_DATES_FROM": "future",
                      "RETURN_AS_TIMEZONE_AWARE": False,
                      "PARSERS": ["timestamp", "custom-formats", "absolute-time"]},
        )
    except Exception:
        return None
    if parsed is None:
        return None
    has_time = bool(re.search(r"\d{1,2}:\d{2}|\d\s*(am|pm)\b", phrase.lower()))
    if not has_time:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return Resolved(parsed.replace(second=0, microsecond=0, tzinfo=now.tzinfo), has_time)


def fmt_time(dt: datetime, now: Optional[datetime] = None) -> str:
    """Human, speakable time: 'today at 5:00 PM', 'tomorrow at 9:30 AM', 'Friday 31 Oct at 2:00 PM'."""
    now = now or datetime.now(dt.tzinfo)
    clock = dt.strftime("%I:%M %p").lstrip("0")
    d = (dt.date() - now.date()).days
    if d == 0:
        return f"today at {clock}"
    if d == 1:
        return f"tomorrow at {clock}"
    if 1 < d < 7:
        return f"{dt.strftime('%A')} at {clock}"
    return f"{dt.strftime('%a %d %b')} at {clock}"
