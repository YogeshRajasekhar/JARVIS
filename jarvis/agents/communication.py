"""
Communication agent: email + Slack. Reads and summarises freely (with permission);
sends NOTHING without an approval card showing the exact recipients and text.

Prompt-injection firebreak: email/Slack bodies are untrusted. They are summarised with
deterministic templates (never fed to a model that can call tools), suspicious messages are
flagged, and any action proposed while handling them is forced through approval.
"""

from __future__ import annotations

import re

from jarvis.agents.base import AgentReply, BaseAgent, TurnContext
from jarvis.core.redaction import injection_score, redact
from jarvis.llm.schemas import DraftOut, MessageOut
from jarvis.llm.structured import StructuredOutputError, complete_json
from jarvis.prompts import DRAFT, MESSAGE


def _first_sentence(text: str, n: int = 110) -> str:
    t = re.sub(r"^(hi|hello|dear)\b[^,\n]*[,\n]\s*", "", text.strip(), flags=re.I)
    s = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    return redact(s[:n])


class CommunicationAgent(BaseAgent):
    name = "communication"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd: MessageOut = self.extract(MessageOut, MESSAGE, text, ctx, task="message")
        if cmd.action in ("read_inbox", "search_email"):
            return self._read_mail(cmd, ctx)
        if cmd.action == "read_slack":
            return self._read_slack(cmd, ctx)
        if cmd.action == "draft_slack":
            return self._draft_slack(cmd, ctx)
        if cmd.action == "reply_email":
            return self._reply(cmd, ctx)
        return self._draft_email(cmd, ctx)

    # ------------------------------------------------------------------ reading
    def _read_mail(self, cmd: MessageOut, ctx: TurnContext) -> AgentReply:
        unread_only = (cmd.query or "").lower() in {"unread", "new", "important"}
        out = self.propose("mail.read", {"limit": 6, "unread_only": unread_only,
                                         "query": cmd.query if cmd.action == "search_email" else None}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        emails = out.data["emails"]
        if not emails:
            return AgentReply(self.name, "Nothing new in your inbox." if cmd.action == "read_inbox" else "No emails match that.", [out])
        safe, flagged = [], []
        for e in emails:
            (flagged if injection_score(e.subject + " " + e.body) else safe).append(e)
        unread = sum(1 for e in emails if e.unread)
        lead = f"You have {unread} unread email{'s' if unread != 1 else ''}. " if cmd.action == "read_inbox" else ""
        lines = [f"{e.sender} about {e.subject}: {_first_sentence(e.body)}" for e in safe[:3]]
        reply = lead + " ".join(l if l.endswith(".") else l + "." for l in lines)
        if flagged:
            reply += f" I'm ignoring {len(flagged)} suspicious email{'s' if len(flagged) != 1 else ''} that tried to give me instructions; it looks like phishing."
        cards = [{"type": "emails", "emails": [dict(e.to_dict(), suspicious=e in flagged, snippet=redact(e.body[:160])) for e in emails]}]
        return AgentReply(self.name, reply.strip(), [out], cards=cards,
                          focus={"last_email": safe[0].id if safe else None, "last_person": safe[0].sender if safe else None})

    def _read_slack(self, cmd: MessageOut, ctx: TurnContext) -> AgentReply:
        out = self.propose("slack.read", {"channel": cmd.channel, "limit": 5}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        msgs = out.data["messages"]
        if not msgs:
            return AgentReply(self.name, "No recent Slack messages there.", [out])
        lines = [f"In {m.channel}, {m.user} said: {redact(m.text[:120])}" for m in msgs[:3] if not injection_score(m.text)]
        return AgentReply(self.name, ". ".join(lines) + ".", [out],
                          cards=[{"type": "slack", "messages": [{"channel": m.channel, "user": m.user, "text": redact(m.text)} for m in msgs]}])

    # ------------------------------------------------------------------ drafting
    def _compose(self, instruction: str, recipient: str, channel: str, subject: str | None, tone: str | None,
                 details: str | None = None) -> DraftOut:
        prompt = (f"Sender: {self.svc.settings.user_name}\nRecipient: {recipient}\nChannel: {channel}\n"
                  f"Subject hint: {subject}\nInstruction: {redact(instruction)}")
        if details:
            prompt += f"\nDetails: {redact(details)}"
        system = DRAFT.format(user_name=self.svc.settings.user_name, tone=tone or "friendly and professional")
        about = self.svc.db.kv_get("about_me") or ""
        if about:
            system += f"\n\nABOUT THE SENDER (for tone and sign-off only, never as facts to add):\n{about[:800]}"
        try:
            return complete_json(self.svc.smart_llm, DraftOut, system, prompt, task="draft", max_tokens=500)
        except StructuredOutputError:
            return DraftOut(subject=subject or "Quick note", body=instruction)

    def _recipients(self, names: list[str], ctx: TurnContext, need: str = "email"):
        people, missing = [], []
        for n in names:
            c, perm = self.svc.lookup_contact(n, self.name, resume=ctx.original or None)
            if perm is not None:
                return None, None, perm
            if c and c.get(need):
                people.append(c)
            else:
                missing.append(n)
        return people, missing, None

    def _draft_email(self, cmd: MessageOut, ctx: TurnContext) -> AgentReply:
        names = cmd.to or ([ctx.focus["last_person"]] if ctx.focus.get("last_person") else [])
        if not names:
            return AgentReply(self.name, "Who should I send it to?", clarify="recipient")
        people, missing, perm = self._recipients(names, ctx)
        if perm is not None:
            return AgentReply(self.name, self.say_outcome(perm), [perm])
        if missing:
            return AgentReply(self.name, f"I don't have an email address for {missing[0]}. What is it?", clarify="email")
        if not cmd.intent:
            return AgentReply(self.name, f"What should the email to {people[0]['name']} say?", clarify="content")
        details = "; ".join(ctx.step_results) or None
        d = self._compose(cmd.intent, ", ".join(p["name"] for p in people), "email", cmd.subject, cmd.tone, details)
        out = self.propose("mail.send", {"to": [p["email"] for p in people], "to_names": [p["name"] for p in people],
                                         "subject": d.subject or cmd.subject or "Quick note", "body": d.body}, ctx)
        return AgentReply(self.name, self.say_outcome(out, out.message), [out], focus={"last_person": people[0]["name"]})

    def _reply(self, cmd: MessageOut, ctx: TurnContext) -> AgentReply:
        out = self.propose("mail.read", {"limit": 10, "query": cmd.query or (cmd.to[0] if cmd.to else None)}, ctx)
        if (b := self.blocked_reply(out)):
            return b
        emails = [e for e in out.data["emails"] if not injection_score(e.subject + e.body)]
        target = emails[0] if emails else None
        if not target and ctx.focus.get("last_email"):
            target = self.svc.mail.get(ctx.focus["last_email"])
        if not target:
            return AgentReply(self.name, "I couldn't find that email.", [out])
        d = self._compose(cmd.intent or "acknowledge the email", target.sender, "email",
                          f"Re: {target.subject}", cmd.tone)
        subj = target.subject if target.subject.lower().startswith("re:") else f"Re: {target.subject}"
        res = self.propose("mail.send", {"to": [target.sender_email], "to_names": [target.sender], "subject": subj, "body": d.body,
                                         "reply_to_id": target.id}, ctx)
        return AgentReply(self.name, self.say_outcome(res), [out, res], focus={"last_person": target.sender})

    def _draft_slack(self, cmd: MessageOut, ctx: TurnContext) -> AgentReply:
        if cmd.channel:
            dest = f"#{cmd.channel.lstrip('#')}"
        elif cmd.to:
            dest = "@" + cmd.to[0]
        else:
            return AgentReply(self.name, "Which channel or person on Slack?", clarify="channel")
        if not cmd.intent:
            return AgentReply(self.name, f"What should I post to {dest}?", clarify="content")
        d = self._compose(cmd.intent, dest, "slack", None, cmd.tone or "brief")
        out = self.propose("slack.send", {"to": dest, "text": d.body}, ctx)
        return AgentReply(self.name, self.say_outcome(out), [out])
