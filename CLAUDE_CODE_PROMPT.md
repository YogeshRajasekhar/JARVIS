# Claude Code Prompt — Complete the JARVIS-Style Multi-Agent Personal Assistant

Paste everything below into Claude Code, run from inside the `jarvis-assistant/` directory
(or tell it that path if starting fresh).

---

## Context

I'm continuing a multi-agent personal assistant project. Two modules are already built and
fully tested (16/16 tests passing) — **do not modify their public interfaces**, only import
and build against them. Your job is to build the remaining modules to the same standard:
real, runnable code with real tests, including adversarial/edge-case tests, not just
happy-path smoke tests.

## Already-built modules (build against these exactly as-is)

### `src/agents/guardrail.py`
```python
"""
Guardrail Agent — mediates every state-changing action before execution.

Design rationale (this is the part to be able to explain in an interview):
- This is deliberately NOT an LLM call. Risk classification here is a bounded
  categorization task (which category of action, on which class of target),
  not open-ended reasoning — a fast local classifier is both cheaper AND
  faster than an LLM API round-trip, which matters because this sits on the
  critical path before every single state-changing action.
- Real precedent: SafeClaw-R (2026) found 36.4% of a real personal-agent
  framework's built-in skills posed high/critical risk without a mediation
  layer like this one. The core design principle borrowed from that work:
  actions must be mediated PRIOR to execution, not audited after the fact.

Risk model: every action is scored on two independent axes —
  1. Action severity: what kind of action is this (read < draft < send/write < delete/financial)
  2. Target sensitivity: what is it acting on (self-only < known-contact < external/unknown)
A HIGH verdict on either axis alone is enough to require human approval —
severity and sensitivity are not averaged, because a highly sensitive
low-severity action (e.g., reading someone else's private notes) is just as
worth stopping as a high-severity action on a safe target.
"""

from dataclasses import dataclass
from enum import Enum


class ActionSeverity(Enum):
    READ = 1
    DRAFT = 2
    WRITE = 3
    IRREVERSIBLE = 4


class TargetSensitivity(Enum):
    SELF = 1
    KNOWN_CONTACT = 2
    EXTERNAL_UNKNOWN = 3


class Verdict(Enum):
    AUTO_APPROVE = "auto_approve"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass
class ActionRequest:
    agent_name: str
    action_description: str
    severity: ActionSeverity
    target_sensitivity: TargetSensitivity
    is_reversible: bool = True


@dataclass
class GuardrailDecision:
    verdict: Verdict
    reason: str
    action: ActionRequest


class GuardrailAgent:
    SEVERITY_APPROVAL_THRESHOLD = ActionSeverity.WRITE
    SENSITIVITY_APPROVAL_THRESHOLD = TargetSensitivity.EXTERNAL_UNKNOWN

    def evaluate(self, request: ActionRequest) -> GuardrailDecision:
        # ... (full logic already implemented — see repo file)
```
**Every agent that performs a WRITE or IRREVERSIBLE action MUST construct an `ActionRequest`
and call `GuardrailAgent().evaluate(...)` before executing. If the verdict is
`REQUIRE_APPROVAL`, the agent returns a pending-approval result instead of executing, and
does NOT execute until a separate, explicit approval call is made.**

### `src/memory/models.py` and `src/memory/graph_store.py`
Full contents in the repo already. Key public interface you'll use:
```python
GraphMemoryStore(persist_path: Optional[str] = None)
  .add_node(Node) -> None
  .add_edge(Edge) -> None            # raises ValueError if either node missing
  .get_node(node_id: str) -> Optional[dict]
  .neighbors(node_id: str, edge_type: Optional[EdgeType] = None) -> list[dict]
  .find_by_type(node_type: NodeType) -> list[dict]
  .path_exists(source_id: str, target_id: str) -> bool
  .save() -> None
Node(id, type: NodeType, label, attributes: dict = {})
Edge(source_id, target_id, type: EdgeType, attributes: dict = {})
NodeType: PERSON, MEETING, TASK, COMMITMENT
EdgeType: RELATES_TO, FOLLOWS_UP, BLOCKS, INTRODUCED_BY, ATTENDED, OWES
```

---

## Modules to build

### 1. `src/llm/client.py` — provider-agnostic LLM wrapper

Requirements:
- Define an abstract interface (`Protocol` or ABC) `LLMClient` with method
  `complete(self, prompt: str, system: str | None = None, **kwargs) -> str`.
- Implement `ClaudeLLMClient(LLMClient)` using the `anthropic` Python SDK. Read
  `ANTHROPIC_API_KEY` from environment (via `python-dotenv`). Constructor takes a `model`
  parameter with **no hardcoded default that silently picks the expensive tier** —
  require it to be passed explicitly, or default to the cheapest available model.
- **Cost-tiering (this matters — mirrors an explicit design decision from earlier in this
  project where GPT-4o vs GPT-4o-mini was chosen per-agent by task complexity; translate
  that same principle onto Claude's tiers since that's the provider available here):**
  - `claude-haiku-4-5-20251001` for: Scheduler intent parsing, Memory Agent NL→query
    translation, Supervisor routing. These are narrow/structured tasks, not open-ended
    reasoning.
  - `claude-sonnet-5` for: Planner/Replanner only. This is the one agent doing genuine
    multi-step reasoning under conflicting constraints (priority arbitration) — keep it
    the strongest model in the system, same reasoning as the earlier GPT-4o exception.
- Handle API errors gracefully (retries with backoff on rate limits; raise a clear custom
  exception on auth failure, don't let a raw SDK exception leak up unhandled).
- **Tests required:** a real integration test that makes one actual live call and asserts
  on getting a non-empty string response (mark it `@pytest.mark.integration` so it can be
  skipped when no API key is present); unit tests using a fake/mocked client for the
  system-prompt and parameter-passing logic that don't need network access.

### 2. `src/integrations/calendar_client.py` — mock Calendar, real interface

Requirements:
- Abstract interface `CalendarClient`:
  ```python
  list_events(start: datetime, end: datetime) -> list[CalendarEvent]
  check_availability(start: datetime, end: datetime) -> bool
  create_event(title: str, start: datetime, end: datetime, attendees: list[str] = []) -> CalendarEvent
  update_event(event_id: str, **changes) -> CalendarEvent
  delete_event(event_id: str) -> bool
  ```
  Shape this to match Google Calendar API v3's actual event fields reasonably closely
  (id, summary/title, start, end, attendees) so swapping in the real client later is a
  drop-in replacement, not a rewrite.
- `MockCalendarClient(CalendarClient)`: in-memory dict-backed implementation, seeded with
  a `seed_events()` method for tests to populate known state.
- **Tests required:** CRUD correctness, `check_availability` correctly detects overlap
  including partial overlaps and back-to-back non-overlapping events (edge case: an event
  ending exactly when another starts should NOT count as unavailable — assert this
  explicitly, it's an easy off-by-one to get wrong).

### 3. `src/agents/scheduler.py`

Requirements:
- Takes an `LLMClient` (haiku tier) and a `CalendarClient` (mock) in its constructor.
- `handle_request(natural_language_request: str) -> SchedulerResult` — parses intent via
  LLM into a structured action (check/create/update/delete), executes against the calendar
  client.
- **Every create/update/delete MUST go through `GuardrailAgent.evaluate()` first** with
  `severity=WRITE` (or `IRREVERSIBLE` for delete) and appropriate `target_sensitivity`
  based on whether attendees are known contacts (look up via `GraphMemoryStore`) or not.
  If the verdict is `REQUIRE_APPROVAL`, return a `SchedulerResult` with a
  `pending_approval=True` flag and do NOT call the calendar client yet.
- **Tests required:** parsing correctness with a mocked LLM response (deterministic,
  no live API call needed for this), guardrail integration (assert a create-event request
  does NOT hit the calendar client until approved), availability-conflict handling.

### 4. `src/agents/memory_agent.py`

Requirements:
- Takes an `LLMClient` (haiku tier) and a `GraphMemoryStore`.
- `query(natural_language_question: str) -> str` — translates the question into a
  **structured intermediate representation** (e.g., a small JSON: `{"operation":
  "neighbors", "node_id": "...", "edge_type": "..."}`) via LLM, then executes that
  structured call deterministically against `GraphMemoryStore` — **do NOT let the LLM
  generate and execute arbitrary code or unvalidated queries against the store.** Validate
  the LLM's structured output against an allow-list of operations before executing.
- `remember(fact_description: str) -> MemoryWriteResult` — parses a fact into
  Node/Edge objects and writes them. This is a WRITE action — **must go through
  GuardrailAgent** the same way Scheduler does.
- **Tests required:** structured-output validation rejects malformed/out-of-allowlist LLM
  output rather than crashing or executing it; the actual multi-hop query types from the
  existing graph tests (e.g., "who did I meet through X") answered correctly end-to-end
  through this agent, not just at the store level.

### 5. `src/agents/planner.py` — Plan-and-Execute with priority-based replanning

Requirements:
- `Task` dataclass: `id, description, target_agent, urgency (1-5), importance (1-5)`,
  computed `priority = urgency * importance`.
- `PlanStep`: wraps a `Task` with status (`pending/in_progress/done/interrupted`).
- `create_plan(goal: str, llm_client) -> list[PlanStep]` — LLM (sonnet tier) decomposes a
  goal into ordered steps.
- `execute_next_step(plan: list[PlanStep]) -> PlanStep` — advances the plan.
- `interrupt_and_replan(current_plan: list[PlanStep], new_task: Task) -> list[PlanStep]` —
  **this is the core logic, make it deterministic and directly testable without requiring
  an LLM call for the interrupt decision itself:** if `new_task.priority` exceeds the
  priority of the currently in-progress step, mark that step `interrupted`, insert the new
  task at the front of the remaining queue, re-sort remaining pending steps by priority
  descending. If not higher priority, append to the end of the queue instead.
- **Tests required (this is the module where adversarial/edge-case tests matter most,
  matching the Guardrail's testing standard):**
  - New task with equal priority to current step does NOT interrupt (defines the
    tie-breaking behavior explicitly rather than leaving it ambiguous).
  - New task arrives when plan is already empty/complete — handled without error.
  - Multiple successive interrupts (a new higher-priority task arrives while another
    interrupt is already pending) — verify the plan ends up correctly ordered, not just
    that it doesn't crash.
  - A task interrupted once, then never resumed because something even higher-priority
    keeps arriving — assert it's still present in the plan (not silently dropped) with
    status `interrupted`, so nothing the user asked for disappears.

### 6. `src/supervisor.py` — LangGraph wiring

Requirements:
- Use `langgraph`'s `StateGraph`. Define a shared state schema (conversation history,
  pending guardrail approvals, active plan).
- Router node uses the LLM client (haiku tier) to classify incoming requests to one of:
  scheduler / memory / planner, and invokes the corresponding agent node.
- Include a node for handling pending Guardrail approvals (surfacing them back to the
  user, accepting yes/no, then executing or discarding the held action).
- **Tests required:** a routing-correctness test (given N sample requests, each routes to
  the expected agent — use a mocked LLM for determinism), and one real end-to-end test
  using the actual Claude-backed LLM client (marked `@pytest.mark.integration`).

### 7. `tests/test_integration_e2e.py` — the full scripted scenario

Build this exact scenario end-to-end, asserting on the state at each stage, not just the
final output:
1. Seed the graph with a person and an existing meeting.
2. Request: "Schedule a meeting with [that person] tomorrow at the same time as
   [existing meeting]" — should produce a conflict.
3. Planner should be invoked to reconsider, given the conflict.
4. Scheduler produces a create-event action → Guardrail flags it `REQUIRE_APPROVAL`
   (WRITE severity) → assert the calendar was NOT modified yet.
5. Simulate approval → assert the event now exists in the mock calendar.
6. Memory Agent query: "who do I have a meeting with tomorrow" → assert it returns the
   newly created meeting via a real graph traversal, not a hardcoded string.

---

## Non-negotiable standards for everything you write

- Every module gets docstrings explaining **why**, not just what — match the style already
  in `guardrail.py` and `models.py` (design rationale, not just parameter descriptions).
  This project is being used to learn from before a technical interview, so the reasoning
  needs to be visible in the code, not just correct.
- No bare `except:` clauses. No silent failures — a failed guardrail check, a failed API
  call, a malformed LLM response must all produce an explicit, typed result or exception,
  never a swallowed error.
- Every new external dependency goes into `requirements.txt` with a pinned version.
- Update `.env.example` with any new required environment variables.
- After writing each module, **run its test file and show me the actual pytest output** —
  don't report success without showing the real output, the same standard already used
  for `guardrail.py` and `graph_store.py`.
- At the end, run the full suite (`pytest tests/ -v`) and report the final pass count.
- Update `README.md`'s "what's real vs mocked" table to reflect the final state.
