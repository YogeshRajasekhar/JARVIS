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
from jarvis.agents.life import ListsAgent, MoneyAgent, RoutineAgent
from jarvis.agents.memory import MemoryAgent
from jarvis.agents.others import BriefingAgent, ChatAgent, GitHubAgent, PlacesAgent, PlannerAgent
from jarvis.agents.scheduler import OPTION_RX, SchedulerAgent
from jarvis.agents.web import AppsAgent, WebAgent
from jarvis.core import quick
from jarvis.core.redaction import redact
from jarvis.core.timeparse import resolve as resolve_time
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
# Backing out of a question JARVIS asked ("What time?" -> "never mind").
CANCEL_RX = re.compile(r"^(?:oh |ah |uh |um |ok(?:ay)?[, ]+|actually[, ]+|no[, ]+|nah[, ]+)*(?:never ?mind|forget (?:it|that|about it)|cancel(?: it| that)?|"
                       r"stop|skip(?: it| that)?|leave it|drop it|nothing|no(?:pe)?|not now|don'?t bother|scrap (?:it|that)|abort)(?:[, ]+(?:thanks|thank you|please|jarvis))*[.! ]*$", re.I)
REPEAT_RX = re.compile(r"^(?:can you |could you |please )?(?:repeat(?: that| it)?|say (?:that|it) again|come again|pardon|what did you (?:just )?say|"
                       r"one more time|i didn'?t (?:catch|hear|get) (?:that|it|you))(?: please)?[?.! ]*$", re.I)
HELP_RX = re.compile(r"^(?:help|help me|what can you do(?: for me)?|what (?:are|r) your (?:features|skills|abilities|capabilities)|"
                     r"what do you do|how (?:can|do) (?:you help|i use you)|what are you capable of|show (?:me )?(?:your )?(?:commands|features)|"
                     r"what can i (?:ask|say)(?: you)?|commands)[?.! ]*$", re.I)
ORDINAL_PICK = re.compile(r"^(?:yes|yeah|yep|ok(?:ay)?|sure|fine|go ahead|that works|sounds good|do it|the first|first|earliest|"
                          r"the earliest(?: one)?|any(?: of them| one)?|whichever|either)[, .!]*(?:please)?[.! ]*$", re.I)
LAST_PICK = re.compile(r"^(?:the )?(?:last|latest|final)(?: one| option| slot)?[.! ]*$", re.I)
COMMAND_START = re.compile(r"^(?:please |can you |could you |hey |jarvis[, ]+)*(?:what|when|where|who|why|how|is|are|do|does|did|will|can|could|should|"
                           r"set|schedule|book|email|mail|send|text|call|open|play|remind|remember|add|show|list|tell|give|find|search|check|turn|"
                           r"cancel my|move my|create|make|start|stop the|navigate|brief|plan|read|reply|draft|note|log|i spent|we're|i need)\b", re.I)

SMALLTALK = [   # answered instantly; no model call, no quota
    (re.compile(r"^(?:ok(?:ay)?|k|cool|great|nice|got it|alright|all right|fine|perfect|awesome|good|understood|noted|sure|yep|yeah)[.! ]*$", re.I), "Okay."),
    (re.compile(r"^(?:thanks|thank you|thanks a lot|thank you so much|thx|ty|cheers|much appreciated)(?: jarvis)?[.! ]*$", re.I), "Anytime."),
    (re.compile(r"^(?:never ?mind|forget it|nothing|no thanks|nope|that'?s all|that will be all|that'?s it)(?: thanks| thank you)?[.! ]*$", re.I), "No problem."),
    (re.compile(r"^(?:bye|goodbye|good bye|see you|see ya|later|talk to you later)(?: jarvis)?[.! ]*$", re.I), "Goodbye."),
    (re.compile(r"^(?:hi|hello|hey|yo|hola|namaste|vanakkam)(?: there| jarvis)?[.! ]*$", re.I), "Hello, {name}. What can I do for you?"),
    (re.compile(r"^good morning(?: jarvis)?[.! ]*$", re.I), "Good morning, {name}. Say \"brief me\" for your day."),
    (re.compile(r"^good (?:afternoon|evening)(?: jarvis)?[.! ]*$", re.I), "Hello, {name}. What do you need?"),
    (re.compile(r"^(?:good ?night|gn|sweet dreams)(?: jarvis)?[.! ]*$", re.I), "Good night, {name}."),
    (re.compile(r"^(?:how are you|how'?s it going|how are you doing|you good|what'?s up)(?: jarvis)?[?.! ]*$", re.I), "Running smoothly. What do you need?"),
    (re.compile(r"^(?:who are you|what are you|what(?:'s| is) your name|introduce yourself)[?.! ]*$", re.I),
     "I'm JARVIS, your personal assistant. I run on your own server and handle your calendar, mail, phone, lists and more, always with your permission."),
    (re.compile(r"^(?:are you there|you there|can you hear me|hello\?+|testing|test|mic check)[?.! ]*$", re.I), "I'm here."),
]
# Things Android does not let an app do. Saying so beats pretending or guessing.
UNSUPPORTED = [
    (re.compile(r"\b(take|capture|grab) (a |the )?screenshot\b|\bscreenshot (this|that|the screen)\b", re.I),
     "I can't take screenshots. Android only lets you do that: press power and volume down together."),
    (re.compile(r"\b(brightness|dim the screen|brighten)\b", re.I),
     "I can't change the screen brightness. Pull down the quick settings to adjust it."),
    (re.compile(r"\b(air ?plane mode|flight mode|mobile data|hotspot|tethering)\b", re.I),
     "Android doesn't let apps switch that. I can open the settings panel if you say \"open settings\"."),
    (re.compile(r"\b(restart|reboot|shut ?down|switch off|power off|turn off) (my |the |this )?(phone|device|mobile)\b", re.I),
     "I can't restart or switch off the phone. Hold the power button for that."),
    (re.compile(r"\b(unlock|lock) (my |the )?(phone|screen)\b", re.I),
     "I can't lock or unlock the phone screen. That stays with you."),
    (re.compile(r"\b(pay|transfer|send) (rs\.?|₹|\d|money|rupees)|\b(upi|gpay|phonepe|paytm) (to|pay)\b|\bmake a payment\b", re.I),
     "I never make payments or open payment apps. That stays with you."),
    (re.compile(r"\bread (my |the )?(sms|texts?|otp|messages from the bank)\b|\bwhat(?:'s| is) (my|the) otp\b", re.I),
     "I don't read your SMS inbox. It holds OTPs and bank alerts, so it's off limits to me."),
]

CAPABILITIES = (
    "Here's what I can do. Calendar: show your day, book, move and cancel meetings, find free time. "
    "Mail and Slack: read, summarise and draft messages, which I only send after you approve. "
    "Phone: alarms, timers, reminders, calls, texts, WhatsApp, open apps, play music or videos, navigation, volume. "
    "Lists and notes: to-dos, shopping, any list. Money: log expenses and track a budget. "
    "The world: weather, news, facts, currency, maths and unit conversions. "
    "Memory: tell me about people and preferences and I'll remember. "
    "Also GitHub, travel times, a daily briefing, planning, and routines that run several steps from one phrase. "
    "Tap the phone icon to talk hands-free."
)


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
    asked: Any                # (kind, tries) of the question this turn is answering
    routine: Optional[str]
    handed: bool


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
            SchedulerAgent(svc), MemoryAgent(svc), CommunicationAgent(svc), DeviceAgent(svc), AppsAgent(svc), ListsAgent(svc),
            MoneyAgent(svc), WebAgent(svc), RoutineAgent(svc), GitHubAgent(svc), PlacesAgent(svc), BriefingAgent(svc),
            PlannerAgent(svc), ChatAgent(svc)]}
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
        if s.get("routine") or s.get("forced"):
            return "route"
        from jarvis.agents.life import parse_routine
        if parse_routine(s["text"]):                 # "when I say X, do A and B" is ONE request (saving a routine)
            return "route"
        return "split" if looks_multi_intent(s["text"]) else "route"

    @staticmethod
    def _answers(kind: str, text: str, now) -> bool:
        """Does `text` look like an answer to the question JARVIS just asked (rather than a new request)?"""
        t = text.strip()
        words = t.split()
        if kind == "email":
            return "@" in t
        if kind == "time":
            return resolve_time(t, now) is not None or bool(re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm)?|noon|midnight|morning|evening|tonight|"
                                                                    r"tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                                                                    r"in \d+|seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", t, re.I))
        if kind == "amount":
            return bool(re.search(r"\d", t))
        if kind in ("contact", "recipient", "channel", "app", "setting", "place", "places"):
            return len(words) <= 6 and not COMMAND_START.match(t)
        # "content": free text is expected, but a clear new command is not the content of a message
        agent, conf = fast_route(t)
        return not (COMMAND_START.match(t) and agent is not None and conf > 0.75 and t.endswith("?"))

    def _preprocess(self, s: BrainState) -> BrainState:
        text = s["text"]
        s.update(steps=[text], idx=0, replies=[], done=False)
        pend = self.focus.get("pending")
        if pend:
            self._set_focus({"pending": None})
            agent, conf = fast_route(text)
            fresh = agent is not None and agent != pend["agent"] and conf > 0.75 and len(text.split()) > 3
            if not fresh and self._answers(pend.get("kind") or "content", text, self.svc.now()):
                if pend.get("kind") == "email":
                    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
                    who = pend.get("person")
                    if email and who:
                        self.svc.executor.propose("memory.write", {"entities": [{"name": who, "type": "person"}],
                                                                   "relations": [], "attributes": [{"entity": who, "key": "email", "value": email.group(0)}]}, "memory")
                    s.update(steps=[pend["text"]], agent=pend["agent"], confidence=1.0)
                else:
                    ans, join = text, " "
                    if pend.get("kind") == "content" and pend["agent"] in ("communication", "device", "apps"):
                        # the answer is the message itself: "tell him the demo is at 5" -> "... saying the demo is at 5"
                        ans = re.sub(r"^(?:tell (?:him|her|them)|say|just say|write|that|saying)(?: that)?[, ]+", "", text, flags=re.I)
                        join = " saying "
                    s.update(steps=[f"{pend['text']}{join}{ans}"], agent=pend["agent"], confidence=1.0)
                s["forced"] = True
                s["asked"] = (pend.get("kind"), pend.get("tries", 0))
                return s
        routine = self.svc.routines.exact(text) if not pend else None
        if routine:                                  # the phrase IS a routine: run its steps in order
            self.svc.routines.ran(routine["name"])
            s["steps"] = list(routine["steps"])
            s["routine"] = routine["name"]
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
        if agent in (None, "web") and self._about_known(text):
            agent, conf = "memory", 0.9              # "who is Arun" is about someone you told me about, not the web
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

    def _about_known(self, text: str) -> bool:
        m = re.match(r"^(?:who(?:'s| is| are| was)|what do (?:i|you) know about|tell me about|what(?:'s| is)|where (?:is|does))\s+(?P<x>.+?)"
                     r"(?:'s (?P<attr>\w+(?: \w+)?))?(?: (?:work|live|study|stay))?[?.! ]*$", text.strip(), re.I)
        if not m:
            return False
        x = re.sub(r"^(?:the|my|our) ", "", m.group("x").strip(), flags=re.I)
        if len(x.split()) > 4:
            return False
        nid = self.svc.graph.resolve(x)
        return bool(nid and nid != "self:me")

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
        if reply.handoff and not s.get("handed") and reply.handoff in self.agents:
            s["handed"] = True                       # one hand-over per turn, e.g. memory doesn't know -> look it up
            s["agent"] = reply.handoff
            return self._dispatch(s)
        s["replies"].append(reply)
        if reply.focus:
            self._set_focus(reply.focus)
        if reply.clarify:
            kind, tries = s.get("asked") or (None, 0)
            if kind == reply.clarify and tries >= 1:
                # Asked the same thing twice and still can't use the answer: stop asking, don't trap the conversation.
                reply.text = "I still didn't get that, so I've dropped it. Tell me the whole request again when you're ready."
                reply.clarify = None
                s["done"] = True
                return s
            pend = {"agent": agent.name, "text": s["steps"][s["idx"]] if not s.get("asked") else text, "kind": reply.clarify,
                    "tries": (tries + 1) if kind == reply.clarify else 0}
            if reply.clarify == "email":
                m = re.search(r"address for (.+?)\.", reply.text)
                pend["person"] = m.group(1) if m else None
            self._set_focus({"pending": pend})
            s["done"] = True
            return s
        if reply.focus.get("plan_steps") and len(s["steps"]) == 1 and not s.get("routine"):
            s["steps"] = s["steps"] + list(reply.focus["plan_steps"])[:8]     # planner goals and "run my X routine"
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
        pend = self.focus.get("pending")
        if pend and CANCEL_RX.match(text):
            self._set_focus({"pending": None})
            return TurnResult("Okay, dropped.", [pend.get("agent") or "chat"])
        if self.focus.get("slot_options") and not pend:
            if CANCEL_RX.match(text) and not [r for r in self.svc.db.pending_requests() if r["kind"] == "approval"]:
                self._set_focus({"slot_options": None})
                return TurnResult("Okay, I won't book it.", ["scheduler"])
        if REPEAT_RX.match(text):
            last = next((m["content"] for m in reversed(self.svc.db.recent_messages(8)) if m["role"] == "assistant"), None)
            return TurnResult(last or "I haven't said anything yet.", ["chat"])
        if HELP_RX.match(text):
            return TurnResult(CAPABILITIES, ["chat"])
        has_approval = any(r["kind"] == "approval" for r in self.svc.db.pending_requests())
        if not pend and not has_approval and not self.focus.get("slot_options") and not self.svc.routines.exact(text):
            for rx, reply in SMALLTALK:
                if rx.match(text):
                    return TurnResult(reply.format(name=self.svc.settings.user_name), ["chat"])
        if not pend:
            for rx, reply in UNSUPPORTED:
                if rx.search(text):
                    return TurnResult(reply, ["guardrail"])
        if not pend:
            ans = quick.answer(text, self.svc.now())
            if ans:
                return TurnResult(ans, ["utility"])
        pending = [r for r in self.svc.db.pending_requests() if r["kind"] == "approval"]
        all_rx = re.match(r"^(approve|confirm|yes to|send) (all|both|everything)\b", text, re.I)
        none_all = re.match(r"^(no to (all|everything|both)|cancel (all|everything|both)|reject (all|everything|both)|none( of them)?)\b", text, re.I)
        if pending and (YES_RX.match(text) or NO_RX.match(text) or all_rx or none_all):
            approve = not (NO_RX.match(text) or none_all)
            if len(pending) > 1 and not all_rx and approve:
                items = "; ".join(r["payload"].get("summary", r["payload"]["tool"]) for r in pending)
                return TurnResult(f"{len(pending)} things are waiting: {items}. Say 'approve all', or tap the one you want.", ["guardrail"])
            none_rx = re.match(r"^(no to (all|everything|both)|cancel (all|everything|both)|reject (all|everything|both)|none( of them)?)\b", text, re.I)
            targets = pending if (all_rx or none_rx) else pending[-1:]
            msgs, reqs = [], []
            for r in targets:
                out = ex.resolve_approval(r["id"], approve, via="voice")
                if out.message and out.message not in msgs:
                    msgs.append(out.message)
                if out.request:
                    reqs.append(out.request)
            if not approve and len(targets) > 1:
                msgs = [f"Cancelled all {len(targets)}. Nothing was sent or changed."]
            elif not approve and len(pending) > 1:
                rest = len(pending) - 1
                msgs.append(f"{rest} other{'s are' if rest != 1 else ' is'} still waiting. Say \"cancel all\" to drop {'them' if rest != 1 else 'it'} too.")
            return TurnResult(" ".join(msgs) or "Done.", ["guardrail"], requests=reqs)
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
        raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", user_text or "")
        only_wake = bool(WAKE_RX.fullmatch(raw.strip() + " ")) or bool(re.fullmatch(r"\s*(?:hey|hi|ok|okay)?\s*,?\s*jarvis[\s,.!?]*", raw, re.I))
        text = re.sub(r"\s+", " ", WAKE_RX.sub("", raw)).strip()[:1500]
        if not text or only_wake:
            return TurnResult("Yes?", ["chat"])
        if not re.search(r"[^\W_]", text, re.UNICODE):          # only punctuation or emoji: nothing to act on
            return TurnResult("Sorry, I didn't catch that. What do you need?", ["chat"])
        if self.focus.get("slot_options") and not self.focus.get("pending"):
            n = len(self.focus["slot_options"])
            if ORDINAL_PICK.match(text):
                text = "1"
            elif LAST_PICK.match(text):
                text = str(min(n, 3))
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
            if final.get("routine"):
                texts.insert(0, f"Running {final['routine']}.")
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
