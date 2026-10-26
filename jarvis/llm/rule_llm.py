"""
RuleLLM: a deterministic, offline stand-in that honours the same JSON contracts as the
cloud models (see prompts.py / schemas.py).

Two jobs:
1. Last link of every FallbackLLM chain: when every free provider is rate-limited or the
   phone is offline, JARVIS still handles common commands instead of failing.
2. Makes the whole pipeline testable without network or keys (this sandbox has none).

It is intentionally simple (regex), so it handles the common phrasings well and falls back
to a safe "chat" answer otherwise.
"""

from __future__ import annotations

import json
import re

from jarvis.router_rules import fast_route, score

TIME_RX = r"(?:(?:at|by|around)\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)"
DAY_RX = r"(?:today|tonight|tomorrow|day after tomorrow|(?:next |this )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next week)"
PART_RX = r"(?:morning|afternoon|evening|night)"
WHEN_RX = re.compile(
    rf"\b((?:{DAY_RX})(?:\s+{PART_RX})?(?:\s+{TIME_RX})?|{TIME_RX}(?:\s+{DAY_RX})?(?:\s+{PART_RX})?|in \d+ (?:minutes?|hours?|days?))\b",
    re.I)
NAME_RX = r"(?:Prof\.?\s+|Dr\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
STOP_NAMES = {"I", "Me", "My", "The", "A", "An", "Tomorrow", "Today", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday", "Remind", "Remember", "Set", "Schedule", "Book", "Email", "Mail", "Send",
              "Text", "Call", "What", "When", "Where", "Who", "How", "Is", "Are", "Can", "Please", "Hey", "Jarvis",
              "Slack", "GitHub", "Github", "Cancel", "Move", "Push", "Reply", "Draft", "Tell", "Open", "Find", "Post",
              "Turn", "Any", "Show", "Check", "Brief", "Plan", "Help", "Lab", "Thesis", "Meeting", "Call With"}


def _request(prompt: str) -> str:
    m = list(re.finditer(r"^(?:Request|User|Goal|Message):\s*(.+)$", prompt, re.M))
    return m[-1].group(1).strip() if m else prompt.strip().splitlines()[-1]


def _names(text: str) -> list[str]:
    out = []
    for m in re.finditer(NAME_RX, text):
        n = m.group(0).strip()
        first = n.split()[0]
        if first in STOP_NAMES or n in STOP_NAMES:
            # "Call Arun" -> keep second word
            rest = n.split()[1:]
            if rest and rest[0] not in STOP_NAMES and rest[0][0].isupper():
                out.append(" ".join(rest))
            continue
        out.append(n)
    seen, res = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower())
            res.append(n)
    return res


def _when(text: str) -> str | None:
    hits = [m.group(1).strip() for m in WHEN_RX.finditer(text) if re.search(r"\d|today|tonight|tomorrow|day|noon|week|morning|afternoon|evening|night", m.group(1), re.I)]
    if not hits:
        return None
    return max(hits, key=len)


def _duration(text: str) -> int | None:
    m = re.search(r"for (\d+)\s*(minutes?|mins?|hours?|hrs?|h)\b", text, re.I)
    if not m:
        m2 = re.search(r"for (an|one|half an) hour", text, re.I)
        if m2:
            return 30 if m2.group(1).lower() == "half an" else 60
        return None
    n = int(m.group(1))
    return n * 60 if m.group(2).lower().startswith("h") else n


class RuleLLM:
    name = "rule-offline"
    is_offline_engine = True

    def complete(self, prompt: str, system: str | None = None, *, task: str | None = None, **kw) -> str:
        self._cloud_errors = kw.get("cloud_errors") or []
        handler = getattr(self, f"_t_{task}", None)
        if handler is None:
            return self._t_chat(prompt, system or "")
        out = handler(prompt, system or "")
        return out if isinstance(out, str) else json.dumps(out)

    # ------------------------------------------------------------------ routing
    def _t_route(self, prompt, system):
        text = _request(prompt)
        agent, conf = fast_route(text)
        if agent:
            return {"agent": agent, "confidence": conf, "clarify": None}
        s = score(text)
        if s:
            best = max(s, key=s.get)
            return {"agent": best, "confidence": 0.55, "clarify": None}
        return {"agent": "chat", "confidence": 0.4, "clarify": None}

    def _t_split(self, prompt, system):
        text = _request(prompt)
        parts = re.split(r"\s*(?:;|,?\s+and then\s+|,?\s+then\s+|,?\s+and also\s+|,?\s+after that\s+|"
                         r",?\s+and\s+(?=(?:mail|email|text|message|tell|send|remind|set|book|schedule|turn|open|call|post)\b))\s*",
                         text, flags=re.I)
        parts = [p.strip(" ,.") for p in parts if p and p.strip(" ,.")]
        names = _names(text)
        fixed = []
        for p in parts:
            if names:
                p = re.sub(r"\b(him|her|them)\b", names[0], p, flags=re.I)
            if fixed and not _when(p) and _when(fixed[0]) and re.search(r"\b(it|the (call|meeting))\b", p, re.I):
                p = p + " (" + _when(fixed[0]) + ")"
            fixed.append(p)
        return {"steps": fixed[:6] or [text]}

    # ------------------------------------------------------------------ calendar
    def _t_calendar(self, prompt, system):
        t = _request(prompt)
        low = t.lower()
        out = {"action": "list", "title": None, "when": None, "duration_minutes": _duration(t), "attendees": [],
               "location": None, "target": None, "new_when": None}
        names = [n for n in _names(t) if n.lower() not in {"zoom", "meet"}]
        if re.search(r"\b(cancel|delete|remove|drop)\b", low):
            out["action"] = "cancel"
            m = re.search(r"(?:cancel|delete|remove|drop)\s+(?:my\s+|the\s+)?(.+?)(?:\s+(?:on|for)\s+.+)?$", t, re.I)
            out["target"] = m.group(1).strip() if m else None
            out["when"] = _when(t)
        elif re.search(r"\b(move|reschedule|push|postpone|shift|prepone)\b", low):
            out["action"] = "move"
            m = re.search(r"(?:move|reschedule|push|postpone|shift|prepone)\s+(?:my\s+|the\s+)?(.+?)\s+(?:to|till|until|by)\s+(.+)$", t, re.I)
            if m:
                out["target"], out["new_when"] = m.group(1).strip(), m.group(2).strip()
        elif re.search(r"\b(am i free|are you free|free at|available|busy at|any clash)\b", low):
            out["action"] = "check"
            out["when"] = _when(t)
        elif re.search(r"\b(find|need|any|look for)\b.*\b(free|slot|time|hour|hours)\b", low) and not re.search(r"\bblock\b", low):
            out["action"] = "find_slot"
            out["when"] = _when(t) or "tomorrow"
            out["duration_minutes"] = out["duration_minutes"] or (60 if "hour" in low else 30)
        elif re.search(r"\b(schedule|book|set up|setup|add|create|arrange|plan a|put|block)\b", low) and not re.search(r"\bwhat\b", low):
            out["action"] = "create"
            out["when"] = _when(t)
            out["attendees"] = names
            kind = "Meeting"
            m = re.search(r"\b(call|meeting|sync|lunch|dinner|coffee|review|standup|session|interview)\b", low)
            if m:
                kind = m.group(1).capitalize()
            mb = re.search(r"\bblock (?:some |an? )?([\w ]+? time|[\w ]+? session)\b", low)
            if mb:
                kind = mb.group(1).strip().capitalize()
            m2 = re.search(r"(?:schedule|book|add|create|put)\s+(?:a\s+|an\s+)?(.+?)\s+(?:with|on|at|for|tomorrow|today|next)\b", t, re.I)
            if names:
                out["title"] = f"{kind} with {' and '.join(names)}"
            elif m2 and len(m2.group(1)) < 40:
                out["title"] = m2.group(1).strip().capitalize()
            else:
                out["title"] = kind
            loc = re.search(r"\bat ([A-Z][\w .]+?)(?:\s+(?:on|at|tomorrow|today)\b|$)", t)
            if loc and not re.match(r"\d", loc.group(1)):
                out["location"] = loc.group(1).strip()
        else:
            out["action"] = "list"
            out["when"] = _when(t) or "today"
            m = re.search(r"when is my (.+?)\??$", t, re.I)
            if m:
                out["target"] = m.group(1)
        return out

    # ------------------------------------------------------------------ memory
    def _t_memory_query(self, prompt, system):
        t = _request(prompt).rstrip("?")
        low = t.lower()
        out = {"operation": "search", "entity": None, "relation": None, "node_type": None, "target": None, "text": t}
        m = re.search(r"who introduced me to (.+)", t, re.I)
        if m:
            return {**out, "operation": "related", "entity": m.group(1).strip(), "relation": "introduced_by", "text": None}
        m = re.search(r"(?:who is|who's|what do (?:i|you) know about|tell me about|about)\s+(.+)", t, re.I)
        if m:
            return {**out, "operation": "about", "entity": m.group(1).strip(), "text": None}
        for kind in ["task", "project", "person", "commitment", "preference", "place", "meeting"]:
            if re.search(rf"\b(my|list|all|open)\b.*\b{kind}s?\b|\b{kind}s\b", low):
                return {**out, "operation": "list", "node_type": kind, "text": None}
        if "people" in low or "contacts" in low:
            return {**out, "operation": "list", "node_type": "person", "text": None}
        if "owe" in low or "promised" in low:
            return {**out, "operation": "list", "node_type": "commitment", "text": None}
        return out

    def _t_memory_fact(self, prompt, system):
        t = re.sub(r"^(please\s+)?(remember|note|don't forget)( that)?\s+", "", _request(prompt), flags=re.I).strip()
        if re.search(r"\b(password|otp|pin|cvv|card number|account number|aadhaar|pan)\b", t, re.I):
            return {"entities": [{"name": "refused", "type": "preference"}], "relations": [], "attributes": []}
        names = _names(t)
        ents = [{"name": n, "type": "person"} for n in names]
        rels, attrs = [], []
        fam = r"((?i:sister|brother|mom|mother|dad|father|wife|husband|partner|friend|boss|manager|advisor|guide|professor|roommate|cousin|colleague|labmate))"
        m1 = re.search(rf"(?i:\bmy) {fam}(?i:'s name is|,| is called| is named|)\s+({NAME_RX})", t)
        m2 = None if m1 else re.search(rf"({NAME_RX}) (?i:is my) {fam}", t)
        m = m1 or m2
        if m:
            rel, who = (m.group(1), m.group(2)) if m1 else (m.group(2), m.group(1))
            who = re.sub(r"\s+(Studies|Works|Lives|Is)$", "", who)
            if who not in [e["name"] for e in ents]:
                ents.append({"name": who, "type": "person"})
            attrs.append({"entity": who, "key": "relationship", "value": rel.lower()})
        m = re.search(rf"({NAME_RX})\s+(studies|works|lives) (?:at|in|for) ([A-Z][\w .]+?)(?:\.|,|$)", t)
        if m:
            attrs.append({"entity": m.group(1), "key": f"{m.group(2)} at", "value": m.group(3).strip()})
        m = re.search(rf"({NAME_RX}) introduced me to ({NAME_RX})", t)
        if m:
            rels.append({"from": m.group(2), "relation": "introduced_by", "to": m.group(1)})
        m = re.search(rf"({NAME_RX})(?:'s| s) (email|phone|number|birthday|role) is (\S+(?: \S+)?)", t)
        if m:
            attrs.append({"entity": m.group(1), "key": "phone" if m.group(2) == "number" else m.group(2), "value": m.group(3).strip(".")})
        m = re.search(rf"({NAME_RX}) (?:works on|is working on) (?:the )?(.+?)(?:\.|$| and )", t)
        if m:
            ents.append({"name": m.group(2), "type": "project"})
            rels.append({"from": m.group(1), "relation": "works_on", "to": m.group(2)})
        m = re.search(r"i (?:prefer|like|love|hate|don't like|dislike) (.+)", t, re.I)
        if m:
            ents.append({"name": m.group(0)[2:].strip(), "type": "preference"})
            rels.append({"from": "me", "relation": "prefers", "to": m.group(0)[2:].strip()})
        m = re.search(rf"i (?:promised|owe) ({NAME_RX}) (?:the |a |an )?(.+?)(?: by (.+))?$", t, re.I)
        if m:
            c = f"{m.group(2)} for {m.group(1)}"
            ents.append({"name": c, "type": "commitment"})
            rels.append({"from": "me", "relation": "owes", "to": c})
            rels.append({"from": c, "relation": "relates_to", "to": m.group(1)})
            if m.group(3):
                attrs.append({"entity": c, "key": "due", "value": m.group(3)})
        if not ents:
            ents = [{"name": t[:80], "type": "preference"}]
        return {"entities": ents, "relations": rels, "attributes": attrs}

    # ------------------------------------------------------------------ communication
    def _t_message(self, prompt, system):
        t = _request(prompt)
        low = t.lower()
        out = {"action": "read_inbox", "to": [], "channel": None, "subject": None, "intent": None, "query": None, "tone": None}
        ch = re.search(r"#([\w-]+)", t) or re.search(r"\b(?:in|on|to) (?:the )?([\w-]+) (?:channel|slack)\b", low)
        is_slack = "slack" in low or bool(ch)
        if re.search(r"\b(reply|respond)\b", low):
            m = re.search(r"reply to ([\w. ]+?)(?:'s)? (?:mail|email|message)(?: saying| that| with)? ?(.*)$", t, re.I)
            out.update(action="reply_email", query=m.group(1) if m else None, intent=(m.group(2) if m else t) or t)
            out["to"] = [m.group(1)] if m else []
            return out
        if re.search(r"\b(send|write|draft|tell|message|ping|mail|email|post|dm|let)\b", low) and not re.search(r"\b(any|unread|check|read|show|summari[sz]e|what)\b.*\b(mails?|emails?|inbox|messages)\b", low):
            names = _names(t)
            if is_slack:
                out["action"] = "draft_slack"
                out["channel"] = ch.group(1) if ch else None
                out["to"] = names if not ch else []
            else:
                out["action"] = "draft_email"
                out["to"] = names[:3]
            m = re.search(r"\b(?:that|saying|to say|about|:)\s+(.+)$", t, re.I)
            out["intent"] = (m.group(1) if m else t).strip()
            m2 = re.search(r"\b(?:the|my)\s+([\w ]{3,30}?)(?:\s+link| for|$)", t, re.I)
            out["subject"] = (m.group(1)[:40].capitalize() if m else (m2.group(1).capitalize() if m2 else None))
            return out
        if is_slack:
            out.update(action="read_slack", channel=ch.group(1) if ch else None)
            return out
        m = re.search(r"\b(?:from|about|regarding)\s+(.+?)\??$", t, re.I)
        if m:
            out.update(action="search_email", query=m.group(1))
        else:
            out["query"] = "unread" if "unread" in low else "recent"
        return out

    def _t_draft(self, prompt, system):
        instr = re.search(r"^Instruction:\s*(.+)$", prompt, re.M)
        rcpt = re.search(r"^Recipient:\s*(.+)$", prompt, re.M)
        chan = re.search(r"^Channel:\s*(\w+)", prompt, re.M)
        subj = re.search(r"^Subject hint:\s*(.+)$", prompt, re.M)
        user = re.search(r"^Sender:\s*(.+)$", prompt, re.M)
        details = re.search(r"^Details:\s*(.+)$", prompt, re.M)
        text = (instr.group(1) if instr else "").strip()
        first_name = (rcpt.group(1).split()[0] if rcpt else "").strip(",#@")
        text = re.sub(rf"^(send|mail|email|e-mail|write|message|tell|ping|post)\s+(to\s+)?({re.escape(first_name)}\w*\s*)?", "", text, flags=re.I).strip() if first_name else text
        if re.match(r"^(the|my|our)\s+\w+", text, re.I) and not re.search(r"\b(is|are|was|will|has|have)\b", text, re.I):
            text = f"sharing {text}"
        text = re.sub(r"^(tell|let)\s+\w+(\s+know)?\s+(that\s+)?", "", text, flags=re.I)
        text = re.sub(r"^(that|saying)\s+", "", text, flags=re.I).strip()
        sentence = text[:1].upper() + text[1:]
        if sentence and sentence[-1] not in ".!?":
            sentence += "."
        sentence = re.sub(r"\b(he|she) (is|was|has)\b", "you \\2", sentence)
        if details:
            sentence += f" For reference: {details.group(1)}."
        first = (rcpt.group(1).split()[0] if rcpt else "").strip(",")
        if chan and chan.group(1).lower() == "email":
            body = f"Hi {first},\n\n{sentence}\n\nBest,\n{user.group(1) if user else 'Yogesh'}"
            subject = subj.group(1) if subj and subj.group(1) != "None" else (text[:40].capitalize() or "Quick note")
            return {"subject": subject, "body": body}
        return {"subject": None, "body": sentence}

    # ------------------------------------------------------------------ follow-up rewriting (offline)
    def _t_rewrite(self, prompt, system):
        latest = re.search(r"^Latest:\s*(.+)$", prompt, re.M)
        prev = re.search(r"^Previous request:\s*(.*)$", prompt, re.M)
        person = re.search(r"^Person mentioned last:\s*(.+)$", prompt, re.M)
        t = (latest.group(1) if latest else "").strip().rstrip("?")
        p = (prev.group(1) if prev else "").strip().rstrip("?")
        m = re.match(r"^(?:and|what about|how about|also|ok(?:ay)? and|but)\s+(.+)$", t, re.I)
        if m and p:
            rest = m.group(1)
            day = re.compile(r"\b(today|tonight|tomorrow|day after tomorrow|(?:next |this )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next week)\b", re.I)
            if day.search(rest) and day.search(p):
                return {"request": day.sub(day.search(rest).group(0), p, count=1)}
            names = _names(rest)
            pnames = _names(p)
            if names and pnames:
                return {"request": p.replace(pnames[0], names[0], 1)}
            return {"request": f"{p} {rest}"}
        if person:
            t = re.sub(r"\b(him|her|them)\b", person.group(1).strip(), t, flags=re.I)
        return {"request": t}

    # ------------------------------------------------------------------ inbox triage (offline heuristics)
    def _t_triage(self, prompt, system):
        low = prompt.lower()
        auto = re.search(r"no-?reply|notification|newsletter|unsubscribe|order #|receipt|has shipped|digest", low)
        upset = re.search(r"unacceptable|disappoint|frustrat|annoy|angry|still (no|not|haven)|asked .{0,20}times|"
                          r"not happy|ridiculous|complain|why haven", low)
        urgent = re.search(r"urgent|asap|today|by tomorrow|deadline|immediately", low)
        asks = "?" in prompt or re.search(r"\b(can you|could you|please (send|share|confirm|let me know)|let me know)\b", low)
        subj = re.search(r"^Subject:\s*(.+)$", prompt, re.M)
        return {"category": "notification" if auto else "work", "needs_reply": bool((asks or upset) and not auto),
                "tone": "upset" if upset else "urgent" if urgent else "neutral",
                "summary": (subj.group(1) if subj else "Email")[:80]}

    def _t_draft_reply(self, prompt, system):
        sender = re.search(r"^Sender:\s*(.+)$", prompt, re.M)
        first = (sender.group(1).split()[0] if sender else "there").strip(",")
        user = re.search(r"on behalf of (\w+)", system or "")
        me = user.group(1) if user else "Yogesh"
        return {"subject": None, "body": f"Hi {first},\n\nThanks for your email. I'll get back to you on this shortly.\n\nBest,\n{me}"}

    # ------------------------------------------------------------------ device
    def _t_device(self, prompt, system):
        t = _request(prompt)
        low = t.lower()
        out = {"action": "battery", "time": None, "minutes": None, "label": None, "app": None, "contact": None,
               "message": None, "setting": None, "state": None}
        if re.search(r"\btimer\b", low):
            m = re.search(r"(\d+)\s*(min|minute|hour|hr|sec)", low)
            mins = int(m.group(1)) * (60 if m and m.group(2).startswith("h") else 1) if m else 5
            lab = re.search(r"\bfor (?:the )?([a-z ]+)$", low)
            return {**out, "action": "set_timer", "minutes": mins, "label": lab.group(1).strip().capitalize() if lab and not lab.group(1).strip().isdigit() and "minute" not in lab.group(1) else "Timer"}
        if re.search(r"\bremind me\b", low):
            m = re.search(r"remind me (?:to |about )?(.+?)\s+(?:at|on|in|tomorrow|today|tonight|by|next)\b", t, re.I) or re.search(r"remind me (?:to |about )?(.+)$", t, re.I)
            return {**out, "action": "set_reminder", "time": _when(t) or "in 1 hour", "label": (m.group(1) if m else "Reminder").strip().capitalize()}
        if re.search(r"\b(alarm|wake me)\b", low):
            return {**out, "action": "set_alarm", "time": _when(t), "label": "Wake up" if "wake" in low else "Alarm"}
        for setting, rx in [("wifi", r"wi-?fi"), ("bluetooth", r"bluetooth"), ("flashlight", r"flash ?light|torch"),
                            ("dnd", r"do not disturb|\bdnd\b"), ("silent", r"silent")]:
            if re.search(rx, low):
                state = not re.search(r"\b(off|disable|stop|deactivate)\b", low)
                return {**out, "action": "toggle", "setting": setting, "state": state}
        if re.search(r"\b(text|sms)\b", low):
            m = re.search(rf"(?:text|sms|message)\s+({NAME_RX})\s*(?:that |saying |:)?\s*(.*)$", t, re.I)
            return {**out, "action": "send_sms", "contact": m.group(1) if m else (_names(t) or [None])[0], "message": (m.group(2) if m else "").strip() or None}
        if re.search(r"\b(call|dial|ring)\b", low):
            m = re.search(rf"(?:call|dial|ring)\s+({NAME_RX}|\+?[\d ]{{7,}})", t, re.I)
            return {**out, "action": "call", "contact": m.group(1).strip() if m else None}
        if re.search(r"\b(open|launch|start)\b", low):
            m = re.search(r"(?:open|launch|start)\s+(?:the\s+)?([\w ]+?)(?:\s+app)?$", t, re.I)
            return {**out, "action": "open_app", "app": m.group(1).strip() if m else None}
        if re.search(r"\bnotification", low):
            return {**out, "action": "read_notifications"}
        if re.search(r"\b(where am i|location)\b", low):
            return {**out, "action": "get_location"}
        if re.search(r"\b(number|contact)\b", low):
            names = _names(t)
            return {**out, "action": "find_contact", "contact": names[0] if names else None}
        return out

    # ------------------------------------------------------------------ github / places
    def _t_github(self, prompt, system):
        t = _request(prompt)
        low = t.lower()
        repo = re.search(r"\b(?:on|in|for)\s+(?:my\s+)?([\w./-]+)\s+(?:repo|repository)\b", t, re.I) or re.search(r"\b(?:on|in) ([A-Z][\w-]+)\b", t)
        out = {"action": "repo_summary", "repo": repo.group(1) if repo else None, "title": None, "body": None, "number": None}
        if re.search(r"\b(open|create|file|raise|new)\b.*\bissue\b", low):
            m = re.search(r":\s*(.+)$", t) or re.search(r"\b(?:about|for|titled)\s+(.+)$", t, re.I)
            return {**out, "action": "create_issue", "title": (m.group(1).strip() if m else "New issue")[:120]}
        if "notification" in low:
            return {**out, "action": "notifications"}
        if re.search(r"\b(pr|prs|pull requests?)\b", low):
            return {**out, "action": "list_prs"}
        if "issue" in low:
            return {**out, "action": "list_issues"}
        if re.search(r"\b(ci|build|tests?|workflow|pipeline|green|pass)\b", low):
            return {**out, "action": "ci_status"}
        return out

    def _t_places(self, prompt, system):
        t = _request(prompt).rstrip("?")
        low = t.lower()
        mode = next((m for k, m in [("walk", "walking"), ("bike", "cycling"), ("cycle", "cycling"), ("bus", "transit"),
                                    ("metro", "transit"), ("train", "transit"), ("drive", "driving"), ("car", "driving")] if k in low), None)
        out = {"action": "find_place", "place": None, "origin": None, "destination": None, "people": [], "mode": mode, "event": None}
        if re.search(r"\b(when should i leave|leave for)\b", low):
            m = re.search(r"(?:leave for|leave to)\s+(?:my\s+|the\s+)?(.+)$", t, re.I)
            return {**out, "action": "when_to_leave", "event": m.group(1).strip() if m else None}
        if re.search(r"\b(meet|halfway|meeting point)\b", low):
            locs = re.findall(r"\b(?:in|at|from)\s+([A-Z][\w.]+(?:\s+[A-Z][\w.]+)?)", t)
            return {**out, "action": "meeting_point", "people": locs}
        m = re.search(r"(?:from)\s+(.+?)\s+to\s+(.+?)(?:\s+by\s+\w+)?$", t, re.I)
        if m:
            return {**out, "action": "travel_time", "origin": m.group(1).strip(), "destination": m.group(2).strip()}
        m = re.search(r"(?:to reach|to get to|to go to|reach|get to)\s+(.+?)(?:\s+from\s+(.+?))?(?:\s+by\s+\w+)?$", t, re.I)
        if m and re.search(r"how (long|far)", low):
            return {**out, "action": "travel_time", "destination": m.group(1).strip(), "origin": m.group(2)}
        m = re.search(r"(?:where is|find|locate)\s+(?:a\s+|the\s+)?(.+)$", t, re.I)
        return {**out, "place": m.group(1).strip() if m else t}

    # ------------------------------------------------------------------ planner / chat
    def _t_plan(self, prompt, system):
        goal = _request(prompt)
        steps = [{"request": f"what's on my calendar {(_when(goal) or 'this week')}", "agent": "scheduler", "urgency": 3, "importance": 4},
                 {"request": f"what do I know about {re.sub(r'^(plan|prepare for|help me (plan|prepare for))\s+(my\s+)?', '', goal, flags=re.I)}", "agent": "memory", "urgency": 2, "importance": 3},
                 {"request": "find me an hour free tomorrow", "agent": "scheduler", "urgency": 3, "importance": 4}]
        return {"steps": steps}

    def _t_chat(self, prompt, system):
        res = re.search(r"^SYSTEM RESULT:\s*(.+?)(?:\n[A-Z ]+:|\Z)", prompt, re.M | re.S)
        if res and res.group(1).strip() not in ("", "none"):
            return res.group(1).strip()
        t = _request(prompt).lower()
        if re.search(r"\b(hi|hello|hey|good (morning|evening|afternoon))\b", t):
            return "Hello. What can I do for you?"
        if re.search(r"\b(thanks|thank you)\b", t):
            return "Anytime."
        if "who are you" in t or "your name" in t:
            return "I'm JARVIS, your personal assistant. I handle your calendar, mail, Slack, GitHub, reminders and the phone, with your permission."
        if "what can you do" in t:
            return "I can manage your calendar, read and draft mail and Slack messages, track people and commitments, set alarms and reminders, check GitHub, and work out travel times. Anything I send needs your approval first."
        if "how are you" in t:
            return "Running smoothly. Thanks for asking."
        if getattr(self, "_cloud_errors", None):
            why = "rate limits" if all("rate limited" in e or "cooling" in e for e in self._cloud_errors) else "an error on the server"
            return (f"I can't reach my language models right now ({why}), so I can only handle direct commands "
                    "like scheduling, reminders, mail drafts and phone actions. The server log has details.")
        return "I'm in offline mode right now, so I can only handle direct commands like scheduling, reminders, mail drafts and phone actions."
