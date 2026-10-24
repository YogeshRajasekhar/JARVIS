"""Email providers: interface, mock (virtual phone / tests), Gmail REST (real)."""

from __future__ import annotations

import base64
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional


@dataclass
class Email:
    id: str
    sender: str
    sender_email: str
    subject: str
    body: str
    date: datetime
    to: list[str] = field(default_factory=list)
    unread: bool = True
    thread_id: Optional[str] = None
    message_id_header: Optional[str] = None

    def summary_line(self) -> str:
        return f"{self.sender}: {self.subject}"

    def to_dict(self) -> dict:
        return {"id": self.id, "from": self.sender, "subject": self.subject, "date": self.date.isoformat(),
                "unread": self.unread, "snippet": self.body[:160]}


class MailProvider(ABC):
    @abstractmethod
    def list_recent(self, limit: int = 10, unread_only: bool = False, query: Optional[str] = None) -> list[Email]: ...

    @abstractmethod
    def send(self, to: list[str], subject: str, body: str, reply_to: Optional[Email] = None) -> str: ...

    @abstractmethod
    def get(self, email_id: str) -> Optional[Email]: ...


def _matches(e: Email, query: Optional[str]) -> bool:
    if not query or query.lower() in {"today", "recent", "latest", "new", "unread", "important"}:
        return True
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    hay = f"{e.sender} {e.sender_email} {e.subject} {e.body}".lower()
    return any(t in hay for t in terms)


class MockMail(MailProvider):
    def __init__(self):
        self.inbox: list[Email] = []
        self.sent: list[dict] = []

    def add(self, sender: str, sender_email: str, subject: str, body: str, date: datetime, unread=True) -> Email:
        e = Email(uuid.uuid4().hex[:8], sender, sender_email, subject, body, date, unread=unread)
        self.inbox.append(e)
        return e

    def list_recent(self, limit=10, unread_only=False, query=None):
        items = [e for e in self.inbox if (e.unread or not unread_only) and _matches(e, query)]
        return sorted(items, key=lambda e: e.date, reverse=True)[:limit]

    def get(self, email_id):
        return next((e for e in self.inbox if e.id == email_id), None)

    def send(self, to, subject, body, reply_to=None):
        mid = uuid.uuid4().hex[:8]
        self.sent.append({"id": mid, "to": to, "subject": subject, "body": body,
                          "in_reply_to": reply_to.id if reply_to else None})
        return mid


class GmailProvider(MailProvider):
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, auth):
        self.auth = auth

    def list_recent(self, limit=10, unread_only=False, query=None):
        q = []
        if unread_only:
            q.append("is:unread")
        if query and query.lower() not in {"today", "recent", "latest", "new", "unread", "important"}:
            q.append(query)
        q.append("-category:promotions -category:social")
        s = self.auth.session()
        r = s.get(f"{self.BASE}/messages", params={"maxResults": limit, "q": " ".join(q)})
        r.raise_for_status()
        out = []
        for m in r.json().get("messages", [])[:limit]:
            d = s.get(f"{self.BASE}/messages/{m['id']}", params={"format": "full"}).json()
            out.append(self._parse(d))
        return out

    def get(self, email_id):
        d = self.auth.session().get(f"{self.BASE}/messages/{email_id}", params={"format": "full"})
        return self._parse(d.json()) if d.status_code == 200 else None

    @staticmethod
    def _body(payload: dict) -> str:
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="ignore")
        for p in payload.get("parts", []) or []:
            t = GmailProvider._body(p)
            if t:
                return t
        return ""

    def _parse(self, d: dict) -> Email:
        h = {x["name"].lower(): x["value"] for x in d["payload"].get("headers", [])}
        name, addr = parseaddr(h.get("from", ""))
        try:
            date = parsedate_to_datetime(h.get("date"))
        except Exception:
            date = datetime.now(timezone.utc)
        return Email(d["id"], name or addr, addr, h.get("subject", "(no subject)"),
                     self._body(d["payload"])[:4000] or d.get("snippet", ""), date,
                     unread="UNREAD" in d.get("labelIds", []), thread_id=d.get("threadId"),
                     message_id_header=h.get("message-id"))

    def send(self, to, subject, body, reply_to=None):
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if reply_to and reply_to.message_id_header:
            msg["In-Reply-To"] = reply_to.message_id_header
            msg["References"] = reply_to.message_id_header
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_json = {"raw": raw}
        if reply_to and reply_to.thread_id:
            body_json["threadId"] = reply_to.thread_id
        r = self.auth.session().post(f"{self.BASE}/messages/send", json=body_json)
        r.raise_for_status()
        return r.json()["id"]
