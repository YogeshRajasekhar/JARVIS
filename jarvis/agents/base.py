"""Shared agent plumbing: turn context, reply type, extraction helper, outcome phrasing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Type

from pydantic import BaseModel

from jarvis.core.actions import Outcome
from jarvis.core.redaction import redact
from jarvis.llm.structured import StructuredOutputError, complete_json


@dataclass
class TurnContext:
    now: datetime
    history: list[dict] = field(default_factory=list)     # [{"role","content"}], already redacted
    focus: dict = field(default_factory=dict)             # last_person, last_event, slot_options, ...
    original: str = ""                                    # the full user utterance (for resume)
    step_results: list[str] = field(default_factory=list) # earlier steps in a multi-step turn


@dataclass
class AgentReply:
    agent: str
    text: str
    outcomes: list[Outcome] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)       # structured UI cards (events, emails, slots)
    focus: dict = field(default_factory=dict)             # updates to merge into focus
    clarify: Optional[str] = None                         # set when the agent needs one more answer
    fact: Optional[str] = None                            # short factual result for later steps in the same turn

    @property
    def requests(self) -> list[dict]:
        return [o.request for o in self.outcomes if o.request]


class AgentError(Exception):
    pass


class BaseAgent:
    name = "base"

    def __init__(self, svc):
        self.svc = svc

    # ------------------------------------------------------------------ LLM helper
    def extract(self, model: Type[BaseModel], system: str, text: str, ctx: TurnContext, task: str,
                smart: bool = False, extra: str = "") -> BaseModel:
        """Builds a compact prompt (time + graph context + short history) and validates the JSON."""
        lines = [f"Current time: {ctx.now.strftime('%A %d %B %Y, %I:%M %p')}"]
        gctx = self.svc.graph.context_for(text)
        if gctx:
            lines.append("Known facts:\n" + gctx)
        if ctx.focus.get("last_person"):
            lines.append(f"Person mentioned last: {ctx.focus['last_person']}")
        if ctx.history:
            convo = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in ctx.history[-4:])
            lines.append("Recent conversation:\n" + convo)
        if extra:
            lines.append(extra)
        lines.append(f"Request: {redact(text)}")
        llm = self.svc.smart_llm if smart else self.svc.fast_llm
        try:
            return complete_json(llm, model, system, "\n\n".join(lines), task=task)
        except StructuredOutputError as e:
            raise AgentError(f"I couldn't understand that request ({self.name}).") from e

    # ------------------------------------------------------------------ outcome phrasing
    def say_outcome(self, out: Outcome, done_text: Optional[str] = None) -> str:
        if out.status == "done":
            return done_text or out.message or "Done."
        if out.status == "needs_permission":
            return f"{out.message} I've put the request on your screen."
        if out.status == "needs_approval":
            first = (out.message or "it").split("\n")[0].rstrip(".")
            return f"Ready for your approval: {first[0].lower() + first[1:]}."
        return out.message or "I couldn't do that."

    def blocked_reply(self, out: Outcome) -> Optional[AgentReply]:
        """If a read needed permission or failed, turn it into the reply immediately."""
        if out.status == "done":
            return None
        return AgentReply(self.name, self.say_outcome(out), [out])

    def propose(self, tool: str, args: dict[str, Any], ctx: TurnContext, untrusted: bool = False) -> Outcome:
        return self.svc.executor.propose(tool, args, self.name, untrusted=untrusted, resume=ctx.original or None)
