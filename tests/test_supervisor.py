"""
Tests for the Supervisor's LangGraph wiring.

Two required test categories per the module spec: a routing-correctness
test across several sample requests with a mocked LLM (deterministic), and
one real end-to-end test using the actual Claude-backed client (marked
integration, skipped without a live key).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.agents.memory_agent import MemoryAgent
from src.agents.scheduler import SchedulerAgent
from src.integrations.calendar_client import MockCalendarClient
from src.llm.client import ClaudeLLMClient, HAIKU_MODEL, SONNET_MODEL
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Node, NodeType
from src.supervisor import Supervisor


class FakeLLMClient:
    """Returns a canned response; can be reconfigured mid-test via .response."""

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, prompt: str, system=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self.response


CHECK_JSON = (
    '{"action": "check", "title": null, "start": "2026-08-24T09:00:00", '
    '"end": "2026-08-24T10:00:00", "attendees": [], "event_id": null}'
)
CREATE_JSON = (
    '{"action": "create", "title": "Team sync", "start": "2026-08-24T09:00:00", '
    '"end": "2026-08-24T09:30:00", "attendees": [], "event_id": null}'
)
QUERY_JSON = '{"operation": "find_by_type", "node_type": "person"}'
PLAN_JSON = (
    '[{"description": "check availability", "target_agent": "scheduler", '
    '"urgency": 3, "importance": 3}]'
)


def make_supervisor(router_response, scheduler_llm_response=None, memory_llm_response=None, planner_llm_response=None):
    router_llm = FakeLLMClient(router_response)
    planner_llm = FakeLLMClient(planner_llm_response or PLAN_JSON)
    scheduler = SchedulerAgent(FakeLLMClient(scheduler_llm_response or CHECK_JSON), MockCalendarClient())
    memory = MemoryAgent(FakeLLMClient(memory_llm_response or QUERY_JSON), GraphMemoryStore())
    return Supervisor(router_llm, planner_llm, scheduler, memory), router_llm, scheduler, memory


# ---- Routing correctness (mocked LLM, deterministic) ----


@pytest.mark.parametrize(
    "request_text, router_response, expected_route",
    [
        ("Am I free tomorrow at 9am?", "scheduler", "scheduler"),
        ("Schedule a meeting with the team", "scheduler", "scheduler"),
        ("Who did I meet through Arya?", "memory", "memory"),
        ("Remember that Rohan owes me a favor", "memory", "memory"),
        ("Help me figure out how to handle three conflicting deadlines", "planner", "planner"),
    ],
)
def test_routes_sample_requests_to_expected_agent(request_text, router_response, expected_route):
    sup, router_llm, scheduler, memory = make_supervisor(router_response)
    sup.handle_message(request_text)
    assert sup.last_route == expected_route


def test_router_receives_the_user_input_as_prompt():
    sup, router_llm, scheduler, memory = make_supervisor("scheduler")
    sup.handle_message("Am I free tomorrow?")
    assert router_llm.calls[0]["prompt"] == "Am I free tomorrow?"
    assert "scheduler" in router_llm.calls[0]["system"]


def test_invalid_route_produces_explicit_error_not_a_crash():
    """Adversarial: router hallucinates something outside the allow-list."""
    sup, router_llm, scheduler, memory = make_supervisor("send_a_rocket_to_mars")
    result = sup.handle_message("do something weird")
    assert "couldn't route" in result.lower()


# ---- Scheduler routing behavior through the supervisor ----


def test_scheduler_route_runs_check_and_returns_message():
    sup, *_ = make_supervisor("scheduler", scheduler_llm_response=CHECK_JSON)
    result = sup.handle_message("am I free tomorrow at 9?")
    assert "available" in result.lower()
    assert sup.pending_approval is None


def test_scheduler_route_create_sets_pending_approval():
    sup, router_llm, scheduler, memory = make_supervisor("scheduler", scheduler_llm_response=CREATE_JSON)
    sup.handle_message("schedule a team sync tomorrow 9-9:30")
    assert sup.pending_approval is not None
    assert sup.pending_approval["agent"] == "scheduler"
    assert len(scheduler.calendar_client._events) == 0


def test_approving_pending_scheduler_action_executes_it():
    sup, router_llm, scheduler, memory = make_supervisor("scheduler", scheduler_llm_response=CREATE_JSON)
    sup.handle_message("schedule a team sync tomorrow 9-9:30")
    assert sup.pending_approval is not None

    result = sup.respond_to_approval(True)

    assert "created" in result.lower()
    assert len(scheduler.calendar_client._events) == 1
    assert sup.pending_approval is None


def test_rejecting_pending_scheduler_action_discards_it():
    sup, router_llm, scheduler, memory = make_supervisor("scheduler", scheduler_llm_response=CREATE_JSON)
    sup.handle_message("schedule a team sync tomorrow 9-9:30")

    result = sup.respond_to_approval(False)

    assert "discarded" in result.lower()
    assert len(scheduler.calendar_client._events) == 0
    assert sup.pending_approval is None


def test_entry_routes_to_approval_node_bypassing_router_when_pending():
    """
    While an approval is pending, the router LLM must not be re-invoked —
    the graph's conditional entry point should send state straight to the
    approval node.
    """
    sup, router_llm, scheduler, memory = make_supervisor("scheduler", scheduler_llm_response=CREATE_JSON)
    sup.handle_message("schedule a team sync tomorrow 9-9:30")
    calls_before = len(router_llm.calls)

    sup.respond_to_approval(True)

    assert len(router_llm.calls) == calls_before  # router never called again


def test_respond_to_approval_with_nothing_pending_is_handled_gracefully():
    sup, *_ = make_supervisor("scheduler")
    result = sup.respond_to_approval(True)
    assert "nothing pending" in result.lower()


# ---- Memory routing behavior through the supervisor ----


def test_memory_route_read_request_queries_not_writes():
    store = GraphMemoryStore()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    sup, router_llm, scheduler, memory = make_supervisor("memory", memory_llm_response=QUERY_JSON)
    memory.memory_store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))

    result = sup.handle_message("who do I know?")

    assert "Arya" in result
    assert sup.pending_approval is None


def test_memory_route_remember_request_sets_pending_approval():
    sup, router_llm, scheduler, memory = make_supervisor(
        "memory",
        memory_llm_response='{"nodes": [{"id": "person:rohan", "type": "person", "label": "Rohan"}], "edges": []}',
    )
    sup.handle_message("Remember that I met Rohan today")
    assert sup.pending_approval is not None
    assert sup.pending_approval["agent"] == "memory"
    assert memory.memory_store.get_node("person:rohan") is None


def test_approving_pending_memory_write_commits_it():
    sup, router_llm, scheduler, memory = make_supervisor(
        "memory",
        memory_llm_response='{"nodes": [{"id": "person:rohan", "type": "person", "label": "Rohan"}], "edges": []}',
    )
    sup.handle_message("Remember that I met Rohan today")
    result = sup.respond_to_approval(True)
    assert "remembered" in result.lower()
    assert memory.memory_store.get_node("person:rohan") is not None


# ---- Planner routing behavior through the supervisor ----


def test_planner_route_creates_a_plan():
    sup, *_ = make_supervisor("planner", planner_llm_response=PLAN_JSON)
    result = sup.handle_message("help me juggle three urgent things")
    assert "Plan:" in result
    assert sup.active_plan is not None
    assert len(sup.active_plan) == 1


def test_planner_route_reconsiders_existing_plan_on_new_conflicting_goal():
    high_priority_plan_json = (
        '[{"description": "handle urgent conflict", "target_agent": "scheduler", '
        '"urgency": 5, "importance": 5}]'
    )
    sup, router_llm, scheduler, memory = make_supervisor("planner", planner_llm_response=PLAN_JSON)
    sup.handle_message("first goal")
    first_plan = sup.active_plan
    # First step starts in_progress via a manual nudge simulating execute_next_step,
    # so the second goal has something to actually interrupt.
    from src.agents.planner import StepStatus
    first_plan[0].status = StepStatus.IN_PROGRESS

    sup.planner_llm = FakeLLMClient(high_priority_plan_json)
    sup.handle_message("second, more urgent goal")

    assert len(sup.active_plan) == 2
    from src.agents.planner import StepStatus as SS
    statuses = {s.task.description: s.status for s in sup.active_plan}
    assert statuses["handle urgent conflict"] == SS.IN_PROGRESS
    assert statuses["check availability"] == SS.INTERRUPTED


# ---- Live integration test ----


@pytest.mark.integration
def test_live_end_to_end_routes_a_real_request():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping live integration test")

    router_llm = ClaudeLLMClient(model=HAIKU_MODEL)
    planner_llm = ClaudeLLMClient(model=SONNET_MODEL)
    scheduler_llm = ClaudeLLMClient(model=HAIKU_MODEL)
    memory_llm = ClaudeLLMClient(model=HAIKU_MODEL)

    scheduler = SchedulerAgent(scheduler_llm, MockCalendarClient())
    memory = MemoryAgent(memory_llm, GraphMemoryStore())
    sup = Supervisor(router_llm, planner_llm, scheduler, memory)

    result = sup.handle_message("Am I free tomorrow at 9am?")

    assert isinstance(result, str)
    assert len(result.strip()) > 0
    assert sup.last_route == "scheduler"
