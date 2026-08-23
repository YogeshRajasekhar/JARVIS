"""
Calendar client — abstract interface + an in-memory mock implementation.

Design rationale:
- The interface is shaped to match Google Calendar API v3's actual event
  fields (id, summary, start/end as RFC3339-ish datetimes, attendees) on
  purpose, not because it happens to be convenient here. The goal is that
  swapping `MockCalendarClient` for a real `GoogleCalendarClient` later is a
  constructor-line change in the Scheduler agent, not a rewrite of every
  caller — the same "interface first, mock behind it" pattern as `LLMClient`.
- `check_availability` treats the interval as half-open, [start, end). An
  event ending at 10:00 and another starting at 10:00 do NOT overlap. This
  is the correct calendar semantics (that's how every real calendar app
  treats back-to-back meetings) and it's also the classic off-by-one trap,
  so it's asserted explicitly in tests rather than left to whatever the
  comparison operators happen to do.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4


@dataclass
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """Half-open interval overlap check — see module docstring."""
        return self.start < end and start < self.end


class CalendarClient(ABC):
    """Abstract calendar interface. See module docstring for why it's shaped this way."""

    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """All events overlapping [start, end)."""

    @abstractmethod
    def check_availability(self, start: datetime, end: datetime) -> bool:
        """True if [start, end) has no overlapping events."""

    @abstractmethod
    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: Optional[list[str]] = None,
    ) -> CalendarEvent:
        ...

    @abstractmethod
    def update_event(self, event_id: str, **changes) -> CalendarEvent:
        ...

    @abstractmethod
    def delete_event(self, event_id: str) -> bool:
        """True if an event was deleted, False if event_id didn't exist."""


class CalendarEventNotFoundError(Exception):
    """Raised by update/delete when event_id doesn't exist — never silently no-ops."""


class MockCalendarClient(CalendarClient):
    """
    In-memory dict-backed calendar. Deterministic, no I/O — safe to construct
    fresh per test with no shared state, and `seed_events()` lets tests set
    up known state without going through `create_event` (so calendar-write
    tests can assert `create_event` was or wasn't called, independent of
    setup).
    """

    def __init__(self):
        self._events: dict[str, CalendarEvent] = {}

    def seed_events(self, events: list[CalendarEvent]) -> None:
        for event in events:
            self._events[event.id] = event

    def list_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [e for e in self._events.values() if e.overlaps(start, end)]

    def check_availability(self, start: datetime, end: datetime) -> bool:
        return len(self.list_events(start, end)) == 0

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: Optional[list[str]] = None,
    ) -> CalendarEvent:
        if end <= start:
            raise ValueError(f"Event end ({end}) must be after start ({start}).")
        event = CalendarEvent(
            id=str(uuid4()),
            summary=title,
            start=start,
            end=end,
            attendees=list(attendees) if attendees else [],
        )
        self._events[event.id] = event
        return event

    def update_event(self, event_id: str, **changes) -> CalendarEvent:
        if event_id not in self._events:
            raise CalendarEventNotFoundError(f"No event with id {event_id!r}.")
        existing = self._events[event_id]
        allowed_fields = {"summary", "start", "end", "attendees"}
        unknown = set(changes) - allowed_fields
        if unknown:
            raise ValueError(f"Unknown event field(s): {sorted(unknown)}")
        updated = CalendarEvent(
            id=existing.id,
            summary=changes.get("summary", existing.summary),
            start=changes.get("start", existing.start),
            end=changes.get("end", existing.end),
            attendees=changes.get("attendees", existing.attendees),
        )
        if updated.end <= updated.start:
            raise ValueError(f"Event end ({updated.end}) must be after start ({updated.start}).")
        self._events[event_id] = updated
        return updated

    def delete_event(self, event_id: str) -> bool:
        if event_id not in self._events:
            return False
        del self._events[event_id]
        return True
