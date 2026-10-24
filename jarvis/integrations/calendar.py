"""
Calendar providers: interface, in-memory mock (virtual phone / tests), Google Calendar (real).

Half-open intervals [start, end) everywhere: back-to-back meetings do not conflict.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    location: Optional[str] = None
    description: Optional[str] = None

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "attendees": self.attendees, "location": self.location}


class CalendarProvider(ABC):
    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> list[Event]: ...

    @abstractmethod
    def create(self, title: str, start: datetime, end: datetime, attendees: list[str] | None = None,
               location: Optional[str] = None, description: Optional[str] = None) -> Event: ...

    @abstractmethod
    def update(self, event_id: str, **changes) -> Event: ...

    @abstractmethod
    def delete(self, event_id: str) -> bool: ...

    def conflicts(self, start: datetime, end: datetime, ignore_id: Optional[str] = None) -> list[Event]:
        return [e for e in self.list_events(start - timedelta(days=1), end + timedelta(days=1))
                if e.overlaps(start, end) and e.id != ignore_id]

    def free_slots(self, day_start: datetime, day_end: datetime, minutes: int,
                   step_minutes: int = 30, limit: int = 3) -> list[tuple[datetime, datetime]]:
        events = sorted(self.list_events(day_start, day_end), key=lambda e: e.start)
        slots, t = [], day_start
        need = timedelta(minutes=minutes)
        while t + need <= day_end and len(slots) < limit:
            if not any(e.overlaps(t, t + need) for e in events):
                slots.append((t, t + need))
                t += need
            else:
                t += timedelta(minutes=step_minutes)
        return slots


class MockCalendar(CalendarProvider):
    def __init__(self):
        self.events: dict[str, Event] = {}

    def list_events(self, start, end):
        return sorted([e for e in self.events.values() if e.overlaps(start, end)], key=lambda e: e.start)

    def create(self, title, start, end, attendees=None, location=None, description=None):
        if end <= start:
            raise ValueError("end must be after start")
        e = Event(uuid.uuid4().hex[:8], title, start, end, list(attendees or []), location, description)
        self.events[e.id] = e
        return e

    def update(self, event_id, **changes):
        if event_id not in self.events:
            raise KeyError(f"event {event_id} not found")
        e = self.events[event_id]
        for k, v in changes.items():
            if v is not None and hasattr(e, k):
                setattr(e, k, v)
        if e.end <= e.start:
            raise ValueError("end must be after start")
        return e

    def delete(self, event_id):
        return self.events.pop(event_id, None) is not None


class GoogleCalendar(CalendarProvider):
    """
    Google Calendar API v3 via REST. Needs an OAuth token with scope
    https://www.googleapis.com/auth/calendar.events (see scripts/google_auth.py).
    """

    BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    def __init__(self, auth, tz: str = "Asia/Kolkata"):
        self.auth = auth  # jarvis.integrations.google_auth.GoogleAuth
        self.tz = tz

    def _ev(self, it: dict) -> Event:
        def parse(x):
            v = x.get("dateTime") or (x.get("date") + "T00:00:00+05:30")
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return Event(it["id"], it.get("summary", "(no title)"), parse(it["start"]), parse(it["end"]),
                     [a.get("email", "") for a in it.get("attendees", [])], it.get("location"), it.get("description"))

    def list_events(self, start, end):
        r = self.auth.session().get(self.BASE, params={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": 50})
        r.raise_for_status()
        return [self._ev(it) for it in r.json().get("items", []) if it.get("status") != "cancelled"]

    def create(self, title, start, end, attendees=None, location=None, description=None):
        body = {"summary": title, "start": {"dateTime": start.isoformat(), "timeZone": self.tz},
                "end": {"dateTime": end.isoformat(), "timeZone": self.tz}}
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees if "@" in a]
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        # sendUpdates=none: invitations are NOT emailed automatically; mail goes through approval.
        r = self.auth.session().post(self.BASE, json=body, params={"sendUpdates": "none"})
        r.raise_for_status()
        return self._ev(r.json())

    def update(self, event_id, **changes):
        body = {}
        if changes.get("title"):
            body["summary"] = changes["title"]
        if changes.get("start"):
            body["start"] = {"dateTime": changes["start"].isoformat(), "timeZone": self.tz}
        if changes.get("end"):
            body["end"] = {"dateTime": changes["end"].isoformat(), "timeZone": self.tz}
        if changes.get("location"):
            body["location"] = changes["location"]
        r = self.auth.session().patch(f"{self.BASE}/{event_id}", json=body, params={"sendUpdates": "none"})
        r.raise_for_status()
        return self._ev(r.json())

    def delete(self, event_id):
        r = self.auth.session().delete(f"{self.BASE}/{event_id}", params={"sendUpdates": "none"})
        return r.status_code in (200, 204)
