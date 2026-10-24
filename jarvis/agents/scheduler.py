"""
Scheduler agent: calendar view/create/move/cancel, free-slot search, conflict resolution.

Conflict policy (deterministic, no LLM):
- A new or moved event that overlaps an existing one is NOT written. Instead the agent offers
  the 3 nearest free slots on the same day that respect the user's stored preferences
  ("no meetings before 10am"), and remembers them so "book the second one" works next turn.
- Cancelling is irreversible, so it always goes to the approval inbox.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from jarvis.agents.base import AgentReply, BaseAgent, TurnContext
from jarvis.core.timeparse import fmt_time, resolve
from jarvis.llm.schemas import CalendarOut
from jarvis.prompts import CALENDAR

WORK_START, WORK_END = 9, 19
PARTS = {"morning": (9, 12), "early morning": (7, 9), "afternoon": (12, 17), "evening": (17, 21), "tonight": (18, 22), "night": (18, 22)}
OPTION_RX = re.compile(r"^(?:book |take |go with |pick |use )?(?:the )?(first|second|third|1st|2nd|3rd|option [123]|[123])(?: one| option| slot)?[.!]?$", re.I)
_ORD = {"first": 0, "1st": 0, "1": 0, "option 1": 0, "second": 1, "2nd": 1, "2": 1, "option 2": 1, "third": 2, "3rd": 2, "3": 2, "option 3": 2}


class SchedulerAgent(BaseAgent):
    name = "scheduler"

    # ------------------------------------------------------------------ helpers
    def _earliest_hour(self) -> int:
        """Reads a 'no meetings before HH' preference from memory, if any."""
        for p in self.svc.graph.by_type("preference"):
            text = (p["label"] + " " + str(p.get("attributes", {}))).lower()
            m = re.search(r"(?:no meetings? before|after)\s*(\d{1,2})", text)
            if m and "meeting" in text:
                return int(m.group(1))
        return WORK_START

    def _events(self, start: datetime, end: datetime, ctx: TurnContext):
        out = self.propose("calendar.list", {"start": start.isoformat(), "end": end.isoformat()}, ctx)
        return out, (out.data or {}).get("events", []) if out.status == "done" else []

    def _find_event(self, target: str, ctx: TurnContext) -> tuple[object, dict | None]:
        now = ctx.now
        out, events = self._events(now - timedelta(hours=2), now + timedelta(days=14), ctx)
        if out.status != "done":
            return out, None
        if not events:
            return out, None
        t = (target or "").lower().strip()
        tr = resolve(t, now)
        if tr and tr.has_time and re.fullmatch(r"(at )?\d{1,2}(:\d{2})?\s*(am|pm)?", t):
            for e in events:  # "my 3pm"
                s = datetime.fromisoformat(e["start"])
                if s.hour == tr.start.hour and s.minute == tr.start.minute:
                    return out, e
        focus_id = ctx.focus.get("last_event")
        if t in {"it", "that", "the meeting", "this meeting", "that meeting", ""} and focus_id:
            return out, next((e for e in events if e["id"] == focus_id), None)
        scored = []
        for e in events:
            title = e["title"].lower()
            s = difflib.SequenceMatcher(None, t, title).ratio()
            if t and t in title:
                s += 1
            s += 0.5 * sum(1 for w in re.findall(r"\w{3,}", t) if w in title or any(w in a.lower() for a in e["attendees"]))
            scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        return out, scored[0][1] if scored and scored[0][0] >= 0.6 else None

    def _slots(self, day: datetime, minutes: int, window: tuple[int, int] | None, ctx: TurnContext, around=None):
        # An explicit window ("tomorrow evening") wins; otherwise working hours + stored preference.
        lo, hi = window if window else (max(WORK_START, self._earliest_hour()), WORK_END)
        start = day.replace(hour=lo, minute=0, second=0, microsecond=0)
        if start.date() == ctx.now.date() and start < ctx.now:
            start = (ctx.now + timedelta(minutes=30 - ctx.now.minute % 30)).replace(second=0, microsecond=0)
        end = day.replace(hour=hi, minute=0, second=0, microsecond=0)
        slots = self.svc.calendar.free_slots(start, end, minutes, limit=24) if end > start else []
        if around:
            slots.sort(key=lambda s: abs((s[0] - around).total_seconds()))
        return slots[:3]

    @staticmethod
    def _slot_text(slots, now):
        return "; ".join(f"{i + 1}) {fmt_time(s, now)}" for i, (s, _) in enumerate(slots))

    # ------------------------------------------------------------------ entry
    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        m = OPTION_RX.match(text.strip())
        if m and ctx.focus.get("slot_options"):
            return self._book_option(_ORD[m.group(1).lower()], ctx)
        cmd: CalendarOut = self.extract(CalendarOut, CALENDAR, text, ctx, task="calendar")
        return getattr(self, f"_do_{cmd.action}")(cmd, ctx)

    # ------------------------------------------------------------------ actions
    def _do_list(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        r = resolve(cmd.when or "today", ctx.now)
        day = (r.start if r else ctx.now).replace(hour=0, minute=0, second=0, microsecond=0)
        span = timedelta(days=7) if cmd.when and "week" in cmd.when.lower() else timedelta(days=1)
        out, events = self._events(day, day + span, ctx)
        if (b := self.blocked_reply(out)):
            return b
        if cmd.target:  # "when is my thesis sync"
            _, ev = self._find_event(cmd.target, ctx)
            if ev:
                s = datetime.fromisoformat(ev["start"])
                return AgentReply(self.name, f"{ev['title']} is {fmt_time(s, ctx.now)}.", [out],
                                  cards=[{"type": "events", "events": [ev]}], focus={"last_event": ev["id"]})
        label = "today" if day.date() == ctx.now.date() else ("tomorrow" if (day.date() - ctx.now.date()).days == 1 else day.strftime("%A"))
        if span.days == 7:
            label = "this week"
        if not events:
            return AgentReply(self.name, f"Your calendar is clear {label}.", [out], cards=[{"type": "events", "events": []}])
        if day.date() == ctx.now.date():
            upcoming = [e for e in events if datetime.fromisoformat(e["end"]) > ctx.now]
            if not upcoming:
                tom = day + timedelta(days=1)
                _, t_ev = self._events(tom, tom + timedelta(days=1), ctx)
                nxt = (f" Tomorrow starts with {t_ev[0]['title']} at "
                       f"{datetime.fromisoformat(t_ev[0]['start']).strftime('%I:%M %p').lstrip('0')}.") if t_ev else ""
                return AgentReply(self.name, "You're done for today." + nxt, [out],
                                  cards=[{"type": "events", "events": t_ev[:4]}] if t_ev else [])
            events = upcoming
        parts = [f"{e['title']} at {datetime.fromisoformat(e['start']).strftime('%I:%M %p').lstrip('0')}" for e in events[:5]]
        more = f", and {len(events) - 5} more" if len(events) > 5 else ""
        n = len(events)
        return AgentReply(self.name, f"You have {n} event{'s' if n != 1 else ''} {label}: " + ", ".join(parts) + more + ".",
                          [out], cards=[{"type": "events", "events": events}], focus={"last_event": events[0]["id"]})

    def _do_check(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        r = resolve(cmd.when, ctx.now)
        if not r:
            return AgentReply(self.name, "Which time should I check?", clarify="time")
        if not r.has_time:
            return self._do_list(CalendarOut(action="list", when=cmd.when), ctx)
        end = r.start + timedelta(minutes=cmd.duration_minutes or 60)
        out, _ = self._events(r.start - timedelta(days=1), end + timedelta(days=1), ctx)
        if (b := self.blocked_reply(out)):
            return b
        clashes = self.svc.calendar.conflicts(r.start, end)
        if not clashes:
            return AgentReply(self.name, f"You're free {fmt_time(r.start, ctx.now)}.", [out])
        c = clashes[0]
        return AgentReply(self.name, f"No, you have {c.title} {fmt_time(c.start, ctx.now)}.", [out],
                          focus={"last_event": c.id})

    def _do_find_slot(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        r = resolve(cmd.when or "today", ctx.now)
        day = r.start if r else ctx.now
        out, _ = self._events(day.replace(hour=0, minute=0), day.replace(hour=23, minute=59), ctx)
        if (b := self.blocked_reply(out)):
            return b
        window = PARTS.get(r.part_of_day) if r and r.part_of_day else None
        minutes = cmd.duration_minutes or 60
        slots = self._slots(day, minutes, window, ctx)
        if not slots:
            return AgentReply(self.name, f"I couldn't find {minutes} free minutes then. Want me to look at another day?", [out])
        return AgentReply(
            self.name, f"Free slots: {self._slot_text(slots, ctx.now)}. Say 'book the first one' to block it.", [out],
            cards=[{"type": "slots", "slots": [[a.isoformat(), b.isoformat()] for a, b in slots]}],
            focus={"slot_options": {"title": cmd.title or "Focus time", "attendees": cmd.attendees,
                                    "slots": [[a.isoformat(), b.isoformat()] for a, b in slots]}})

    def _book_option(self, idx: int, ctx: TurnContext) -> AgentReply:
        opt = ctx.focus["slot_options"]
        if idx >= len(opt["slots"]):
            return AgentReply(self.name, f"There are only {len(opt['slots'])} options.")
        s, e = opt["slots"][idx]
        if opt.get("move_event"):
            ev = opt["move_event"]
            out = self.propose("calendar.move", {"event_id": ev["id"], "title": ev["title"], "start": s, "end": e,
                                                 "attendees": ev.get("attendees", [])}, ctx)
        else:
            out = self.propose("calendar.create", {"title": opt["title"], "start": s, "end": e,
                                                   "attendees": opt.get("attendees") or [], "location": opt.get("location")}, ctx)
        focus = {"slot_options": None}
        if out.status == "done":
            focus["last_event"] = out.data["event"]["id"]
        return AgentReply(self.name, self.say_outcome(out), [out], focus=focus)

    def _do_create(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        r = resolve(cmd.when, ctx.now)
        title = cmd.title or "Meeting"
        if not r:
            return AgentReply(self.name, f"When should I schedule {title}?", clarify="time")
        if not r.has_time:
            return AgentReply(self.name, f"What time {fmt_time(r.start, ctx.now).split(' at ')[0]}?", clarify="time")
        if r.start < ctx.now - timedelta(minutes=5):
            return AgentReply(self.name, f"{fmt_time(r.start, ctx.now)} has already passed. Did you mean a later time?", clarify="time")
        minutes = cmd.duration_minutes or (30 if re.search(r"\b(call|sync|standup|chat)\b", title, re.I) else 60)
        start, end = r.start, r.start + timedelta(minutes=minutes)
        people = [self.svc.graph.node(self.svc.graph.resolve(a, "person"))["label"] if self.svc.graph.resolve(a, "person") else a
                  for a in cmd.attendees]
        out, _ = self._events(start - timedelta(days=1), end + timedelta(days=1), ctx)
        if (b := self.blocked_reply(out)):
            return b
        clashes = self.svc.calendar.conflicts(start, end)
        warn = ""
        if start.hour < self._earliest_hour():
            warn = f" Note: that's before {self._earliest_hour()} AM, which you said you avoid."
        if clashes:
            slots = self._slots(start, minutes, None, ctx, around=start)
            if not slots:
                return AgentReply(self.name, f"That clashes with {clashes[0].title} and the day is full. Try another day?", [out])
            return AgentReply(
                self.name,
                f"That clashes with {clashes[0].title}. Nearest free slots: {self._slot_text(slots, ctx.now)}. Which one?",
                [out], cards=[{"type": "slots", "slots": [[a.isoformat(), b.isoformat()] for a, b in slots]}],
                focus={"slot_options": {"title": title, "attendees": people, "location": cmd.location,
                                        "slots": [[a.isoformat(), b.isoformat()] for a, b in slots]}})
        res = self.propose("calendar.create", {"title": title, "start": start.isoformat(), "end": end.isoformat(),
                                               "attendees": people, "location": cmd.location}, ctx)
        focus = {"last_person": people[0]} if people else {}
        if res.status == "done":
            focus["last_event"] = res.data["event"]["id"]
            return AgentReply(self.name, f"Booked {title} {fmt_time(start, ctx.now)}.{warn}", [out, res], focus=focus,
                              fact=f"{title} is {fmt_time(start, ctx.now)}" + (f" at {cmd.location}" if cmd.location else ""),
                              cards=[{"type": "events", "events": [res.data["event"]]}])
        return AgentReply(self.name, self.say_outcome(res) + warn, [out, res], focus=focus,
                          fact=f"{title} is {fmt_time(start, ctx.now)}" + (f" at {cmd.location}" if cmd.location else ""))

    def _do_move(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        out, ev = self._find_event(cmd.target or "", ctx)
        if (b := self.blocked_reply(out)):
            return b
        if not ev:
            return AgentReply(self.name, f"I couldn't find '{cmd.target}' on your calendar in the next two weeks.", [out])
        old_s, old_e = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
        nw = (cmd.new_when or cmd.when or "").strip()
        r = resolve(nw, old_s.replace(hour=0, minute=0)) if nw else None
        if not r:
            return AgentReply(self.name, f"When should I move {ev['title']} to?", clarify="time", focus={"last_event": ev["id"]})
        if not r.has_time:  # new day, same clock time
            new_s = r.start.replace(hour=old_s.hour, minute=old_s.minute)
        elif re.search(r"(today|tomorrow|day|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|\d{1,2}/|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", nw, re.I):
            new_s = resolve(nw, ctx.now).start
        else:  # "push my standup to 11" -> same day, new time
            new_s = old_s.replace(hour=r.start.hour, minute=r.start.minute)
        new_e = new_s + (old_e - old_s)
        clashes = self.svc.calendar.conflicts(new_s, new_e, ignore_id=ev["id"])
        if clashes:
            slots = self._slots(new_s, int((old_e - old_s).total_seconds() // 60), None, ctx, around=new_s)
            return AgentReply(self.name, f"{fmt_time(new_s, ctx.now)} clashes with {clashes[0].title}. Free options: "
                              f"{self._slot_text(slots, ctx.now)}. Which one?", [out],
                              focus={"slot_options": {"title": ev["title"], "move_event": ev,
                                                      "slots": [[a.isoformat(), b.isoformat()] for a, b in slots]}})
        res = self.propose("calendar.move", {"event_id": ev["id"], "title": ev["title"], "start": new_s.isoformat(),
                                             "end": new_e.isoformat(), "attendees": ev.get("attendees", [])}, ctx)
        return AgentReply(self.name, self.say_outcome(res, f"Moved {ev['title']} to {fmt_time(new_s, ctx.now)}."),
                          [out, res], focus={"last_event": ev["id"]}, fact=f"{ev['title']} moved to {fmt_time(new_s, ctx.now)}")

    def _do_cancel(self, cmd: CalendarOut, ctx: TurnContext) -> AgentReply:
        out, ev = self._find_event(cmd.target or cmd.when or "", ctx)
        if (b := self.blocked_reply(out)):
            return b
        if not ev:
            return AgentReply(self.name, f"I couldn't find '{cmd.target}' on your calendar.", [out])
        res = self.propose("calendar.cancel", {"event_id": ev["id"], "title": ev["title"], "start": ev["start"]}, ctx)
        return AgentReply(self.name, self.say_outcome(res), [out, res], focus={"last_event": ev["id"]})
