"""
Tests for MockCalendarClient.

CRUD correctness is necessary but not the interesting part — the interesting
part is `check_availability`'s overlap logic, especially the back-to-back
boundary case (an event ending exactly when another starts should NOT count
as a conflict). That's the specific off-by-one this module is built to get
right, so it gets its own explicit test rather than being implied by a
general overlap test.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.integrations.calendar_client import (
    MockCalendarClient,
    CalendarEvent,
    CalendarEventNotFoundError,
)


def dt(hour, minute=0, day=15):
    return datetime(2026, 8, day, hour, minute)


def make_client():
    return MockCalendarClient()


# ---- CRUD correctness ----


def test_create_event_returns_event_with_id():
    client = make_client()
    event = client.create_event("Standup", dt(9), dt(9, 30))
    assert event.id
    assert event.summary == "Standup"
    assert client.list_events(dt(0), dt(23))[0].id == event.id


def test_create_event_rejects_end_before_start():
    client = make_client()
    with pytest.raises(ValueError):
        client.create_event("Bad event", dt(10), dt(9))


def test_create_event_defaults_attendees_to_empty_list():
    client = make_client()
    event = client.create_event("Solo work block", dt(9), dt(10))
    assert event.attendees == []


def test_update_event_changes_only_specified_fields():
    client = make_client()
    event = client.create_event("Standup", dt(9), dt(9, 30), attendees=["a@x.com"])
    updated = client.update_event(event.id, summary="Renamed Standup")
    assert updated.summary == "Renamed Standup"
    assert updated.start == dt(9)
    assert updated.attendees == ["a@x.com"]


def test_update_nonexistent_event_raises():
    client = make_client()
    with pytest.raises(CalendarEventNotFoundError):
        client.update_event("does-not-exist", summary="x")


def test_update_event_rejects_unknown_field():
    client = make_client()
    event = client.create_event("Standup", dt(9), dt(9, 30))
    with pytest.raises(ValueError):
        client.update_event(event.id, location="Room 4")


def test_update_event_rejects_resulting_end_before_start():
    client = make_client()
    event = client.create_event("Standup", dt(9), dt(9, 30))
    with pytest.raises(ValueError):
        client.update_event(event.id, end=dt(8))


def test_delete_event_removes_it():
    client = make_client()
    event = client.create_event("Standup", dt(9), dt(9, 30))
    assert client.delete_event(event.id) is True
    assert client.list_events(dt(0), dt(23)) == []


def test_delete_nonexistent_event_returns_false_not_raises():
    client = make_client()
    assert client.delete_event("ghost-id") is False


def test_seed_events_populates_known_state():
    client = make_client()
    seeded = CalendarEvent(id="e1", summary="Seeded", start=dt(14), end=dt(15))
    client.seed_events([seeded])
    assert client.list_events(dt(0), dt(23)) == [seeded]


# ---- check_availability / overlap logic ----


def test_availability_true_when_no_events():
    client = make_client()
    assert client.check_availability(dt(9), dt(10)) is True


def test_availability_false_on_exact_overlap():
    client = make_client()
    client.create_event("Meeting", dt(9), dt(10))
    assert client.check_availability(dt(9), dt(10)) is False


def test_availability_false_on_partial_overlap_start():
    """New slot starts inside an existing event."""
    client = make_client()
    client.create_event("Meeting", dt(9), dt(10))
    assert client.check_availability(dt(9, 30), dt(10, 30)) is False


def test_availability_false_on_partial_overlap_end():
    """New slot ends inside an existing event."""
    client = make_client()
    client.create_event("Meeting", dt(9), dt(10))
    assert client.check_availability(dt(8, 30), dt(9, 30)) is False


def test_availability_false_when_new_slot_fully_contains_existing_event():
    client = make_client()
    client.create_event("Meeting", dt(9, 15), dt(9, 45))
    assert client.check_availability(dt(9), dt(10)) is False


def test_availability_false_when_new_slot_fully_inside_existing_event():
    client = make_client()
    client.create_event("Long meeting", dt(9), dt(12))
    assert client.check_availability(dt(10), dt(11)) is False


def test_back_to_back_events_do_not_count_as_unavailable():
    """
    The explicit edge case called out in the spec: an event ending exactly
    when another starts should NOT count as unavailable. Half-open interval
    semantics — [start, end) — get this right; a naive <=/>= comparison
    would wrongly flag this as a conflict.
    """
    client = make_client()
    client.create_event("Morning meeting", dt(9), dt(10))
    assert client.check_availability(dt(10), dt(11)) is True
    assert client.check_availability(dt(8), dt(9)) is True


def test_availability_unaffected_by_events_outside_window():
    client = make_client()
    client.create_event("Unrelated meeting", dt(14), dt(15))
    assert client.check_availability(dt(9), dt(10)) is True


def test_list_events_excludes_non_overlapping_events():
    client = make_client()
    client.create_event("Morning", dt(9), dt(10))
    client.create_event("Afternoon", dt(14), dt(15))
    results = client.list_events(dt(9), dt(10))
    assert len(results) == 1
    assert results[0].summary == "Morning"
