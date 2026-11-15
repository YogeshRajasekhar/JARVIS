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


_WHEN_BIT = (r"(?:(?:at|by|around)\s+\d{1,2}(?::\d{2})?\b(?!\s*(?:am|pm|a\.m|p\.m)))|(?:(?:at|by|around|on|in|this|next|every|each)\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}(?::\d{2})\b|noon|midnight|today|tonight|"
             r"tomorrow(?:\s+(?:morning|afternoon|evening|night))?|day after tomorrow|this (?:morning|afternoon|evening)|(?:every |each )?(?:day|weekday|"
             r"morning|evening|night)|daily|(?:on |next |this |every )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
             r"the \d{1,2}(?:st|nd|rd|th)|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
             r"in \d+\s*(?:minutes?|mins?|hours?|hrs?|days?)|in (?:an?|half an?) (?:hour|minute)|(?:at |in the )?(?:morning|afternoon|evening|night))")
_REMIND_RX = re.compile(r"^(?:please\s+)?(?:can you\s+)?(?:remind me|set (?:a |me a )?reminder|give me a reminder|don'?t let me forget)\s*(?P<body>.*)$", re.I)


def reminder_parts(text: str) -> tuple[str | None, str | None]:
    """'remind me at 6pm to go shopping' / 'remind me to call mom tomorrow at 7' -> ('go shopping', 'at 6pm')."""
    m = _REMIND_RX.match((text or "").strip().rstrip(".!?"))
    if not m:
        return None, None
    body = m.group("body").strip()
    whens = [w.group(0).strip() for w in re.finditer(_WHEN_BIT, body, re.I)]
    label = re.sub(_WHEN_BIT, " ", body, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip(" ,")
    label = re.sub(r"^(?:to|that i (?:need|have|should) to|that i|about|for)\s+", "", label, flags=re.I)
    label = re.sub(r"\s+(?:at|on|by|in|to|and)$", "", label, flags=re.I).strip(" ,")
    when = " ".join(whens) if whens else None
    return (label or None), when


def _lower1(s: str) -> str:
    return s if (len(s) > 1 and s[1].isupper()) or s[:2] == "I " else s[0].lower() + s[1:]


_DUR_RX = re.compile(r"(?P<n>\d+(?:\.\d+)?|an?|half an?|one|two|three|four|five|ten|fifteen|twenty|thirty|forty five|sixty|ninety)\s*"
                     r"(?:and a half\s*)?(?P<u>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b", re.I)
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20,
              "thirty": 30, "forty five": 45, "sixty": 60, "ninety": 90, "half a": .5, "half an": .5}


def parse_duration(text: str) -> int | None:
    """'90 seconds', '1 hour 30 minutes', 'two and a half minutes', 'half an hour' -> seconds."""
    total = 0.0
    for m in _DUR_RX.finditer(text or ""):
        raw = m.group("n").lower()
        n = _NUM_WORDS.get(raw) if raw in _NUM_WORDS else float(raw)
        if "and a half" in m.group(0).lower():
            n += .5
        u = m.group("u").lower()
        total += n * (1 if u.startswith("s") else 60 if u.startswith("m") else 3600)
    return int(round(total)) or None


def say_duration(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s_ = divmod(rem, 60)
    parts = [f"{h} hour" + ("s" if h != 1 else "")] if h else []
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s_ and not h:
        parts.append(f"{s_} second" + ("s" if s_ != 1 else ""))
    return " ".join(parts) or "0 second"


# Apps that also live on the web: if the app isn't installed, open the site instead of failing.
WEB_FALLBACK = {"youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "maps": "https://maps.google.com",
                "google maps": "https://maps.google.com", "instagram": "https://www.instagram.com", "twitter": "https://x.com",
                "x": "https://x.com", "linkedin": "https://www.linkedin.com", "github": "https://github.com", "netflix": "https://www.netflix.com",
                "spotify": "https://open.spotify.com", "whatsapp": "https://web.whatsapp.com", "reddit": "https://www.reddit.com",
                "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com", "chatgpt": "https://chatgpt.com",
                "google": "https://www.google.com", "drive": "https://drive.google.com", "google drive": "https://drive.google.com",
                "calendar": "https://calendar.google.com", "notion": "https://www.notion.so", "telegram": "https://web.telegram.org",
                "swiggy": "https://www.swiggy.com", "zomato": "https://www.zomato.com", "uber": "https://m.uber.com", "ola": "https://www.olacabs.com"}


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
        elif out.status == "done" and isinstance(out.data, dict) and out.data.get("needs_tap"):
            # Android doesn't let a background app open other apps; the phone posted a notification instead.
            text = ("Android won't let me open that while I'm in the background, so it's waiting in your notifications. "
                    "Tap it. To skip this next time, allow \"Open apps from the background\" in JARVIS Settings.")
        return AgentReply(self.name, text, [out])

    def _do_set_alarm(self, cmd, ctx):
        r = resolve(cmd.time, ctx.now, prefer_morning=True)
        if not r or not r.has_time:
            return AgentReply(self.name, "What time should the alarm be?", clarify="time")
        return self._single("device.set_alarm", {"hour": r.start.hour, "minute": r.start.minute, "label": cmd.label or "Alarm"},
                            ctx, f"Alarm set for {r.start.strftime('%I:%M %p').lstrip('0')}.")

    def _do_set_timer(self, cmd, ctx):
        secs = parse_duration(ctx.original) or (cmd.minutes * 60 if cmd.minutes else None)
        if not secs:
            return AgentReply(self.name, "How long should the timer be?", clarify="time")
        secs = max(5, min(secs, 24 * 3600))
        return self._single("device.set_timer", {"seconds": secs, "label": cmd.label or "Timer"}, ctx, f"{say_duration(secs)} timer started.")

    def _do_set_reminder(self, cmd, ctx):
        label0, when0 = reminder_parts(ctx.original)
        if label0:
            cmd.label = label0
        r = resolve(cmd.time, ctx.now) or (resolve(when0, ctx.now) if when0 else None)
        if not r:
            return AgentReply(self.name, f"When should I remind you to {_lower1(cmd.label) if cmd.label else 'do that'}?", clarify="time")
        at = r.start if r.has_time else r.start.replace(hour=9)
        if at <= ctx.now:
            at = at + timedelta(days=1)
        label = (cmd.label or "Reminder").rstrip(".")
        label = label[0].upper() + label[1:]
        rep = self._single("device.set_reminder", {"at_iso": at.isoformat(), "label": label}, ctx,
                           f"I'll remind you to {_lower1(label)} {fmt_time(at, ctx.now)}.")
        if re.search(r"\b(every|each|daily|weekly|every day|weekdays)\b", ctx.original or "", re.I) and rep.outcomes and rep.outcomes[0].status == "done":
            rep.text += " I can't repeat reminders yet, so this is for the next one only."
        return rep

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
        app = re.sub(r"^(?:the|my) ", "", cmd.app.strip(), flags=re.I)
        out = self.propose("device.open_app", {"app": app}, ctx)
        data = out.data if isinstance(out.data, dict) else {}
        if out.status == "done" and data.get("ok") is False and app.lower() in WEB_FALLBACK:
            web = self.propose("device.open_url", {"url": WEB_FALLBACK[app.lower()], "label": app, "app": "browser"}, ctx)
            if web.status == "done" and (web.data or {}).get("ok", True):
                return AgentReply(self.name, f"{app.capitalize()} isn't installed, so I opened it in the browser.", [out, web])
        if out.status == "done" and data.get("ok") is False:
            return AgentReply(self.name, f"I couldn't find an app called {app} on this phone.", [out])
        if out.status == "done" and data.get("needs_tap"):
            return AgentReply(self.name, "Android won't let me open that while I'm in the background, so it's waiting in your notifications. "
                              "Tap it. To skip this next time, allow \"Open apps from the background\" in JARVIS Settings.", [out])
        return AgentReply(self.name, self.say_outcome(out, f"Opening {data.get('app') or app}."), [out])

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
        rep = self._single("device.toggle", {"setting": cmd.setting, "state": state}, ctx)
        # "do not disturb for 2 hours": Android has no timed toggle an app can set, so remind the user to switch it back.
        secs = parse_duration(ctx.original) if re.search(r"\bfor\b", ctx.original or "", re.I) else None
        if secs and state and rep.outcomes and rep.outcomes[0].status == "done" and (rep.outcomes[0].data or {}).get("ok", True):
            at = ctx.now + timedelta(seconds=secs)
            name = {"dnd": "Do Not Disturb", "silent": "silent mode"}.get(cmd.setting, cmd.setting)
            rem = self.propose("device.set_reminder", {"at_iso": at.isoformat(), "label": f"Turn off {name}"}, ctx)
            if rem.status == "done":
                rep.text += f" I can't switch it off by itself, so I'll remind you {fmt_time(at, ctx.now)}."
                rep.outcomes.append(rem)
        return rep

    def _do_battery(self, cmd, ctx):
        out = self.propose("device.battery", {}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        return AgentReply(self.name, f"Battery is at {out.data.get('level')} percent{', charging' if out.data.get('charging') else ''}.", [out])
