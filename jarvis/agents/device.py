"""
Device agent: the phone itself, only through scoped tools.

What it can do on Android (all via official APIs, no screen scraping):
alarms/timers (AlarmClock intents), reminders (scheduled notifications), open apps
(launch intents, payment/banking/password apps blocked), calls (opens dialer, you press call),
SMS (approval card first), contacts lookup, location, filtered notifications, quick settings.
"""

from __future__ import annotations

import re
from datetime import timedelta

from jarvis.agents.base import AgentReply, BaseAgent, TurnContext
from jarvis.core.timeparse import fmt_time, resolve
from jarvis.llm.schemas import DeviceOut
from jarvis.prompts import DEVICE


# "call me at 6", "ring me in 20 minutes about the laundry": JARVIS phones YOU (a ringing call, not the dialer).
CALL_ME_RX = re.compile(r"^(?:please\s+)?(?:call|ring|phone)\s+me\b(?P<rest>.*)$", re.I)
ABOUT_RX = re.compile(r"\b(?P<conn>about|to|for|regarding|and (?:remind|tell) me (?:to|about))\s+(?P<what>.+)$", re.I)


class DeviceAgent(BaseAgent):
    name = "device"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        m = CALL_ME_RX.match(text.strip().rstrip(".!?"))
        if m:
            return self._call_me(m.group("rest").strip(), ctx)
        cmd: DeviceOut = self.extract(DeviceOut, DEVICE, text, ctx, task="device")
        return getattr(self, f"_do_{cmd.action}")(cmd, ctx)

    def _single(self, tool: str, args: dict, ctx: TurnContext, done: str | None = None) -> AgentReply:
        out = self.propose(tool, args, ctx)
        text = self.say_outcome(out, done)
        if out.status == "done" and isinstance(out.data, dict) and out.data.get("ok") is False:
            text = f"That didn't work: {out.data.get('error', 'unknown error')}."
        return AgentReply(self.name, text, [out])

    def _do_set_alarm(self, cmd, ctx):
        r = resolve(cmd.time, ctx.now, prefer_morning=True)
        if not r or not r.has_time:
            return AgentReply(self.name, "What time should the alarm be?", clarify="time")
        return self._single("device.set_alarm", {"hour": r.start.hour, "minute": r.start.minute, "label": cmd.label or "Alarm"},
                            ctx, f"Alarm set for {r.start.strftime('%I:%M %p').lstrip('0')}.")

    def _do_set_timer(self, cmd, ctx):
        minutes = cmd.minutes or 5
        return self._single("device.set_timer", {"seconds": minutes * 60, "label": cmd.label or "Timer"}, ctx,
                            f"{minutes} minute timer started.")

    def _do_set_reminder(self, cmd, ctx):
        r = resolve(cmd.time, ctx.now)
        if not r:
            return AgentReply(self.name, f"When should I remind you to {cmd.label or 'do that'}?", clarify="time")
        at = r.start if r.has_time else r.start.replace(hour=9)
        if at <= ctx.now:
            at = at + timedelta(days=1)
        label = (cmd.label or "Reminder").rstrip(".")
        return self._single("device.set_reminder", {"at_iso": at.isoformat(), "label": label}, ctx,
                            f"I'll remind you to {label[0].lower() + label[1:]} {fmt_time(at, ctx.now)}.")

    def _call_me(self, rest: str, ctx: TurnContext) -> AgentReply:
        what, conn = None, "about"
        a = ABOUT_RX.search(rest)
        if a:
            what, rest = a.group("what").strip(), rest[:a.start()].strip()
            conn = "to" if a.group("conn").lower().endswith("to") else "about"
        if not rest:
            return AgentReply(self.name, "When should I call you?", clarify="time")
        r = resolve(rest, ctx.now)
        if not r or not r.has_time and not re.search(r"\bin\s+\d", rest):
            return AgentReply(self.name, "What time should I call you?", clarify="time")
        at = r.start
        if at <= ctx.now:
            at = at + timedelta(days=1)
        label = (what or "Your call from JARVIS").rstrip(".")
        about = f" {conn} {what}" if what else ""
        return self._single("device.set_reminder", {"at_iso": at.isoformat(), "label": label, "call": True}, ctx,
                            f"I'll call you {fmt_time(at, ctx.now)}{about}.")

    def _do_open_app(self, cmd, ctx):
        if not cmd.app:
            return AgentReply(self.name, "Which app?", clarify="app")
        return self._single("device.open_app", {"app": cmd.app}, ctx, f"Opening {cmd.app}.")

    def _contact(self, name, ctx):
        c, perm = self.svc.lookup_contact(name, self.name, resume=ctx.original or None, need="phone")
        return c, perm

    def _do_call(self, cmd, ctx):
        who = cmd.contact or ctx.focus.get("last_person")
        if not who:
            return AgentReply(self.name, "Who should I call?", clarify="contact")
        if any(ch.isdigit() for ch in who) and sum(ch.isdigit() for ch in who) >= 7:
            return self._single("device.call", {"number": who, "name": ""}, ctx)
        c, perm = self._contact(who, ctx)
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if not c or not c.get("phone"):
            return AgentReply(self.name, f"I couldn't find a number for {who}.")
        return self._single("device.call", {"number": c["phone"], "name": c["name"]}, ctx)

    def _do_send_sms(self, cmd, ctx):
        who = cmd.contact or ctx.focus.get("last_person")
        if not who:
            return AgentReply(self.name, "Who should I text?", clarify="contact")
        if not cmd.message:
            return AgentReply(self.name, f"What should the message to {who} say?", clarify="content")
        c, perm = self._contact(who, ctx)
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if not c or not c.get("phone"):
            return AgentReply(self.name, f"I couldn't find a number for {who}.")
        return self._single("device.send_sms", {"number": c["phone"], "name": c["name"], "text": cmd.message}, ctx)

    def _do_find_contact(self, cmd, ctx):
        if not cmd.contact:
            return AgentReply(self.name, "Whose contact details?", clarify="contact")
        c, perm = self._contact(cmd.contact, ctx)
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if not c:
            return AgentReply(self.name, f"No contact named {cmd.contact}.")
        bits = [x for x in [c.get("phone"), c.get("email")] if x]
        return AgentReply(self.name, f"{c['name']}: {', '.join(bits)}.", focus={"last_person": c["name"]})

    def _do_get_location(self, cmd, ctx):
        out = self.propose("device.location", {}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        return AgentReply(self.name, f"You're near {out.data.get('name') or 'your current location'}.", [out])

    def _do_read_notifications(self, cmd, ctx):
        out = self.propose("device.notifications", {"limit": 6}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        items = out.data.get("notifications", [])
        if not items:
            return AgentReply(self.name, "No new notifications.", [out])
        lines = [f"{n['app']}: {n['title']}, {n['text']}" for n in items[:4]]
        return AgentReply(self.name, f"{len(items)} notifications. " + ". ".join(lines) + ". Banking and OTP alerts are hidden.",
                          [out], cards=[{"type": "notifications", "items": items}])

    def _do_toggle(self, cmd, ctx):
        if not cmd.setting:
            return AgentReply(self.name, "Which setting?", clarify="setting")
        state = True if cmd.state is None else cmd.state
        return self._single("device.toggle", {"setting": cmd.setting, "state": state}, ctx)

    def _do_battery(self, cmd, ctx):
        out = self.propose("device.battery", {}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        return AgentReply(self.name, f"Battery is at {out.data.get('level')} percent{', charging' if out.data.get('charging') else ''}.", [out])
