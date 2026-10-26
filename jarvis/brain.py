"""
JARVIS brain: the v2 Supervisor, built as a LangGraph StateGraph.

    preprocess ──► control ──────────────────────────────────────────► finalize
        │  (lockdown, voice approvals, slot picks handled without any LLM)
        ├──► split (multi-intent only) ──► route ──► dispatch ──┬──► route (next step)
        └──────────────────────────────►  route                 ├──► clarify ─► finalize
                                                                └──► finalize

Conflict-free by construction:
- Steps of a multi-intent request run sequentially; each step sees the previous results.
- Agents only *propose* side effects; the ActionExecutor serialises and gates them.
- An agent that needs more info parks the step as a clarification. The next utterance
  completes it instead of being routed as a brand-new request (unless it clearly is one).
- A low-confidence route asks one question rather than guessing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from jarvis.agents.base import AgentError, AgentReply, TurnContext
from jarvis.agents.communication import CommunicationAgent
from jarvis.agents.device import DeviceAgent
from jarvis.agents.memory import MemoryAgent
from jarvis.agents.others import BriefingAgent, ChatAgent, GitHubAgent, PlacesAgent, PlannerAgent
from jarvis.agents.scheduler import OPTION_RX, SchedulerAgent
from jarvis.core.redaction import redact
from jarvis.llm.providers import LLMUnavailable
from jarvis.llm.schemas import RouteOut, SplitOut
from jarvis.llm.structured import StructuredOutputError, complete_json
from jarvis.prompts import REWRITE, ROUTER, SPLITTER
from jarvis.router_rules import fast_route, looks_multi_intent

log = logging.getLogger("jarvis.brain")

WAKE_RX = re.compile(r"^\s*(?:hey|hi|ok|okay)?\s*,?\s*jarvis\b[\s,.!:]*", re.I)
YES_RX = re.compile(r"^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|send it|approve(d)?|confirm(ed)?|looks good|ship it|book it)"
                    r"([\s,.!]+(please|send it|do it|go ahead|go for it|that's fine|book it|confirm|approve it))*[\s.!]*$", re.I)
NO_RX = re.compile(r"^(no|nope|nah|don'?t( send( it)?)?|do not( send( it)?)?|cancel( it| that)?|stop|discard( it)?|reject( it)?|never ?mind)[\s,.!]*(thanks|please)?[\s.!]*$", re.I)
LOCKDOWN_RX = re.compile(r"\b(lock ?down|emergency stop|stop everything|kill switch)\b", re.I)
UNLOCK_RX = re.compile(r"^\s*(unlock|disable lockdown|end lockdown)\b", re.I)
FOLLOWUP_RX = re.compile(
    r"^(and|what about|how about|also|same (for|with)|then|ok(ay)? and|but)\b"
    r"|^.{0,40}\b(him|her|them|it|that one|there|those|these|he|she|they|the same)\b.{0,40}$", re.I)
ALLOW_RX = re.compile(r"^(allow|always allow|grant|give (it )?access)\b", re.I)


class RewriteOut(BaseModel):
    request: str = Field(min_length=1, max_length=400)


class BrainState(TypedDict, total=False):
    text: str
    steps: list[str]
    idx: int
    agent: Optional[str]
    confidence: float
    replies: list[Any]
    done: bool
    clarify: Optional[str]
    forced: bool


@dataclass
class TurnResult:
    reply: str
    agents: list[str] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    provider: Optional[str] = None
    understood: Optional[str] = None     # the standalone request a follow-up was rewritten to

    def to_dict(self) -> dict:
        return {"reply": self.reply, "agents": self.agents, "requests": self.requests, "cards": self.cards,
                "steps": self.steps, "provider": self.provider, "understood": self.understood}


class Brain:
    def __init__(self, svc):
        self.svc = svc
        self.agents = {a.name: a for a in [
            SchedulerAgent(svc), MemoryAgent(svc), CommunicationAgent(svc), DeviceAgent(svc), GitHubAgent(svc),
            PlacesAgent(svc), BriefingAgent(svc), PlannerAgent(svc), ChatAgent(svc)]}
        self.graph = self._build()

    # ------------------------------------------------------------------ state helpers
    @property
    def focus(self) -> dict:
        return self.svc.db.kv_get("focus", {}) or {}

    def _set_focus(self, updates: dict) -> None:
        f = self.focus
        for k, v in updates.items():
            if v is None:
                f.pop(k, None)
            else:
                f[k] = v
        self.svc.db.kv_set("focus", f)

    def _ctx(self, original: str, step_results: list[str]) -> TurnContext:
        hist = [{"role": m["role"], "content": m["content"]} for m in self.svc.db.recent_messages(6)]
        return TurnContext(now=self.svc.now(), history=hist, focus=self.focus, original=original,
                           step_results=step_results)

    # ------------------------------------------------------------------ graph
    def _build(self):
        g = StateGraph(BrainState)
        g.add_node("preprocess", self._preprocess)
        g.add_node("split", self._split)
        g.add_node("route", self._route)
        g.add_node("dispatch", self._dispatch)
        g.set_entry_point("preprocess")
        g.add_conditional_edges("preprocess", self._after_pre, {"end": END, "split": "split", "route": "route"})
        g.add_edge("split", "route")
        g.add_conditional_edges("route", lambda s: "end" if s.get("done") else "dispatch", {"end": END, "dispatch": "dispatch"})
        g.add_conditional_edges("dispatch", lambda s: "route" if not s.get("done") and s["idx"] < len(s["steps"]) else "end",
                                {"route": "route", "end": END})
        return g.compile()

    @staticmethod
    def _after_pre(s: BrainState) -> str:
        if s.get("done"):
            return "end"
        return "split" if looks_multi_intent(s["text"]) else "route"

    def _preprocess(self, s: BrainState) -> BrainState:
        text = s["text"]
        s.update(steps=[text], idx=0, replies=[], done=False)
        pend = self.focus.get("pending")
        if pend:
            agent, conf = fast_route(text)
            fresh = agent is not None and agent != pend["agent"] and conf > 0.75 and len(text.split()) > 3
            if not fresh:
                self._set_focus({"pending": None})
                if pend.get("kind") == "email" and "@" in text:
                    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
                    who = pend.get("person")
                    if email and who:
                        self.svc.executor.propose("memory.write", {"entities": [{"name": who, "type": "person"}],
                                                                   "relations": [], "attributes": [{"entity": who, "key": "email", "value": email.group(0)}]}, "memory")
                    s.update(steps=[pend["text"]], agent=pend["agent"], confidence=1.0)
                else:
                    s.update(steps=[f"{pend['text']} {text}"], agent=pend["agent"], confidence=1.0)
                s["forced"] = True
            else:
                self._set_focus({"pending": None})
        return s

    def _split(self, s: BrainState) -> BrainState:
        try:
            out: SplitOut = complete_json(self.svc.fast_llm, SplitOut, SPLITTER, f"Request: {redact(s['text'])}", task="split")
            steps = [x.strip() for x in out.steps if x.strip()]
            if steps:
                s["steps"] = steps
        except (StructuredOutputError, LLMUnavailable) as e:
            log.info("split failed, running as one step: %s", e)
        return s

    def _route(self, s: BrainState) -> BrainState:
        if s.pop("forced", False):
            return s
        text = s["steps"][s["idx"]]
        if OPTION_RX.match(text.strip()) and self.focus.get("slot_options"):
            s.update(agent="scheduler", confidence=1.0)
            return s
        agent, conf = fast_route(text)
        if agent is None:
            try:
                r: RouteOut = complete_json(self.svc.fast_llm, RouteOut, ROUTER, f"Request: {redact(text)}", task="route")
                agent, conf = r.agent, r.confidence
                if conf < 0.5 and r.clarify:
                    s["replies"].append(AgentReply("router", r.clarify, clarify="route"))
                    s["done"] = True
                    return s
            except (StructuredOutputError, LLMUnavailable):
                agent, conf = "chat", 0.3
        s.update(agent=agent, confidence=conf)
        return s

    def _dispatch(self, s: BrainState) -> BrainState:
        text, agent_name = s["steps"][s["idx"]], s["agent"]
        agent = self.agents.get(agent_name, self.agents["chat"])
        prev = [r.fact for r in s["replies"] if r.fact]
        ctx = self._ctx(text, prev)
        try:
            reply = agent.handle(text, ctx)
        except AgentError as e:
            reply = AgentReply(agent.name, str(e))
        except LLMUnavailable:
            reply = AgentReply(agent.name, "My language models are all unreachable right now. Try a simpler command.")
        except Exception as e:  # never let one agent crash the turn
            log.exception("agent %s crashed", agent.name)
            reply = AgentReply(agent.name, f"Something went wrong in the {agent.name} agent.")
        s["replies"].append(reply)
        if reply.focus:
            self._set_focus(reply.focus)
        if reply.clarify:
            pend = {"agent": agent.name, "text": text, "kind": reply.clarify}
            if reply.clarify == "email":
                m = re.search(r"address for (.+?)\.", reply.text)
                pend["person"] = m.group(1) if m else None
            self._set_focus({"pending": pend})
            s["done"] = True
            return s
        if agent.name == "planner" and reply.focus.get("plan_steps") and len(s["steps"]) == 1:
            s["steps"] = s["steps"] + reply.focus["plan_steps"]
            self._set_focus({"plan_steps": None})
        s["idx"] += 1
        return s

    # ------------------------------------------------------------------ public API
    def _control(self, text: str) -> Optional[TurnResult]:
        """Commands that must never depend on an LLM."""
        ex = self.svc.executor
        if LOCKDOWN_RX.search(text):
            n = ex.lockdown()
            return TurnResult(f"Lockdown on. All permissions revoked and {n} pending request{'s' if n != 1 else ''} cancelled. "
                              "Unlock from the Security screen.", ["guardrail"])
        if UNLOCK_RX.match(text):
            return TurnResult("For safety, unlocking needs a tap on the Security screen.", ["guardrail"])
        pending = [r for r in self.svc.db.pending_requests() if r["kind"] == "approval"]
        all_rx = re.match(r"^(approve|confirm|yes to|send) (all|both|everything)\b", text, re.I)
        if pending and (YES_RX.match(text) or NO_RX.match(text) or all_rx):
            approve = not NO_RX.match(text)
            if len(pending) > 1 and not all_rx and approve:
                items = "; ".join(r["payload"].get("summary", r["payload"]["tool"]) for r in pending)
                return TurnResult(f"{len(pending)} things are waiting: {items}. Say 'approve all', or tap the one you want.", ["guardrail"])
            targets = pending if (all_rx or not approve) else pending[-1:]
            msgs, reqs = [], []
            for r in targets:
                out = ex.resolve_approval(r["id"], approve, via="voice")
                msgs.append(out.message)
                if out.request:
                    reqs.append(out.request)
            return TurnResult(" ".join(msgs), ["guardrail"], requests=reqs)
        if ALLOW_RX.match(text) and any(r["kind"] == "permission" for r in self.svc.db.pending_requests()):
            return TurnResult("Please tap Allow on the permission prompt. I don't accept permissions by voice.", ["guardrail"])
        return None

    # ------------------------------------------------------------------ follow-ups
    def _contextualize(self, text: str) -> Optional[str]:
        """
        "and tomorrow?", "email her that", "what about Priya" only make sense with the previous turn.
        Rewrite those into a standalone request (fast model, with the last exchanges), so every agent gets
        the full meaning. Standalone messages are left alone (no model call).
        """
        if self.focus.get("pending") or OPTION_RX.match(text.strip()) or not FOLLOWUP_RX.search(text):
            return None
        rows = self.svc.db.recent_messages(7)[:-1]            # without the message we just stored
        if not rows or time.time() - rows[-1]["ts"] > 15 * 60:
            return None
        convo = "\n".join(f"{m['role']}: {m['content'][:220]}" for m in rows[-4:])
        last_user = next((m["content"] for m in reversed(rows) if m["role"] == "user"), "")
        f = self.focus
        hints = "".join([f"\nPerson mentioned last: {f['last_person']}" if f.get("last_person") else ""])
        prompt = f"Conversation:\n{convo}{hints}\nPrevious request: {last_user}\nLatest: {text}"
        try:
            out: RewriteOut = complete_json(self.svc.fast_llm, RewriteOut, REWRITE, prompt, task="rewrite", max_tokens=120)
            r = out.request.strip()
            return r if 0 < len(r) <= 400 else None
        except Exception:
            return None

    def handle(self, user_text: str, record: bool = True) -> TurnResult:
        text = WAKE_RX.sub("", user_text or "").strip()
        if not text:
            return TurnResult("Yes?", ["chat"])
        if record:
            self.svc.db.add_message("user", redact(text))
        res = self._control(text)
        understood = None
        if res is None:
            standalone = self._contextualize(text)
            if standalone and standalone.strip().lower() != text.strip().lower():
                understood, text = standalone, standalone
            final = self.graph.invoke({"text": text})
            replies: list[AgentReply] = final["replies"]
            texts = [r.text for r in replies if r.text]
            n_appr = sum(1 for r in replies for q in r.requests if q.get("kind") == "approval")
            if n_appr:
                texts.append("Say yes or tap Approve." if n_appr == 1 else "Say 'approve all' or review them on screen.")
            res = TurnResult(
                reply=" ".join(texts) if texts else "Done.",
                agents=[r.agent for r in replies],
                requests=[q for r in replies for q in r.requests],
                cards=[c for r in replies for c in r.cards],
                steps=final.get("steps", []),
                provider=getattr(self.svc.fast_llm, "last_provider", None),
                understood=understood,
            )
        self.svc.db.add_message("assistant", redact(res.reply), agent=",".join(res.agents))
        self.svc.db.trim_messages()
        return res

    def resolve_permission(self, rid: str, mode: str) -> TurnResult:
        out = self.svc.executor.resolve_permission(rid, mode)
        if out.status == "resume":
            r = self.handle(out.data["resume"], record=False)
            return r
        reply = out.message or ("Done." if out.status == "done" else "")
        if out.status == "needs_approval":
            reply = "Allowed. Now check the approval card."
        return TurnResult(reply, ["guardrail"], requests=[out.request] if out.request else [])

    def resolve_approval(self, rid: str, approved: bool, edits: Optional[dict] = None, via: str = "tap") -> TurnResult:
        out = self.svc.executor.resolve_approval(rid, approved, edits, via)
        msg = out.message or ("Done." if out.status == "done" else "Okay.")
        self.svc.db.add_message("assistant", redact(msg), agent="guardrail")
        return TurnResult(msg, ["guardrail"], requests=[out.request] if out.request else [])

    def pending(self) -> list[dict]:
        out = []
        for r in self.svc.db.pending_requests():
            out.append({"id": r["id"], "kind": r["kind"], **r["payload"]})
        return out
