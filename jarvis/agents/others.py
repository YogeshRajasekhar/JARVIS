"""GitHub, Places, Briefing, Planner and Chat agents."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from src.agents.planner import PlanStep, StepStatus, Task, execute_next_step
from jarvis.agents.base import AgentReply, BaseAgent, TurnContext
from jarvis.core.permissions import PermStatus
from jarvis.core.timeparse import fmt_time
from jarvis.integrations.places import Place
from jarvis.llm.schemas import GithubOut, PlacesOut
from jarvis.llm.structured import StructuredOutputError, complete_json
from jarvis.prompts import GITHUB, PERSONA, PLAN, PLACES
from pydantic import BaseModel, Field


# ============================================================================ GitHub
class GitHubAgent(BaseAgent):
    name = "github"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd: GithubOut = self.extract(GithubOut, GITHUB, text, ctx, task="github")
        repo = cmd.repo
        if cmd.action == "create_issue":
            if not cmd.title:
                return AgentReply(self.name, "What should the issue say?", clarify="content")
            out = self.propose("github.create_issue", {"repo": repo, "title": cmd.title, "body": cmd.body or ""}, ctx)
            return AgentReply(self.name, self.say_outcome(out), [out])
        if cmd.action == "comment_issue":
            if not cmd.number or not cmd.body:
                return AgentReply(self.name, "Which issue number, and what should the comment say?", clarify="content")
            out = self.propose("github.comment", {"repo": repo, "number": cmd.number, "body": cmd.body}, ctx)
            return AgentReply(self.name, self.say_outcome(out), [out])
        op = {"notifications": "notifications", "list_prs": "list_prs", "list_issues": "list_issues",
              "ci_status": "ci_status", "repo_summary": "repo_summary"}[cmd.action]
        out = self.propose("github.read", {"op": op, "repo": repo}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        r = out.data["result"]
        name = self.svc.github.full_name(repo).split("/")[-1]
        if op == "notifications":
            text = (f"{len(r)} GitHub notifications. " + ". ".join(f"{n['title']} in {n['repo'].split('/')[-1]} ({n['reason'].replace('_', ' ')})" for n in r[:3]) + ".") if r else "No GitHub notifications."
        elif op == "list_prs":
            text = (f"{len(r)} open pull request{'s' if len(r) != 1 else ''} on {name}: " + ", ".join(f"#{p['number']} {p['title']}" for p in r[:4]) + ".") if r else f"No open pull requests on {name}."
        elif op == "list_issues":
            text = (f"{len(r)} open issue{'s' if len(r) != 1 else ''} on {name}: " + ", ".join(f"#{i['number']} {i['title']}" for i in r[:4]) + ".") if r else f"No open issues on {name}."
        elif op == "ci_status":
            st = r.get("state")
            word = {"success": "passing", "failure": "failing", "in_progress": "running", "queued": "queued", "none": "not set up"}.get(st, st)
            text = f"The latest {r.get('workflow') or 'CI'} run on {name} is {word}" + (f" on {r['branch']}" if r.get("branch") else "") + "."
        else:
            text = f"{name}: {r.get('open_issues', 0)} open issues, default branch {r.get('default_branch')}, last push {r.get('last_push')}."
        return AgentReply(self.name, text, [out], cards=[{"type": "github", "op": op, "data": r}])


# ============================================================================ Places
class PlacesAgent(BaseAgent):
    name = "places"

    def _here(self, ctx: TurnContext):
        out = self.propose("device.location", {}, ctx)
        if out.status != "done":
            return None, out
        d = out.data
        return Place(d.get("name") or "your location", d["lat"], d["lon"]), None

    def _place(self, name: str | None, ctx: TurnContext, near=None):
        if not name:
            return None
        nid = self.svc.graph.resolve(name, "place")
        if nid:
            a = self.svc.graph.node(nid).get("attributes", {})
            if "lat" in a:
                return Place(self.svc.graph.node(nid)["label"], float(a["lat"]), float(a["lon"]))
        return self.svc.places.geocode(name, near)

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd: PlacesOut = self.extract(PlacesOut, PLACES, text, ctx, task="places")
        mode = cmd.mode or "driving"
        if cmd.action == "find_place":
            p = self._place(cmd.place, ctx)
            if not p:
                return AgentReply(self.name, f"I couldn't find {cmd.place}.")
            return AgentReply(self.name, f"{p.name} is at {p.address or f'{p.lat:.4f}, {p.lon:.4f}'}.",
                              cards=[{"type": "place", "name": p.name, "lat": p.lat, "lon": p.lon}])
        if cmd.action == "travel_time":
            dest = self._place(cmd.destination, ctx)
            if not dest:
                return AgentReply(self.name, f"I couldn't find {cmd.destination}.")
            if cmd.origin:
                origin = self._place(cmd.origin, ctx)
                if not origin:
                    return AgentReply(self.name, f"I couldn't find {cmd.origin}.")
            else:
                origin, perm = self._here(ctx)
                if perm is not None:
                    return AgentReply(self.name, self.say_outcome(perm), [perm])
            r = self.svc.places.route(origin, dest, mode)
            return AgentReply(self.name, f"About {r['minutes']} minutes by {self._mode_word(mode)} from {origin.name} to {dest.name}, {r['km']} km.",
                              cards=[{"type": "route", **r, "from": origin.name, "to": dest.name}])
        if cmd.action == "meeting_point":
            pts = [p for p in (self._place(n, ctx) for n in cmd.people) if p]
            if len(pts) < 2:
                here, perm = self._here(ctx)
                if perm is not None:
                    return AgentReply(self.name, self.say_outcome(perm), [perm])
                pts.append(here)
            if len(pts) < 2:
                return AgentReply(self.name, "Tell me where each of you is starting from.", clarify="places")
            mid = self.svc.places.midpoint(pts)
            spots = self.svc.places.nearby(mid, "cafe")
            if not spots:
                return AgentReply(self.name, "I couldn't find a good spot in between.")
            times = [self.svc.places.route(p, spots[0], mode)["minutes"] for p in pts]
            return AgentReply(self.name, f"{spots[0].name} in {spots[0].address.split(',')[0]} is roughly halfway: "
                              + ", ".join(f"{t} minutes from {p.name}" for p, t in zip(pts, times)) + ".",
                              cards=[{"type": "places", "items": [{"name": s.name, "address": s.address} for s in spots]}])
        # when_to_leave
        now = ctx.now
        out = self.propose("calendar.list", {"start": now.isoformat(), "end": (now + timedelta(days=2)).isoformat()}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        events = [e for e in out.data["events"] if datetime.fromisoformat(e["start"]) > now]
        if cmd.event:
            words = [w for w in re.findall(r"\w{3,}", cmd.event.lower()) if w not in {"meeting", "the", "with", "my"}]
            events = [e for e in events if any(w in (e["title"] + " " + " ".join(e["attendees"])).lower() for w in words)] or events
        events = [e for e in events if e.get("location")]
        if not events:
            return AgentReply(self.name, "I don't see an upcoming event with a location.", [out])
        ev = events[0]
        dest = self._place(ev["location"], ctx)
        here, perm = self._here(ctx)
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if not dest:
            return AgentReply(self.name, f"I couldn't locate {ev['location']}.", [out])
        r = self.svc.places.route(here, dest, mode)
        start = datetime.fromisoformat(ev["start"])
        leave = start - timedelta(minutes=r["minutes"] + 10)
        if leave <= now:
            return AgentReply(self.name, f"Leave now. {ev['title']} starts {fmt_time(start, now)} and it's {r['minutes']} minutes away.", [out])
        return AgentReply(self.name, f"Leave by {leave.strftime('%I:%M %p').lstrip('0')} for {ev['title']} at {ev['location']}. "
                          f"It's about {r['minutes']} minutes by {self._mode_word(mode)}, plus 10 minutes buffer.", [out])

    @staticmethod
    def _mode_word(mode: str) -> str:
        return {"driving": "road", "walking": "foot", "cycling": "bike", "transit": "public transport"}.get(mode, mode)


# ============================================================================ Briefing
class BriefingAgent(BaseAgent):
    """
    Proactive summary of the day. Only uses sources the user already granted, so a briefing
    never triggers a burst of permission prompts; missing sources are mentioned once.
    """
    name = "briefing"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        now, parts, outs, cards, missing = ctx.now, [], [], [], []
        b = self.svc.broker

        def granted(scope):
            return b.check([scope]).status == PermStatus.GRANTED

        hour = now.hour
        greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        parts.append(f"{greet}, {self.svc.settings.user_name}.")
        if granted("calendar.read"):
            day_end = now.replace(hour=23, minute=59)
            o = self.propose("calendar.list", {"start": now.isoformat(), "end": day_end.isoformat()}, ctx)
            outs.append(o)
            evs = o.data["events"] if o.ok else []
            if evs:
                nxt = evs[0]
                parts.append(f"{len(evs)} more event{'s' if len(evs) != 1 else ''} today. Next is {nxt['title']} {fmt_time(datetime.fromisoformat(nxt['start']), now)}.")
                cards.append({"type": "events", "events": evs})
            else:
                tomorrow = now.replace(hour=0, minute=0) + timedelta(days=1)
                o2 = self.propose("calendar.list", {"start": tomorrow.isoformat(), "end": (tomorrow + timedelta(days=1)).isoformat()}, ctx)
                t_evs = o2.data["events"] if o2.ok else []
                parts.append("Nothing else on your calendar today." + (f" Tomorrow starts with {t_evs[0]['title']} {fmt_time(datetime.fromisoformat(t_evs[0]['start']), now)}." if t_evs else ""))
        else:
            missing.append("calendar")
        if granted("mail.read"):
            o = self.propose("mail.read", {"limit": 10, "unread_only": True}, ctx)
            outs.append(o)
            mails = o.data["emails"] if o.ok else []
            from jarvis.core.redaction import injection_score
            real = [m for m in mails if not injection_score(m.subject + m.body)]
            if real:
                parts.append(f"{len(real)} unread email{'s' if len(real) != 1 else ''}, including {real[0].sender} about {real[0].subject}.")
        else:
            missing.append("email")
        if granted("github.read"):
            o = self.propose("github.read", {"op": "notifications", "repo": None}, ctx)
            if o.ok and o.data["result"]:
                parts.append(f"{len(o.data['result'])} GitHub notification{'s' if len(o.data['result']) != 1 else ''}.")
        commitments = self.svc.graph.by_type("commitment")
        if commitments:
            c = commitments[0]
            due = c.get("attributes", {}).get("due")
            parts.append(f"You promised: {c['label']}" + (f", due {due}." if due else "."))
        if missing:
            parts.append(f"I can include your {' and '.join(missing)} once you allow access.")
        return AgentReply(self.name, " ".join(parts), outs, cards=cards)


# ============================================================================ Planner
class PlanStepOut(BaseModel):
    request: str
    agent: str = "scheduler"
    urgency: int = Field(3, ge=1, le=5)
    importance: int = Field(3, ge=1, le=5)


class PlanOut(BaseModel):
    steps: list[PlanStepOut] = Field(min_length=1, max_length=6)


class PlannerAgent(BaseAgent):
    """
    Turns a goal into prioritised steps (smart LLM), then orders them with v1's deterministic
    priority logic (urgency x importance, highest first, ties keep the model's order).
    The brain then executes the steps one by one through the normal agents, so every write
    still goes through permissions and approvals.
    """
    name = "planner"

    def plan(self, goal: str, ctx: TurnContext) -> list[PlanStep]:
        try:
            p: PlanOut = complete_json(self.svc.smart_llm, PlanOut, PLAN,
                                       f"Current time: {ctx.now:%A %d %B %Y %H:%M}\nGoal: {goal}", task="plan", max_tokens=700)
        except StructuredOutputError:
            return []
        steps = []
        for i, s in enumerate(p.steps):
            steps.append(PlanStep(Task(f"step-{i + 1}", s.request, s.agent, s.urgency, s.importance)))
        ordered, work = [], [PlanStep(s.task, StepStatus.PENDING) if s.status == StepStatus.PENDING else s for s in steps]
        while (nxt := execute_next_step(work)) is not None:
            ordered.append(nxt)
            nxt.status = StepStatus.DONE
        return ordered

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        steps = self.plan(text, ctx)
        if not steps:
            return AgentReply(self.name, "I couldn't build a plan for that. Can you break it down a bit?")
        listing = "; ".join(f"{i + 1}) {s.task.description}" for i, s in enumerate(steps))
        return AgentReply(self.name, f"Here's the plan: {listing}.",
                          cards=[{"type": "plan", "steps": [{"request": s.task.description, "agent": s.task.target_agent,
                                                             "priority": s.priority} for s in steps]}],
                          focus={"plan_steps": [s.task.description for s in steps]})


# ============================================================================ Chat / persona
class ChatAgent(BaseAgent):
    name = "chat"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        system = PERSONA.format(user_name=self.svc.settings.user_name, now=ctx.now.strftime("%A %d %B, %I:%M %p"))
        try:
            reply = self.svc.smart_llm.complete(f"User: {text}", system=system, temperature=0.6, max_tokens=200,
                                                task="chat", history=ctx.history[-6:])
        except Exception:
            reply = "I'm having trouble reaching my language model. Direct commands still work."
        reply = re.sub(r"[*_#`]", "", reply).strip()
        return AgentReply(self.name, reply or "Sorry, I didn't catch that.")
