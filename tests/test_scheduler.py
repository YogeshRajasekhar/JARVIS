"""
Tests for SchedulerAgent.

All tests here use a fake LLM client returning canned JSON — deterministic,
no live API call needed, per the module spec. The two things that matter
most: (1) intent parsing is correct and validated against the action
allow-list, (2) write actions never touch the calendar client until the
Guardrail has cleared them.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agents.guardrail import Verdict
from src.agents.scheduler import SchedulerAgent, ParsedIntent
from src.integrations.calendar_client import MockCalendarClient
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Node, NodeType


class FakeLLMClient:
    """Returns a pre-set canned response regardless of prompt, and records calls."""

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, prompt: str, system=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        return self.response


def make_scheduler(response: str, calendar=None, memory=None):
    llm = FakeLLMClient(response)
    calendar = calendar or MockCalendarClient()
    scheduler = SchedulerAgent(llm_client=llm, calendar_client=calendar, memory_store=memory)
    return scheduler, llm, calendar


# ---- Intent parsing correctness ----


def test_parses_check_intent():
    scheduler, llm, calendar = make_scheduler(
        '{"action": "check", "title": null, "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T10:00:00", "attendees": [], "event_id": null}'
    )
    result = scheduler.handle_request("Am I free tomorrow at 9?")
    assert result.success is True
    assert "available" in result.message.lower()


def test_parses_create_intent_correctly_but_holds_for_approval():
    """
    Per the Guardrail's own design (SEVERITY_APPROVAL_THRESHOLD = WRITE), any
    WRITE-severity action requires approval regardless of target sensitivity
    — so even a create with no attendees at all must NOT go straight to the
    calendar. The intent is still parsed correctly (title/times captured),
    it's just held pending approval rather than executed.
    """
    scheduler, llm, calendar = make_scheduler(
        '{"action": "create", "title": "Team sync", "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T09:30:00", "attendees": [], "event_id": null}'
    )
    result = scheduler.handle_request("Schedule a team sync tomorrow 9-9:30")
    assert result.success is True
    assert result.pending_approval is True
    assert result.event is None
    assert len(calendar._events) == 0
    assert result.guardrail_decision.action.action_description == "Create calendar event: Team sync"


def test_rejects_action_outside_allowlist():
    """Adversarial: LLM hallucinates an action not in the allow-list — must reject, not execute."""
    scheduler, llm, calendar = make_scheduler(
        '{"action": "reschedule_everything", "title": null, "start": null, '
        '"end": null, "attendees": [], "event_id": null}'
    )
    result = scheduler.handle_request("do something weird")
    assert result.success is False
    assert len(calendar._events) == 0


def test_rejects_malformed_json():
    scheduler, llm, calendar = make_scheduler("not json at all, sorry")
    result = scheduler.handle_request("schedule something")
    assert result.success is False


def test_rejects_json_missing_action_key():
    scheduler, llm, calendar = make_scheduler('{"title": "oops"}')
    result = scheduler.handle_request("schedule something")
    assert result.success is False


def test_extracts_json_from_markdown_fenced_response():
    """LLMs sometimes wrap JSON in code fences despite instructions not to."""
    scheduler, llm, calendar = make_scheduler(
        '```json\n{"action": "check", "title": null, "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T10:00:00", "attendees": [], "event_id": null}\n```'
    )
    result = scheduler.handle_request("am I free?")
    assert result.success is True


def test_system_prompt_is_passed_to_llm():
    scheduler, llm, calendar = make_scheduler(
        '{"action": "check", "title": null, "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T10:00:00", "attendees": [], "event_id": null}'
    )
    scheduler.handle_request("am I free?")
    assert llm.calls[0]["system"] is not None
    assert "JSON" in llm.calls[0]["system"]


# ---- Guardrail integration ----


def test_create_event_does_not_hit_calendar_until_approved():
    """
    Core guardrail-integration assertion: a create-event request with an
    unknown attendee must NOT call the calendar client — it should come back
    pending_approval, and the mock calendar must remain empty.
    """
    scheduler, llm, calendar = make_scheduler(
        '{"action": "create", "title": "Meeting with stranger", '
        '"start": "2026-08-24T09:00:00", "end": "2026-08-24T09:30:00", '
        '"attendees": ["unknown@external.com"], "event_id": null}'
    )
    result = scheduler.handle_request("meet a stranger tomorrow at 9")

    assert result.pending_approval is True
    assert result.event is None
    assert len(calendar._events) == 0
    assert result.guardrail_decision.verdict == Verdict.REQUIRE_APPROVAL


def test_create_event_with_known_contact_still_requires_approval_but_target_sensitivity_reflects_it():
    """
    A known-contact attendee should resolve to KNOWN_CONTACT target
    sensitivity (not EXTERNAL_UNKNOWN) — but since WRITE severity alone
    already crosses the Guardrail's threshold, this still requires approval.
    What this test actually verifies is that the target-sensitivity lookup
    against GraphMemoryStore ran and produced the right classification, via
    the reason string only citing severity, not target.
    """
    memory = GraphMemoryStore()
    memory.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    scheduler, llm, calendar = make_scheduler(
        '{"action": "create", "title": "1:1 with Arya", '
        '"start": "2026-08-24T09:00:00", "end": "2026-08-24T09:30:00", '
        '"attendees": ["Arya"], "event_id": null}',
        memory=memory,
    )
    result = scheduler.handle_request("meet Arya tomorrow at 9")

    assert result.pending_approval is True
    from src.agents.guardrail import TargetSensitivity
    assert result.guardrail_decision.action.target_sensitivity == TargetSensitivity.KNOWN_CONTACT
    assert "target" not in result.guardrail_decision.reason


def test_create_event_with_unknown_attendee_flags_both_severity_and_target():
    memory = GraphMemoryStore()
    memory.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    scheduler, llm, calendar = make_scheduler(
        '{"action": "create", "title": "Meeting", '
        '"start": "2026-08-24T09:00:00", "end": "2026-08-24T09:30:00", '
        '"attendees": ["Arya", "stranger@external.com"], "event_id": null}',
        memory=memory,
    )
    result = scheduler.handle_request("meet Arya and a stranger tomorrow at 9")

    from src.agents.guardrail import TargetSensitivity
    assert result.pending_approval is True
    assert result.guardrail_decision.action.target_sensitivity == TargetSensitivity.EXTERNAL_UNKNOWN
    assert "severity" in result.guardrail_decision.reason
    assert "target" in result.guardrail_decision.reason


def test_delete_event_requires_approval_even_with_no_attendees():
    """IRREVERSIBLE severity always requires approval, regardless of target sensitivity."""
    calendar = MockCalendarClient()
    existing = calendar.create_event("Old meeting", datetime(2026, 8, 24, 9), datetime(2026, 8, 24, 10))
    scheduler, llm, _ = make_scheduler(
        f'{{"action": "delete", "title": null, "start": null, "end": null, '
        f'"attendees": [], "event_id": "{existing.id}"}}',
        calendar=calendar,
    )
    result = scheduler.handle_request("delete that old meeting")

    assert result.pending_approval is True
    assert len(calendar._events) == 1  # untouched


def test_confirm_pending_actually_executes_create():
    scheduler, llm, calendar = make_scheduler("irrelevant")
    intent = ParsedIntent(
        action="create",
        title="Approved meeting",
        start=datetime(2026, 8, 24, 9),
        end=datetime(2026, 8, 24, 9, 30),
        attendees=["unknown@external.com"],
    )
    result = scheduler.confirm_pending(intent)
    assert result.success is True
    assert len(calendar._events) == 1


# ---- Availability-conflict handling ----


def test_check_reports_conflict_when_unavailable():
    calendar = MockCalendarClient()
    calendar.create_event("Existing", datetime(2026, 8, 24, 9), datetime(2026, 8, 24, 10))
    scheduler, llm, _ = make_scheduler(
        '{"action": "check", "title": null, "start": "2026-08-24T09:30:00", '
        '"end": "2026-08-24T10:30:00", "attendees": [], "event_id": null}',
        calendar=calendar,
    )
    result = scheduler.handle_request("am I free 9:30 to 10:30 tomorrow?")
    assert "conflict" in result.message.lower()
    assert len(result.events) == 1


def test_create_reports_conflict_and_does_not_touch_guardrail_or_calendar():
    """
    Create must never silently double-book: an overlapping slot comes back
    as a conflict result, with the calendar (and Guardrail) never invoked.
    """
    calendar = MockCalendarClient()
    calendar.create_event("Existing standup", datetime(2026, 8, 24, 9), datetime(2026, 8, 24, 9, 30))
    scheduler, llm, _ = make_scheduler(
        '{"action": "create", "title": "New meeting", "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T09:30:00", "attendees": [], "event_id": null}',
        calendar=calendar,
    )
    result = scheduler.handle_request("book a new meeting at 9 tomorrow")

    assert result.conflict is True
    assert result.pending_approval is False
    assert len(result.events) == 1
    assert len(calendar._events) == 1  # untouched, still just the original


def test_create_intent_is_captured_on_pending_result_for_later_confirmation():
    scheduler, llm, calendar = make_scheduler(
        '{"action": "create", "title": "Team sync", "start": "2026-08-24T09:00:00", '
        '"end": "2026-08-24T09:30:00", "attendees": [], "event_id": null}'
    )
    result = scheduler.handle_request("Schedule a team sync tomorrow 9-9:30")
    assert result.intent is not None
    assert result.intent.title == "Team sync"

    confirmed = scheduler.confirm_pending(result.intent)
    assert confirmed.success is True
    assert len(calendar._events) == 1


def test_check_missing_time_fields_fails_cleanly():
    scheduler, llm, calendar = make_scheduler(
        '{"action": "check", "title": null, "start": null, "end": null, '
        '"attendees": [], "event_id": null}'
    )
    result = scheduler.handle_request("am I free at some point?")
    assert result.success is False
