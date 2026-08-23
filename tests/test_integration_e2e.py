"""
Full scripted end-to-end scenario, touching every agent built for this
project. Uses fake, canned LLM responses throughout (deterministic, no live
API calls) — the point of this test is proving the modules are correctly
wired together, not re-testing LLM call correctness (that's covered per-
module elsewhere).

Scenario (asserting on state at each stage, not just the final output):
  1. Seed the graph with a person and an existing meeting; seed the calendar
     with the matching event.
  2. Request a new meeting with that person at the same time as the existing
     one -> Scheduler must detect the conflict itself and refuse to create.
  3. Planner is invoked to reconsider given the conflict, proposing a
     rescheduled time.
  4. Scheduler retries at the new time -> Guardrail flags REQUIRE_APPROVAL
     (WRITE severity) -> assert the calendar is NOT modified yet.
  5. Simulate approval -> assert the event now exists in the mock calendar.
  6. The new meeting is written into graph memory via the Memory Agent's
     remember() (also Guardrail-gated) and approved.
  7. Memory Agent query -> assert it answers via a real graph traversal
     through GraphMemoryStore.neighbors(), not a hardcoded string.

Note on step 7's exact phrasing: GraphMemoryStore.neighbors() only follows
outgoing edges (by design — see graph_store.py), and this project's ATTENDED
edge convention (established in test_memory_graph.py) is person -> meeting.
Answering "who is at my meeting" therefore has to start the traversal from
the known person, not from an implicit "meeting" or "self" node — so the
question here is phrased "what meeting do I have with Arya tomorrow" rather
than "who's in my meeting tomorrow". Same underlying capability (a real
1-hop traversal proving the write actually landed), phrased so it's
answerable with the store's actual, protected read interface.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agents.guardrail import Verdict
from src.agents.memory_agent import MemoryAgent
from src.agents.planner import create_plan
from src.agents.scheduler import SchedulerAgent
from src.integrations.calendar_client import MockCalendarClient
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Edge, EdgeType, Node, NodeType


class ScriptedLLMClient:
    """
    Returns a different canned response each call, in order — models a real
    conversation where each LLM call is answering a different, specific
    prompt, without needing a live API.
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = []

    def complete(self, prompt: str, system=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if not self._responses:
            raise AssertionError(
                f"ScriptedLLMClient ran out of canned responses on prompt: {prompt!r}"
            )
        return self._responses.pop(0)


REFERENCE_NOW = datetime(2026, 8, 23, 8, 0)  # "today"; "tomorrow" resolves to 2026-08-24
TOMORROW_9AM = datetime(2026, 8, 24, 9, 0)
TOMORROW_930AM = datetime(2026, 8, 24, 9, 30)
TOMORROW_10AM = datetime(2026, 8, 24, 10, 0)
TOMORROW_1030AM = datetime(2026, 8, 24, 10, 30)


def test_full_scripted_scenario():
    # ---- Stage 1: seed graph + calendar with a person and existing meeting ----
    memory_store = GraphMemoryStore()
    memory_store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    memory_store.add_node(
        Node(id="meeting:standup", type=NodeType.MEETING, label="Existing Standup")
    )
    memory_store.add_edge(
        Edge(source_id="person:arya", target_id="meeting:standup", type=EdgeType.ATTENDED)
    )

    calendar = MockCalendarClient()
    existing_event = calendar.create_event(
        "Existing Standup", TOMORROW_9AM, TOMORROW_930AM, attendees=["Arya"]
    )
    assert len(calendar._events) == 1  # sanity check on seeded state

    scheduler_llm = ScriptedLLMClient(
        [
            # Stage 2: parse "schedule a meeting with Arya tomorrow at the same
            # time as the existing standup" -> resolves to the conflicting slot.
            '{"action": "create", "title": "Sync with Arya", '
            '"start": "2026-08-24T09:00:00", "end": "2026-08-24T09:30:00", '
            '"attendees": ["Arya"], "event_id": null}',
            # Stage 4: parse the retry at the Planner-proposed alternate time.
            '{"action": "create", "title": "Sync with Arya", '
            '"start": "2026-08-24T10:00:00", "end": "2026-08-24T10:30:00", '
            '"attendees": ["Arya"], "event_id": null}',
        ]
    )
    scheduler = SchedulerAgent(scheduler_llm, calendar, memory_store=memory_store)

    # ---- Stage 2: request produces a conflict ----
    conflict_result = scheduler.handle_request(
        "Schedule a meeting with Arya tomorrow at the same time as the existing standup",
        reference_time=REFERENCE_NOW,
    )
    assert conflict_result.conflict is True
    assert conflict_result.pending_approval is False
    assert len(calendar._events) == 1  # nothing created
    assert conflict_result.events[0].id == existing_event.id

    # ---- Stage 3: Planner reconsiders given the conflict ----
    planner_llm = ScriptedLLMClient(
        [
            '[{"description": "Offer Arya an alternative time slot at 10am", '
            '"target_agent": "scheduler", "urgency": 4, "importance": 4}]'
        ]
    )
    plan = create_plan(
        "Reconsider scheduling a meeting with Arya — the requested time conflicts "
        "with an existing standup",
        planner_llm,
    )
    assert len(plan) == 1
    assert plan[0].task.target_agent == "scheduler"
    assert "10am" in plan[0].task.description or "alternative" in plan[0].task.description

    # ---- Stage 4: Scheduler retries at the Planner-proposed time -> pending approval ----
    retry_result = scheduler.handle_request(
        "Schedule a meeting with Arya tomorrow at 10am instead",
        reference_time=REFERENCE_NOW,
    )
    assert retry_result.conflict is False
    assert retry_result.pending_approval is True
    assert retry_result.guardrail_decision.verdict == Verdict.REQUIRE_APPROVAL
    assert len(calendar._events) == 1  # still just the original — nothing created yet

    # ---- Stage 5: simulate approval -> event now exists ----
    confirmed = scheduler.confirm_pending(retry_result.intent)
    assert confirmed.success is True
    assert len(calendar._events) == 2
    new_event = confirmed.event
    assert new_event.start == TOMORROW_10AM
    assert new_event.summary == "Sync with Arya"

    # ---- Stage 6: write the new meeting into graph memory, Guardrail-gated ----
    memory_llm = ScriptedLLMClient(
        [
            '{"nodes": ['
            '  {"id": "meeting:arya_sync", "type": "meeting", "label": "Sync with Arya"}'
            '], '
            '"edges": ['
            '  {"source_id": "person:arya", "target_id": "meeting:arya_sync", "type": "attended"}'
            ']}',
            # Stage 7's query prompt (issued after the write below).
            '{"operation": "neighbors", "node_id": "person:arya", "edge_type": "attended"}',
        ]
    )
    memory_agent = MemoryAgent(memory_llm, memory_store)

    write_result = memory_agent.remember(
        "I'm now meeting Arya tomorrow at 10am — call it 'Sync with Arya'."
    )
    assert write_result.pending_approval is True
    assert memory_store.get_node("meeting:arya_sync") is None  # not written yet

    committed = memory_agent.commit_pending(write_result)
    assert committed.success is True
    assert memory_store.get_node("meeting:arya_sync") is not None

    # ---- Stage 7: query answers via real graph traversal, not a hardcoded string ----
    answer = memory_agent.query("What meeting do I have with Arya tomorrow?")
    assert "Sync with Arya" in answer
    # And prove it's a real traversal by checking the store directly agrees:
    neighbors = memory_store.neighbors("person:arya", edge_type=EdgeType.ATTENDED)
    neighbor_labels = {n["label"] for n in neighbors}
    assert "Sync with Arya" in neighbor_labels
    assert "Existing Standup" in neighbor_labels  # the original meeting is still there too
