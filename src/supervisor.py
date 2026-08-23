"""
Supervisor — LangGraph wiring that routes a request to the right agent and
surfaces pending Guardrail approvals back to the caller.

Design rationale:
- Routing uses the haiku-tier LLM client: "which of {scheduler, memory,
  planner} does this request belong to" is a narrow classification task,
  same reasoning as intent parsing in the Scheduler and query translation
  in the Memory Agent — it doesn't need the reasoning-heavy sonnet tier.
- The graph has an explicit `approval` node rather than folding
  yes/no-handling into the router's classification. A pending approval is
  entry-point state, not something the LLM should have to (re-)classify —
  if there's a held action waiting on a human answer, the graph goes
  straight to resolving it, bypassing the router LLM call entirely. This
  also means router misclassification can never accidentally lose or
  mis-execute a pending write.
- `SupervisorState` deliberately stays a plain, inspectable TypedDict (no
  message-list reducers, no hidden accumulation) — every field is either
  overwritten each turn or explicitly carried forward by the `Supervisor`
  class between calls. For a single-user personal assistant with one
  conversation at a time, this is simpler to reason about and test than a
  general multi-turn reducer graph, and every intermediate value is
  directly assertable in tests.
- No silent failure: an LLM route classification outside {scheduler,
  memory, planner} produces an explicit error result via a dedicated
  fallback path, not a crash and not a guess at the "safest" agent.
"""

from typing import Optional, TypedDict

from langgraph.graph import StateGraph, END

from src.agents.memory_agent import MemoryAgent
from src.agents.planner import PlanStep, create_plan, interrupt_and_replan
from src.agents.scheduler import SchedulerAgent
from src.llm.client import LLMClient

ALLOWED_ROUTES = {"scheduler", "memory", "planner"}

ROUTER_SYSTEM_PROMPT = """You route a personal assistant request to exactly one
of three agents. Respond with ONLY one word, no punctuation, no explanation:

"scheduler" - calendar requests: checking availability, creating, moving, or
  cancelling meetings/events.
"memory" - questions about people/relationships/tasks the user has previously
  told the assistant about, or explicit requests to remember a new fact.
"planner" - open-ended, multi-step, or ambiguous goals that need to be broken
  into an ordered plan, especially anything involving reprioritizing or
  reconsidering existing commitments.

Respond with exactly one of: scheduler, memory, planner"""


class SupervisorState(TypedDict, total=False):
    user_input: str
    route: Optional[str]
    result: Optional[str]
    pending_approval: Optional[dict]
    active_plan: Optional[list[PlanStep]]
    approval_decision: Optional[bool]


def _looks_like_a_write(text: str) -> bool:
    """
    Cheap local heuristic for query-vs-remember inside the memory node —
    deliberately not an LLM call, same "don't pay for a model call on a
    decision a keyword check answers just as well" reasoning as elsewhere
    in this system. False negatives just fall through to query(), which is
    read-only and safe by construction; false positives fall into
    remember(), which still runs its own structured-output validation.
    """
    lowered = text.strip().lower()
    return lowered.startswith("remember") or "remember that" in lowered


class Supervisor:
    def __init__(
        self,
        router_llm: LLMClient,
        planner_llm: LLMClient,
        scheduler: SchedulerAgent,
        memory_agent: MemoryAgent,
    ):
        self.router_llm = router_llm
        self.planner_llm = planner_llm
        self.scheduler = scheduler
        self.memory_agent = memory_agent

        # Carried between turns explicitly (see module docstring) rather
        # than via a LangGraph checkpointer — this is a single-session
        # in-process assistant, not a multi-conversation server.
        self.pending_approval: Optional[dict] = None
        self.active_plan: Optional[list[PlanStep]] = None
        self.last_route: Optional[str] = None

        self.graph = self._build_graph()

    # ---- Graph construction ----

    def _build_graph(self):
        graph = StateGraph(SupervisorState)
        graph.add_node("router", self._router_node)
        graph.add_node("scheduler", self._scheduler_node)
        graph.add_node("memory", self._memory_node)
        graph.add_node("planner", self._planner_node)
        graph.add_node("approval", self._approval_node)
        graph.add_node("invalid_route", self._invalid_route_node)

        graph.set_conditional_entry_point(
            self._entry_router,
            {"approval": "approval", "router": "router"},
        )
        graph.add_conditional_edges(
            "router",
            lambda state: state["route"] if state["route"] in ALLOWED_ROUTES else "invalid",
            {
                "scheduler": "scheduler",
                "memory": "memory",
                "planner": "planner",
                "invalid": "invalid_route",
            },
        )
        graph.add_edge("scheduler", END)
        graph.add_edge("memory", END)
        graph.add_edge("planner", END)
        graph.add_edge("approval", END)
        graph.add_edge("invalid_route", END)
        return graph.compile()

    @staticmethod
    def _entry_router(state: SupervisorState) -> str:
        return "approval" if state.get("pending_approval") is not None else "router"

    # ---- Node implementations ----

    def _router_node(self, state: SupervisorState) -> SupervisorState:
        raw = self.router_llm.complete(state["user_input"], system=ROUTER_SYSTEM_PROMPT)
        route = raw.strip().lower()
        return {**state, "route": route}

    def _invalid_route_node(self, state: SupervisorState) -> SupervisorState:
        return {
            **state,
            "result": f"Couldn't route that request (router returned {state['route']!r}).",
        }

    def _scheduler_node(self, state: SupervisorState) -> SupervisorState:
        result = self.scheduler.handle_request(state["user_input"])
        new_state = {**state, "result": result.message}
        if result.pending_approval:
            new_state["pending_approval"] = {
                "agent": "scheduler",
                "intent": result.intent,
                "guardrail_decision": result.guardrail_decision,
            }
        return new_state

    def _memory_node(self, state: SupervisorState) -> SupervisorState:
        text = state["user_input"]
        if _looks_like_a_write(text):
            result = self.memory_agent.remember(text)
            new_state = {**state, "result": result.message}
            if result.pending_approval:
                new_state["pending_approval"] = {"agent": "memory", "write_result": result}
            return new_state

        answer = self.memory_agent.query(text)
        return {**state, "result": answer}

    def _planner_node(self, state: SupervisorState) -> SupervisorState:
        goal = state["user_input"]
        current_plan = state.get("active_plan")

        if current_plan:
            # Treat the new goal as a competing task against whatever's
            # already running, rather than starting a second, unrelated
            # plan — this is the "Planner reconsiders given a conflict"
            # path the supervisor exposes on top of the pure function in
            # planner.py.
            new_step = create_plan(goal, self.planner_llm)[0]
            plan = interrupt_and_replan(current_plan, new_step.task)
        else:
            plan = create_plan(goal, self.planner_llm)

        summary = "; ".join(f"[{s.status.value}] {s.task.description}" for s in plan)
        return {**state, "active_plan": plan, "result": f"Plan: {summary}"}

    def _approval_node(self, state: SupervisorState) -> SupervisorState:
        pending = state["pending_approval"]
        approved = state.get("approval_decision")
        if approved is None:
            return {**state, "result": f"Awaiting your approval: {pending}"}

        agent = pending["agent"]
        if not approved:
            return {**state, "result": "Discarded — action not taken.", "pending_approval": None}

        if agent == "scheduler":
            result = self.scheduler.confirm_pending(pending["intent"])
            return {**state, "result": result.message, "pending_approval": None}

        if agent == "memory":
            result = self.memory_agent.commit_pending(pending["write_result"])
            return {**state, "result": result.message, "pending_approval": None}

        raise ValueError(f"Unknown pending-approval agent: {agent!r}")

    # ---- Public API ----

    def handle_message(self, user_input: str) -> str:
        """One conversational turn. If a Guardrail approval is pending, the
        request is treated as classification input only after routing —
        callers should use respond_to_approval() instead while a decision
        is outstanding."""
        state: SupervisorState = {
            "user_input": user_input,
            "pending_approval": self.pending_approval,
            "active_plan": self.active_plan,
            "approval_decision": None,
        }
        result_state = self.graph.invoke(state)
        self.last_route = result_state.get("route")
        self.pending_approval = result_state.get("pending_approval")
        self.active_plan = result_state.get("active_plan")
        return result_state["result"]

    def respond_to_approval(self, approved: bool) -> str:
        if self.pending_approval is None:
            return "There's nothing pending approval right now."
        state: SupervisorState = {
            "user_input": "",
            "pending_approval": self.pending_approval,
            "active_plan": self.active_plan,
            "approval_decision": approved,
        }
        result_state = self.graph.invoke(state)
        self.pending_approval = result_state.get("pending_approval")
        self.active_plan = result_state.get("active_plan")
        return result_state["result"]
