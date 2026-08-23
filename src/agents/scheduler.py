"""
Scheduler Agent — natural language -> calendar action, mediated by the Guardrail.

Design rationale:
- Intent parsing uses the haiku-tier LLM client (see src/llm/client.py):
  "what does this sentence want done to the calendar" is a narrow structured
  extraction task, not open-ended reasoning, so it doesn't need the
  expensive tier.
- The LLM's job stops at producing a structured `ParsedIntent` — a JSON
  object with an allow-listed `action` and typed fields. It never
  free-forms a response the code then tries to interpret; the parsing
  boundary is the same "validate, don't execute blindly" principle used in
  the Memory Agent for its own LLM->query step.
- Every create/update/delete goes through `GuardrailAgent.evaluate()` before
  touching the calendar client — no exceptions, no "just this once." Target
  sensitivity is derived from whether attendees are known contacts in the
  graph memory (KNOWN_CONTACT) or not (EXTERNAL_UNKNOWN); an event with no
  attendees at all only affects the user's own schedule (SELF). If the
  verdict is REQUIRE_APPROVAL, the result comes back with
  `pending_approval=True` and the calendar is NOT touched — approval is a
  separate, explicit step (`confirm_pending`), not something that happens
  by falling through.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.agents.guardrail import (
    ActionRequest,
    ActionSeverity,
    GuardrailAgent,
    TargetSensitivity,
    Verdict,
)
from src.integrations.calendar_client import CalendarClient, CalendarEvent
from src.llm.client import LLMClient
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import NodeType

# What the LLM is allowed to say it wants to do. Anything else is rejected
# before it ever reaches the calendar client.
ALLOWED_ACTIONS = {"check", "create", "update", "delete"}

INTENT_SYSTEM_PROMPT = """You are an intent parser for a calendar scheduling assistant.
Given a natural language scheduling request, respond with ONLY a JSON object
(no prose, no markdown fences) with this shape:

{
  "action": "check" | "create" | "update" | "delete",
  "title": string or null,
  "start": "YYYY-MM-DDTHH:MM:SS" or null,
  "end": "YYYY-MM-DDTHH:MM:SS" or null,
  "attendees": [string, ...],
  "event_id": string or null
}

"check" means checking availability for a time window. "create" means scheduling
a new event. "update" and "delete" require "event_id" if known, else null.
Use the reference datetime given in the prompt to resolve relative dates like
"tomorrow". Always return every key, using null/[] for anything not mentioned."""


class SchedulerParseError(Exception):
    """Raised when the LLM's response can't be parsed into a valid, allow-listed intent."""


@dataclass
class ParsedIntent:
    action: str
    title: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    attendees: list[str] = field(default_factory=list)
    event_id: Optional[str] = None


@dataclass
class SchedulerResult:
    success: bool
    message: str
    pending_approval: bool = False
    event: Optional[CalendarEvent] = None
    events: list[CalendarEvent] = field(default_factory=list)
    guardrail_decision: Optional[object] = None
    conflict: bool = False
    # Set only when pending_approval is True, so a caller (e.g. the
    # Supervisor's approval node) can later call confirm_pending(result.intent)
    # without having to re-parse the original request.
    intent: Optional[ParsedIntent] = None


def _extract_json(raw: str) -> dict:
    """
    LLMs (even when told "JSON only") sometimes wrap output in ```json fences
    or add a stray sentence. Extract the first {...} block rather than
    trusting the whole string is clean JSON.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise SchedulerParseError(f"No JSON object found in LLM response: {raw!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise SchedulerParseError(f"Malformed JSON from LLM: {e}") from e


def _parse_datetime(value: Optional[str], field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise SchedulerParseError(f"Invalid datetime for {field_name!r}: {value!r}") from e


class SchedulerAgent:
    def __init__(
        self,
        llm_client: LLMClient,
        calendar_client: CalendarClient,
        memory_store: Optional[GraphMemoryStore] = None,
    ):
        self.llm_client = llm_client
        self.calendar_client = calendar_client
        self.memory_store = memory_store
        self.guardrail = GuardrailAgent()

    # ---- Intent parsing ----

    def _parse_intent(
        self, natural_language_request: str, reference_time: Optional[datetime] = None
    ) -> ParsedIntent:
        reference_time = reference_time or datetime.now()
        prompt = (
            f"Reference datetime (now): {reference_time.isoformat()}\n"
            f"Request: {natural_language_request}"
        )
        raw = self.llm_client.complete(prompt, system=INTENT_SYSTEM_PROMPT)
        data = _extract_json(raw)

        action = data.get("action")
        if action not in ALLOWED_ACTIONS:
            raise SchedulerParseError(
                f"LLM returned action {action!r}, not in allow-list {ALLOWED_ACTIONS}."
            )

        return ParsedIntent(
            action=action,
            title=data.get("title"),
            start=_parse_datetime(data.get("start"), "start"),
            end=_parse_datetime(data.get("end"), "end"),
            attendees=list(data.get("attendees") or []),
            event_id=data.get("event_id"),
        )

    # ---- Guardrail helpers ----

    def _target_sensitivity_for(self, attendees: list[str]) -> TargetSensitivity:
        """
        No attendees -> only the user's own schedule changes (SELF).
        All attendees already known in graph memory -> KNOWN_CONTACT.
        Any attendee not in memory -> EXTERNAL_UNKNOWN (the riskier case wins,
        same "don't average risk away" principle as the Guardrail itself).
        """
        if not attendees:
            return TargetSensitivity.SELF
        if self.memory_store is None:
            return TargetSensitivity.EXTERNAL_UNKNOWN

        known_people = {
            p["label"].lower() for p in self.memory_store.find_by_type(NodeType.PERSON)
        }
        if all(attendee.lower() in known_people for attendee in attendees):
            return TargetSensitivity.KNOWN_CONTACT
        return TargetSensitivity.EXTERNAL_UNKNOWN

    def _guardrail_check(
        self, action_description: str, severity: ActionSeverity, attendees: list[str]
    ):
        request = ActionRequest(
            agent_name="scheduler",
            action_description=action_description,
            severity=severity,
            target_sensitivity=self._target_sensitivity_for(attendees),
        )
        return self.guardrail.evaluate(request)

    # ---- Public API ----

    def handle_request(
        self, natural_language_request: str, reference_time: Optional[datetime] = None
    ) -> SchedulerResult:
        try:
            intent = self._parse_intent(natural_language_request, reference_time)
        except SchedulerParseError as e:
            return SchedulerResult(success=False, message=str(e))

        if intent.action == "check":
            return self._handle_check(intent)
        if intent.action == "create":
            return self._handle_create(intent)
        if intent.action == "update":
            return self._handle_update(intent)
        if intent.action == "delete":
            return self._handle_delete(intent)

        # Unreachable given ALLOWED_ACTIONS validation in _parse_intent, but
        # no silent fallthrough — an unhandled action is a bug, not a no-op.
        raise SchedulerParseError(f"Unhandled action: {intent.action!r}")

    def _handle_check(self, intent: ParsedIntent) -> SchedulerResult:
        if intent.start is None or intent.end is None:
            return SchedulerResult(success=False, message="Check request missing start/end time.")
        available = self.calendar_client.check_availability(intent.start, intent.end)
        events = self.calendar_client.list_events(intent.start, intent.end)
        message = "Time slot is available." if available else "Time slot conflicts with existing event(s)."
        return SchedulerResult(success=True, message=message, events=events)

    def _handle_create(self, intent: ParsedIntent) -> SchedulerResult:
        if intent.start is None or intent.end is None or not intent.title:
            return SchedulerResult(
                success=False, message="Create request missing title/start/end."
            )

        # Never silently double-book: a conflicting slot is surfaced as a
        # conflict result (nothing touched, no Guardrail check even run yet)
        # rather than creating an overlapping event. Resolving the conflict
        # is the caller's job (e.g. the Planner reconsidering, or asking the
        # user for a different time) — not something this agent guesses at.
        if not self.calendar_client.check_availability(intent.start, intent.end):
            conflicting = self.calendar_client.list_events(intent.start, intent.end)
            return SchedulerResult(
                success=True,
                message="Cannot create event: time slot conflicts with existing event(s).",
                conflict=True,
                events=conflicting,
                intent=intent,
            )

        decision = self._guardrail_check(
            action_description=f"Create calendar event: {intent.title}",
            severity=ActionSeverity.WRITE,
            attendees=intent.attendees,
        )
        if decision.verdict != Verdict.AUTO_APPROVE:
            return SchedulerResult(
                success=True,
                message=f"Create event pending approval: {decision.reason}",
                pending_approval=True,
                guardrail_decision=decision,
                intent=intent,
            )

        event = self.calendar_client.create_event(
            title=intent.title, start=intent.start, end=intent.end, attendees=intent.attendees
        )
        return SchedulerResult(success=True, message=f"Event created: {event.summary}", event=event)

    def _handle_update(self, intent: ParsedIntent) -> SchedulerResult:
        if not intent.event_id:
            return SchedulerResult(success=False, message="Update request missing event_id.")

        decision = self._guardrail_check(
            action_description=f"Update calendar event {intent.event_id}",
            severity=ActionSeverity.WRITE,
            attendees=intent.attendees,
        )
        if decision.verdict != Verdict.AUTO_APPROVE:
            return SchedulerResult(
                success=True,
                message=f"Update pending approval: {decision.reason}",
                pending_approval=True,
                guardrail_decision=decision,
                intent=intent,
            )

        changes = {}
        if intent.title is not None:
            changes["summary"] = intent.title
        if intent.start is not None:
            changes["start"] = intent.start
        if intent.end is not None:
            changes["end"] = intent.end
        if intent.attendees:
            changes["attendees"] = intent.attendees

        event = self.calendar_client.update_event(intent.event_id, **changes)
        return SchedulerResult(success=True, message=f"Event updated: {event.summary}", event=event)

    def _handle_delete(self, intent: ParsedIntent) -> SchedulerResult:
        if not intent.event_id:
            return SchedulerResult(success=False, message="Delete request missing event_id.")

        decision = self._guardrail_check(
            action_description=f"Delete calendar event {intent.event_id}",
            severity=ActionSeverity.IRREVERSIBLE,
            attendees=intent.attendees,
        )
        if decision.verdict != Verdict.AUTO_APPROVE:
            return SchedulerResult(
                success=True,
                message=f"Delete pending approval: {decision.reason}",
                pending_approval=True,
                guardrail_decision=decision,
                intent=intent,
            )

        deleted = self.calendar_client.delete_event(intent.event_id)
        message = "Event deleted." if deleted else f"No event found with id {intent.event_id}."
        return SchedulerResult(success=deleted, message=message)

    def confirm_pending(self, intent: ParsedIntent) -> SchedulerResult:
        """
        Executes a previously-held action after explicit human approval.
        Deliberately bypasses the Guardrail re-check (approval already
        happened) but not the calendar client's own validation — the caller
        is responsible for only invoking this after a real approval event.
        """
        if intent.action == "create":
            if intent.start is None or intent.end is None or not intent.title:
                return SchedulerResult(success=False, message="Cannot confirm: missing fields.")
            event = self.calendar_client.create_event(
                title=intent.title, start=intent.start, end=intent.end, attendees=intent.attendees
            )
            return SchedulerResult(success=True, message=f"Event created: {event.summary}", event=event)
        if intent.action == "update":
            changes = {}
            if intent.title is not None:
                changes["summary"] = intent.title
            if intent.start is not None:
                changes["start"] = intent.start
            if intent.end is not None:
                changes["end"] = intent.end
            if intent.attendees:
                changes["attendees"] = intent.attendees
            event = self.calendar_client.update_event(intent.event_id, **changes)
            return SchedulerResult(success=True, message=f"Event updated: {event.summary}", event=event)
        if intent.action == "delete":
            deleted = self.calendar_client.delete_event(intent.event_id)
            message = "Event deleted." if deleted else f"No event found with id {intent.event_id}."
            return SchedulerResult(success=deleted, message=message)
        raise SchedulerParseError(f"confirm_pending called with non-write action: {intent.action!r}")
