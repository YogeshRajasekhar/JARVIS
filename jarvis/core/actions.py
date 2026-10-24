"""
Tool registry + ActionExecutor: the ONLY path by which anything in JARVIS has a side effect.

Agents never call integrations to change the world. They *propose* an action; the executor
runs the same pipeline every time:

    deny-list -> permission broker -> guardrail -> approval queue -> resource lock -> run -> audit

Centralising this is what makes the multi-agent system conflict-free:
- one place serialises writes (per-resource locks), so two agents can't double-book;
- one approval inbox (FIFO, ids, idempotent resolution) instead of a single "pending" slot,
  so a second request never overwrites the first, and nothing executes twice;
- one audit trail.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.agents.guardrail import (
    ActionRequest, ActionSeverity, GuardrailAgent, TargetSensitivity, Verdict,
)
from jarvis.core.permissions import SCOPES, PermissionBroker, PermStatus
from jarvis.core.redaction import redact
from jarvis.core.storage import Database

log = logging.getLogger("jarvis.actions")

APPROVAL_TTL_SECONDS = 30 * 60


class GuardrailV2(GuardrailAgent):
    """
    v1's deterministic two-axis classifier, plus three v2 rules:
      1. A reversible WRITE that only affects the user's own data (an alarm, a private event,
         a memory note) is auto-approved: the permission grant already covers it, and asking
         every time would train the user to approve without reading.
      2. Anything proposed while processing untrusted content (email/Slack/web text) needs
         approval, however harmless it looks. This is the prompt-injection firebreak.
      3. Everything else falls through to v1 unchanged.
    """

    def evaluate_v2(self, request: ActionRequest, untrusted: bool):
        if untrusted and request.severity.value >= ActionSeverity.DRAFT.value:
            return Verdict.REQUIRE_APPROVAL, "Proposed while reading external content; needs your OK."
        if (request.severity == ActionSeverity.WRITE and request.target_sensitivity == TargetSensitivity.SELF
                and request.is_reversible):
            return Verdict.AUTO_APPROVE, "Reversible change to your own data."
        d = self.evaluate(request)
        return d.verdict, d.reason


@dataclass
class Tool:
    name: str
    run: Callable[[dict], Any]
    describe: Callable[[dict], str]
    scopes: tuple[str, ...] = ()
    severity: ActionSeverity = ActionSeverity.READ
    reversible: bool = True
    target: Callable[[dict], TargetSensitivity] = lambda a: TargetSensitivity.SELF
    always_confirm: bool = False        # e.g. sending any message
    # Approval needs an on-screen tap (+ biometric on the phone), voice "yes" is not enough.
    # bool or a function of args (e.g. True only for unknown recipients).
    strong_auth: Any = False
    editable: tuple[str, ...] = ()      # fields the user may edit on the approval card
    resource: Optional[Callable[[dict], str]] = None
    precheck: Optional[Callable[[dict], Optional[str]]] = None  # returns a block reason or None
    summary: Optional[Callable[[dict], str]] = None   # short speakable description for voice replies


@dataclass
class Outcome:
    status: str          # done | needs_permission | needs_approval | denied | blocked | error | expired
    tool: str
    message: str = ""
    data: Any = None
    request: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


class ActionExecutor:
    def __init__(self, db: Database, broker: PermissionBroker):
        self.db = db
        self.broker = broker
        self.guardrail = GuardrailV2()
        self.tools: dict[str, Tool] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.locked_down = False

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Tool {tool.name} registered twice")
        for s in tool.scopes:
            if s not in SCOPES:
                raise ValueError(f"Tool {tool.name} uses unknown scope {s}")
        self.tools[tool.name] = tool

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------ propose
    def propose(self, tool_name: str, args: dict, actor: str, untrusted: bool = False,
                skip_permission: bool = False, resume: Optional[str] = None) -> Outcome:
        """
        `resume`: the user utterance to re-run after a permission is granted. Used when a tool
        is called in the middle of an agent's flow (e.g. contact lookup while drafting an email):
        re-running the whole (read-only up to this point) turn is simpler and safer than
        trying to continue a half-finished agent.
        """
        tool = self.tools.get(tool_name)
        if tool is None:
            self.db.audit(actor, tool_name, args, "blocked", "unknown tool")
            return Outcome("blocked", tool_name, f"I don't have a tool called {tool_name}.")
        if self.locked_down and tool.severity != ActionSeverity.READ:
            return Outcome("blocked", tool_name, "Lockdown is on. Say 'unlock' on screen to re-enable actions.")

        if tool.precheck:
            reason = tool.precheck(args)
            if reason:
                self.db.audit(actor, tool_name, args, "blocked", reason)
                return Outcome("blocked", tool_name, reason)

        if not skip_permission:
            pc = self.broker.check(tool.scopes)
            if pc.status == PermStatus.BLOCKED:
                self.db.audit(actor, tool_name, args, "blocked", pc.reason)
                return Outcome("blocked", tool_name, f"That's off-limits: {pc.reason}.")
            if pc.status == PermStatus.DENIED:
                return Outcome("denied", tool_name,
                               f"You haven't allowed me to {SCOPES[pc.scope].title}. You can change that in Permissions.")
            if pc.status == PermStatus.NEEDS_PROMPT:
                scope = SCOPES[pc.scope]
                payload = {"scope": scope.name, "title": scope.title, "why": scope.why, "risk": scope.risk,
                           "pending": {"tool": tool_name, "args": args, "actor": actor, "untrusted": untrusted,
                                       "resume": resume}}
                rid = self.db.add_request("permission", payload)
                return Outcome("needs_permission", tool_name,
                               f"I need permission to {scope.title}.", request={"id": rid, "kind": "permission", **payload})

        verdict, reason = self.guardrail.evaluate_v2(
            ActionRequest(agent_name=actor, action_description=tool.describe(args), severity=tool.severity,
                          target_sensitivity=tool.target(args), is_reversible=tool.reversible),
            untrusted,
        )
        if verdict == Verdict.BLOCK:
            self.db.audit(actor, tool_name, args, "blocked", reason)
            return Outcome("blocked", tool_name, reason)
        if verdict == Verdict.REQUIRE_APPROVAL or tool.always_confirm:
            target = tool.target(args)
            if untrusted:
                reason = "This came up while reading an email or message, so it needs your OK."
            elif tool.always_confirm:
                reason = "JARVIS never sends anything on your behalf without your OK."
            elif tool.severity == ActionSeverity.IRREVERSIBLE or not tool.reversible:
                reason = "This can't be undone."
            elif target == TargetSensitivity.EXTERNAL_UNKNOWN:
                reason = "This involves someone who isn't in your contacts."
            elif target == TargetSensitivity.KNOWN_CONTACT:
                reason = "This affects other people's calendars or inboxes."
            else:
                reason = "This needs your OK."
            payload = {"tool": tool_name, "args": args, "actor": actor, "preview": tool.describe(args),
                       "reason": reason,
                       "strong_auth": bool(tool.strong_auth(args) if callable(tool.strong_auth) else tool.strong_auth),
                       "editable": list(tool.editable),
                       "untrusted": untrusted, "expires": time.time() + APPROVAL_TTL_SECONDS,
                       "severity": tool.severity.name}
            payload["summary"] = tool.summary(args) if tool.summary else payload["preview"].split("\n")[0]
            rid = self.db.add_request("approval", payload)
            self.db.audit(actor, tool_name, args, "pending_approval", reason)
            return Outcome("needs_approval", tool_name, payload["summary"],
                           request={"id": rid, "kind": "approval", **payload})

        return self._run(tool, args, actor)

    # ------------------------------------------------------------------ run
    def _run(self, tool: Tool, args: dict, actor: str) -> Outcome:
        key = tool.resource(args) if tool.resource else tool.name
        with self._lock_for(key):
            try:
                data = tool.run(args)
            except Exception as e:  # integrations raise many things; never crash the turn
                log.exception("tool %s failed", tool.name)
                self.db.audit(actor, tool.name, args, "error", str(e))
                short = redact(str(e).split("\n")[0])[:140]
                return Outcome("error", tool.name, f"That didn't work ({short}).")
        self.broker.consume(tool.scopes)
        self.db.audit(actor, tool.name, args, "done")
        msg = data.get("message", "") if isinstance(data, dict) else ""
        return Outcome("done", tool.name, msg, data=data)

    # ------------------------------------------------------------------ resolve user decisions
    def resolve_permission(self, rid: str, mode: str) -> Outcome:
        req = self.db.get_request(rid)
        if not req or req["kind"] != "permission":
            return Outcome("error", "permission", "No such permission request.")
        if req["status"] != "pending":
            return Outcome("error", "permission", "That request was already answered.")
        p = req["payload"]
        self.broker.grant(p["scope"], mode)
        self.db.resolve_request(rid, "granted" if mode != "deny" else "denied")
        pend = p["pending"]
        if mode == "deny":
            return Outcome("denied", pend["tool"], f"Okay, I won't {p['title']}.")
        if pend.get("resume"):
            return Outcome("resume", pend["tool"], "", data={"resume": pend["resume"]})
        return self.propose(pend["tool"], pend["args"], pend["actor"], pend.get("untrusted", False))

    def resolve_approval(self, rid: str, approved: bool, edits: Optional[dict] = None,
                         via: str = "tap") -> Outcome:
        req = self.db.get_request(rid)
        if not req or req["kind"] != "approval":
            return Outcome("error", "approval", "No such approval request.")
        if req["status"] != "pending":
            return Outcome("error", "approval", "That was already decided.")  # idempotent: never runs twice
        p = req["payload"]
        tool = self.tools.get(p["tool"])
        if time.time() > p.get("expires", 0):
            self.db.resolve_request(rid, "expired")
            return Outcome("expired", p["tool"], "That approval expired. Ask me again if you still want it.")
        if not approved:
            self.db.resolve_request(rid, "rejected")
            self.db.audit("user", p["tool"], p["args"], "rejected")
            return Outcome("denied", p["tool"], "Cancelled. Nothing was sent or changed.")
        if p.get("strong_auth") and via == "voice":
            return Outcome("needs_approval", p["tool"],
                           "This one needs an on-screen confirmation, not just voice.",
                           request={"id": rid, "kind": "approval", **p})
        args = dict(p["args"])
        for k, v in (edits or {}).items():
            if k in p.get("editable", []) and isinstance(v, str):
                args[k] = v
        if not self.db.resolve_request(rid, "approved"):
            return Outcome("error", p["tool"], "That was already decided.")
        if self.locked_down:
            return Outcome("blocked", p["tool"], "Lockdown is on.")
        # Permission can have been revoked between proposal and approval: re-check.
        pc = self.broker.check(tool.scopes)
        if pc.status != PermStatus.GRANTED:
            return Outcome("denied", p["tool"], "Permission was revoked before approval, so I didn't do it.")
        return self._run(tool, args, p["actor"])

    def lockdown(self) -> int:
        """Kill switch: revoke every grant and reject every pending request."""
        self.locked_down = True
        self.broker.revoke_all()
        n = 0
        for r in self.db.pending_requests():
            if self.db.resolve_request(r["id"], "cancelled"):
                n += 1
        self.db.audit("user", "lockdown", {}, "done", f"{n} pending cancelled")
        return n

    def unlock(self) -> None:
        self.locked_down = False
        self.db.audit("user", "unlock", {}, "done")
