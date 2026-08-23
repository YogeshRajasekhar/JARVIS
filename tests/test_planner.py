"""
Tests for the Planner agent.

Per the module spec, this is the file where adversarial/edge-case tests
matter most — matching the Guardrail's own testing standard. The core claim
under test is `interrupt_and_replan`'s exact tie-breaking and ordering
behavior, not just "it doesn't crash."
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.agents.planner import (
    PlanStep,
    PlannerParseError,
    StepStatus,
    Task,
    create_plan,
    execute_next_step,
    interrupt_and_replan,
)


class FakeLLMClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, prompt: str, system=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self.response


def task(desc, urgency, importance, target_agent="scheduler", id=None):
    return Task(
        id=id or f"t-{desc}",
        description=desc,
        target_agent=target_agent,
        urgency=urgency,
        importance=importance,
    )


def step(desc, urgency, importance, status=StepStatus.PENDING, id=None):
    return PlanStep(task=task(desc, urgency, importance, id=id), status=status)


# ---- Task validation ----


def test_priority_is_urgency_times_importance():
    t = task("x", urgency=3, importance=4)
    assert t.priority == 12


@pytest.mark.parametrize("urgency", [0, 6, -1])
def test_invalid_urgency_rejected(urgency):
    with pytest.raises(ValueError):
        task("x", urgency=urgency, importance=3)


@pytest.mark.parametrize("importance", [0, 6, -1])
def test_invalid_importance_rejected(importance):
    with pytest.raises(ValueError):
        task("x", urgency=3, importance=importance)


# ---- create_plan (mocked LLM) ----


def test_create_plan_parses_steps_in_order():
    llm = FakeLLMClient(
        '[{"description": "check availability", "target_agent": "scheduler", '
        '"urgency": 3, "importance": 3}, '
        '{"description": "create event", "target_agent": "scheduler", '
        '"urgency": 4, "importance": 4}]'
    )
    plan = create_plan("schedule a meeting", llm)
    assert len(plan) == 2
    assert plan[0].task.description == "check availability"
    assert plan[1].task.description == "create event"
    assert all(s.status == StepStatus.PENDING for s in plan)


def test_create_plan_rejects_non_array_response():
    llm = FakeLLMClient('{"description": "not an array"}')
    with pytest.raises(PlannerParseError):
        create_plan("do something", llm)


def test_create_plan_rejects_empty_array():
    llm = FakeLLMClient("[]")
    with pytest.raises(PlannerParseError):
        create_plan("do nothing", llm)


def test_create_plan_rejects_step_missing_required_field():
    llm = FakeLLMClient('[{"description": "x", "target_agent": "scheduler", "urgency": 3}]')
    with pytest.raises(PlannerParseError):
        create_plan("do something", llm)


def test_create_plan_rejects_malformed_json():
    llm = FakeLLMClient("not json")
    with pytest.raises(PlannerParseError):
        create_plan("do something", llm)


# ---- execute_next_step ----


def test_execute_next_step_picks_highest_priority_pending():
    plan = [step("low", 1, 1), step("high", 5, 5), step("mid", 3, 3)]
    result = execute_next_step(plan)
    assert result.task.description == "high"
    assert result.status == StepStatus.IN_PROGRESS


def test_execute_next_step_returns_existing_in_progress_without_starting_second():
    plan = [step("running", 3, 3, status=StepStatus.IN_PROGRESS), step("pending_higher", 5, 5)]
    result = execute_next_step(plan)
    assert result.task.description == "running"
    assert plan[1].status == StepStatus.PENDING  # not started concurrently


def test_execute_next_step_on_empty_plan_returns_none():
    assert execute_next_step([]) is None


def test_execute_next_step_when_all_done_returns_none():
    plan = [step("done1", 3, 3, status=StepStatus.DONE), step("done2", 2, 2, status=StepStatus.DONE)]
    assert execute_next_step(plan) is None


# ---- interrupt_and_replan: tie-breaking ----


def test_equal_priority_does_not_interrupt():
    """Defines tie-breaking explicitly: equal priority favors what's already running."""
    running = step("running", 4, 4, status=StepStatus.IN_PROGRESS)  # priority 16
    plan = [running]
    new_task = task("equal_priority_newcomer", urgency=4, importance=4)  # also 16

    result = interrupt_and_replan(plan, new_task)

    running_step = next(s for s in result if s.task.id == running.task.id)
    assert running_step.status == StepStatus.IN_PROGRESS
    new_step = next(s for s in result if s.task.id == new_task.id)
    assert new_step.status == StepStatus.PENDING


def test_lower_priority_does_not_interrupt_and_is_queued_in_sorted_position():
    running = step("running", 4, 4, status=StepStatus.IN_PROGRESS)  # 16
    existing_pending = step("existing", 2, 2)  # 4
    plan = [running, existing_pending]
    new_task = task("low_priority_newcomer", urgency=1, importance=1)  # 1

    result = interrupt_and_replan(plan, new_task)

    assert result[0].status == StepStatus.IN_PROGRESS
    pending = [s for s in result if s.status == StepStatus.PENDING]
    assert [s.task.priority for s in pending] == sorted(
        (s.task.priority for s in pending), reverse=True
    )
    assert pending[0].task.description == "existing"
    assert pending[-1].task.description == "low_priority_newcomer"


def test_strictly_higher_priority_interrupts():
    running = step("running", 3, 3, status=StepStatus.IN_PROGRESS)  # 9
    plan = [running]
    new_task = task("urgent_newcomer", urgency=5, importance=5)  # 25

    result = interrupt_and_replan(plan, new_task)

    old = next(s for s in result if s.task.id == running.task.id)
    assert old.status == StepStatus.INTERRUPTED
    new_step = next(s for s in result if s.task.id == new_task.id)
    assert new_step.status == StepStatus.IN_PROGRESS


# ---- interrupt_and_replan: empty/complete plan ----


def test_new_task_on_empty_plan_handled_without_error():
    result = interrupt_and_replan([], task("first_ever", 3, 3))
    assert len(result) == 1
    assert result[0].task.description == "first_ever"
    assert result[0].status == StepStatus.PENDING  # nothing was in progress to preempt


def test_new_task_when_plan_already_fully_done_handled_without_error():
    plan = [step("finished", 5, 5, status=StepStatus.DONE)]
    result = interrupt_and_replan(plan, task("next_thing", 2, 2))
    assert any(s.status == StepStatus.DONE for s in result)
    assert any(s.status == StepStatus.PENDING for s in result)
    assert len(result) == 2


# ---- interrupt_and_replan: multiple successive interrupts ----


def test_multiple_successive_interrupts_preserve_correct_order():
    """
    A arrives and runs. B (higher) interrupts A. C (higher still) interrupts
    B before B ever resumes. Verify final ordering: both A and B are present
    and INTERRUPTED, C is IN_PROGRESS, and nothing pending is out of order.
    """
    plan = [step("A", 2, 2)]  # priority 4
    plan = interrupt_and_replan(execute_started(plan), task("B", 4, 4, id="B"))  # 16 > 4, interrupts
    plan = interrupt_and_replan(plan, task("C", 5, 5, id="C"))  # 25 > 16, interrupts

    by_id = {s.task.id for s in plan}
    assert {"t-A", "B", "C"} <= by_id

    a_step = next(s for s in plan if s.task.id == "t-A")
    b_step = next(s for s in plan if s.task.id == "B")
    c_step = next(s for s in plan if s.task.id == "C")

    assert a_step.status == StepStatus.INTERRUPTED
    assert b_step.status == StepStatus.INTERRUPTED
    assert c_step.status == StepStatus.IN_PROGRESS


def execute_started(plan):
    """Test helper: force the first step of a freshly-built plan into IN_PROGRESS."""
    plan[0].status = StepStatus.IN_PROGRESS
    return plan


def test_task_interrupted_once_and_never_resumed_is_still_present_not_dropped():
    """
    A is interrupted by B, then C, then D, in a chain where nothing ever
    goes back to finish A or B. Both must still be present in the final
    plan with status INTERRUPTED — the exact "nothing the user asked for
    disappears" guarantee from the spec.
    """
    plan = [step("A", 2, 2, id="A", status=StepStatus.IN_PROGRESS)]  # 4
    plan = interrupt_and_replan(plan, task("B", 3, 3, id="B"))  # 9 > 4
    plan = interrupt_and_replan(plan, task("C", 4, 4, id="C"))  # 16 > 9
    plan = interrupt_and_replan(plan, task("D", 5, 5, id="D"))  # 25 > 16

    ids_present = {s.task.id: s.status for s in plan}
    assert ids_present["A"] == StepStatus.INTERRUPTED
    assert ids_present["B"] == StepStatus.INTERRUPTED
    assert ids_present["C"] == StepStatus.INTERRUPTED
    assert ids_present["D"] == StepStatus.IN_PROGRESS
    assert len(plan) == 4  # nobody dropped


def test_interrupted_step_can_still_be_resumed_later_via_execute_next_step():
    """
    Sanity check that INTERRUPTED steps aren't a dead end: once nothing
    higher-priority is running, execute_next_step should be able to pick an
    interrupted step back up if it's the best remaining candidate. This
    requires resetting it to PENDING first (interrupt_and_replan itself
    doesn't auto-resume — that's a deliberate separate decision the caller
    / supervisor makes).
    """
    plan = [step("A", 2, 2, id="A", status=StepStatus.IN_PROGRESS)]
    plan = interrupt_and_replan(plan, task("B", 5, 5, id="B"))
    a_step = next(s for s in plan if s.task.id == "A")
    assert a_step.status == StepStatus.INTERRUPTED

    # Simulate the supervisor resuming A once B completes.
    b_step = next(s for s in plan if s.task.id == "B")
    b_step.status = StepStatus.DONE
    a_step.status = StepStatus.PENDING

    resumed = execute_next_step(plan)
    assert resumed.task.id == "A"
    assert resumed.status == StepStatus.IN_PROGRESS
