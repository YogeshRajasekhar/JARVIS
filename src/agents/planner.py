"""
Planner Agent — Plan-and-Execute with priority-based interrupt/replanning.

Design rationale:
- `create_plan` is the one place in this system that uses the sonnet tier
  (see src/llm/client.py): decomposing an ambiguous goal into an ordered
  set of steps, under constraints that might conflict, is genuine multi-step
  reasoning — the same reasoning as the earlier project-wide decision to
  keep GPT-4o (not the mini tier) for the one agent doing real arbitration
  rather than structured extraction.
- `interrupt_and_replan` is deliberately NOT an LLM call. Whether a new task
  preempts the current one is a pure function of two numbers (priority =
  urgency * importance) and is fully specified by the rule below — running
  that through an LLM would add latency and nondeterminism to a decision
  that doesn't need either. This mirrors the Guardrail's own reasoning for
  staying local rather than calling out to a model.
- The interrupt rule, stated precisely (this is the part to defend in an
  interview): a new task interrupts the in-progress step iff
  `new_task.priority > in_progress_step.priority` — strictly greater, not
  greater-or-equal. Equal priority does NOT interrupt; ties favor whatever
  is already running, because preempting on a tie would mean the system
  could churn forever on same-priority work without ever finishing anything.
  An interrupted step is marked `interrupted`, never removed — nothing the
  user asked for is allowed to silently vanish from the plan, even if it
  never gets resumed because something even higher-priority keeps arriving.
"""

import json
import re
from dataclasses import dataclass
from enum import Enum
from itertools import count
from typing import Optional

from src.llm.client import LLMClient

_id_counter = count(1)


class StepStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    INTERRUPTED = "interrupted"


@dataclass
class Task:
    id: str
    description: str
    target_agent: str
    urgency: int  # 1-5
    importance: int  # 1-5

    def __post_init__(self):
        if not (1 <= self.urgency <= 5):
            raise ValueError(f"urgency must be in [1, 5], got {self.urgency}")
        if not (1 <= self.importance <= 5):
            raise ValueError(f"importance must be in [1, 5], got {self.importance}")

    @property
    def priority(self) -> int:
        return self.urgency * self.importance


@dataclass
class PlanStep:
    task: Task
    status: StepStatus = StepStatus.PENDING

    @property
    def priority(self) -> int:
        return self.task.priority


class PlannerParseError(Exception):
    """Raised when the LLM's plan-decomposition response can't be parsed."""


def _extract_json(raw: str):
    match = re.search(r"[\[{].*[\]}]", raw, re.DOTALL)
    if not match:
        raise PlannerParseError(f"No JSON found in LLM response: {raw!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise PlannerParseError(f"Malformed JSON from LLM: {e}") from e


PLAN_SYSTEM_PROMPT = """You decompose a goal into an ordered list of concrete steps
for a multi-agent assistant. Respond with ONLY a JSON array (no prose, no markdown
fences), where each element has this shape:

{
  "description": string,
  "target_agent": "scheduler" | "memory" | "planner",
  "urgency": integer 1-5,
  "importance": integer 1-5
}

Order the array in the sequence steps should be attempted. Keep it to the
minimum steps that actually accomplish the goal."""


def create_plan(goal: str, llm_client: LLMClient) -> list[PlanStep]:
    """
    Decomposes `goal` into an ordered list of PlanSteps via the (sonnet-tier)
    LLM. Raises PlannerParseError on malformed output rather than returning
    a plan built from partial/guessed data.
    """
    raw = llm_client.complete(goal, system=PLAN_SYSTEM_PROMPT)
    data = _extract_json(raw)
    if not isinstance(data, list) or not data:
        raise PlannerParseError(f"Expected a non-empty JSON array of steps, got: {data!r}")

    steps = []
    for item in data:
        if not isinstance(item, dict):
            raise PlannerParseError(f"Expected each plan step to be an object, got: {item!r}")
        required = {"description", "target_agent", "urgency", "importance"}
        missing = required - item.keys()
        if missing:
            raise PlannerParseError(f"Plan step missing field(s) {missing}: {item!r}")
        task = Task(
            id=f"task-{next(_id_counter)}",
            description=item["description"],
            target_agent=item["target_agent"],
            urgency=item["urgency"],
            importance=item["importance"],
        )
        steps.append(PlanStep(task=task))
    return steps


def execute_next_step(plan: list[PlanStep]) -> Optional[PlanStep]:
    """
    Advances the plan by one step: marks the highest-priority PENDING step
    IN_PROGRESS (there should be at most one IN_PROGRESS step at a time —
    if one already exists, it's returned as-is rather than starting a
    second one concurrently) and returns it. Returns None if there's
    nothing left to do (empty or fully-done/interrupted-with-nothing-
    pending plan) — callers must handle that, not assume a step exists.
    """
    in_progress = next((s for s in plan if s.status == StepStatus.IN_PROGRESS), None)
    if in_progress is not None:
        return in_progress

    pending = [s for s in plan if s.status == StepStatus.PENDING]
    if not pending:
        return None

    next_step = max(pending, key=lambda s: s.priority)
    next_step.status = StepStatus.IN_PROGRESS
    return next_step


def interrupt_and_replan(current_plan: list[PlanStep], new_task: Task) -> list[PlanStep]:
    """
    Core replanning logic — deterministic, no LLM call (see module docstring
    for the precise rule and why).

    Returns a new list container (steps carried over transition their
    `.status` in place rather than being copied — callers should treat the
    returned list as the current plan going forward). The following
    invariants hold regardless of how many times this is called:
      - Every step that was ever in `current_plan` is still present.
      - At most one step is IN_PROGRESS at a time.
      - PENDING steps are sorted by priority, descending.
    """
    plan = list(current_plan)
    in_progress = next((s for s in plan if s.status == StepStatus.IN_PROGRESS), None)

    new_step = PlanStep(task=new_task, status=StepStatus.PENDING)

    if in_progress is None:
        # Nothing currently running (plan empty, or everything done/pending) —
        # nothing to interrupt. New task joins the pending pool and the whole
        # pending set gets priority-sorted below.
        plan.append(new_step)
    elif new_task.priority > in_progress.priority:
        # Strictly greater — ties do not interrupt (see module docstring).
        in_progress.status = StepStatus.INTERRUPTED
        new_step.status = StepStatus.IN_PROGRESS
        plan.append(new_step)
    else:
        plan.append(new_step)

    # Re-sort only the PENDING steps by priority descending; DONE/INTERRUPTED/
    # IN_PROGRESS steps keep their relative position (interruption history
    # and completion order are audit trail, not something to shuffle).
    done_or_active = [s for s in plan if s.status != StepStatus.PENDING]
    pending_sorted = sorted(
        (s for s in plan if s.status == StepStatus.PENDING),
        key=lambda s: s.priority,
        reverse=True,
    )
    return done_or_active + pending_sorted
