"""
Permission broker: Android-style runtime permissions for every tool JARVIS can use.

Each tool declares the scopes it needs. Before a tool runs, the broker checks the grants:
  granted       -> continue
  needs_prompt  -> the phone shows "Allow JARVIS to <x>?  [Only this time] [Always] [Don't allow]"
  denied        -> the user said no; JARVIS explains and does not nag again until asked
  blocked       -> hard deny-list. Not grantable by anyone, including the user via voice.

The deny-list exists because some data is too dangerous to put within reach of an LLM that
reads untrusted email: one successful prompt injection must not be able to read an OTP,
open a payment app, or read a password manager.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from jarvis.core.storage import Database


@dataclass(frozen=True)
class Scope:
    name: str
    title: str          # dialog text: "Allow JARVIS to {title}?"
    why: str            # one line shown under the title
    risk: str           # low | medium | high


SCOPES: dict[str, Scope] = {s.name: s for s in [
    Scope("calendar.read", "see your calendar", "To tell you what's coming up and find free time.", "low"),
    Scope("calendar.write", "add and change calendar events", "To book, move and cancel meetings you ask for.", "medium"),
    Scope("mail.read", "read your email", "To summarise your inbox and find messages.", "medium"),
    Scope("mail.send", "send email as you", "Every email is still shown to you for approval first.", "high"),
    Scope("slack.read", "read your Slack messages", "To catch you up on channels and DMs.", "medium"),
    Scope("slack.send", "post Slack messages as you", "Every message is still shown to you for approval first.", "high"),
    Scope("github.read", "read your GitHub repos and notifications", "To report PRs, issues and CI status.", "low"),
    Scope("github.write", "create issues and comments on GitHub", "Each one is shown to you for approval first.", "medium"),
    Scope("contacts.read", "access your contacts", "To find people's numbers and emails.", "medium"),
    Scope("location", "access this device's location", "For travel times and meeting points.", "medium"),
    Scope("notifications.read", "read your notifications", "To brief you on what you missed. OTP and banking notifications are always hidden.", "high"),
    Scope("alarms.write", "set alarms, timers and reminders", "Uses the phone's clock app.", "low"),
    Scope("apps.launch", "open apps", "Only opens the app; JARVIS never operates inside it.", "low"),
    Scope("settings.toggle", "change Wi-Fi, Bluetooth, flashlight and Do Not Disturb", "Quick settings only.", "medium"),
    Scope("phone.call", "start phone calls", "The dialer opens; you confirm the call.", "medium"),
    Scope("sms.send", "send SMS as you", "Every SMS is shown to you for approval first.", "high"),
]}

# Never grantable. Requests for these are refused and audited.
BLOCKED_SCOPES = {
    "sms.read": "SMS inbox holds OTPs and bank alerts",
    "payments": "payments and UPI are never automated",
    "passwords": "passwords and password managers are never accessed",
    "banking": "banking data is never accessed",
    "accessibility.full": "full screen control is disabled for safety",
    "storage.all": "full file access is not needed",
}

# Apps JARVIS will not open or read notifications from. Matched case-insensitively.
BLOCKED_APP_PATTERNS = [
    r"\bg ?pay\b", r"google pay", r"phone ?pe", r"paytm", r"bhim", r"cred\b", r"amazon pay",
    r"\bbank\b", r"\bsbi\b", r"hdfc", r"icici", r"axis", r"kotak", r"yono", r"net ?banking",
    r"zerodha", r"kite", r"groww", r"upstox", r"wallet", r"crypto", r"binance", r"coinbase",
    r"1password", r"lastpass", r"bitwarden", r"keepass", r"authenticator", r"password",
    r"digilocker", r"aadhaar", r"m-?aadhaar",
]
_BLOCKED_APP_RE = re.compile("|".join(BLOCKED_APP_PATTERNS), re.I)


def is_blocked_app(name: Optional[str]) -> bool:
    return bool(name and _BLOCKED_APP_RE.search(name))


class PermStatus(Enum):
    GRANTED = "granted"
    NEEDS_PROMPT = "needs_prompt"
    DENIED = "denied"
    BLOCKED = "blocked"


@dataclass
class PermCheck:
    status: PermStatus
    scope: Optional[str] = None
    reason: str = ""


class PermissionBroker:
    def __init__(self, db: Database):
        self.db = db

    def check(self, scopes: Iterable[str]) -> PermCheck:
        for s in scopes:
            if s in BLOCKED_SCOPES:
                return PermCheck(PermStatus.BLOCKED, s, BLOCKED_SCOPES[s])
            if s not in SCOPES:
                return PermCheck(PermStatus.BLOCKED, s, f"unknown scope {s!r}")
            mode = self.db.get_grant(s)
            if mode == "deny":
                return PermCheck(PermStatus.DENIED, s, "you denied this permission")
            if mode not in ("always", "once"):
                return PermCheck(PermStatus.NEEDS_PROMPT, s, SCOPES[s].why)
        return PermCheck(PermStatus.GRANTED)

    def grant(self, scope: str, mode: str) -> None:
        if scope in BLOCKED_SCOPES or scope not in SCOPES:
            raise PermissionError(f"Scope {scope!r} cannot be granted")
        if mode not in ("always", "once", "deny"):
            raise ValueError(mode)
        self.db.set_grant(scope, mode)
        self.db.audit("user", "permission.grant", {"scope": scope}, mode)

    def consume(self, scopes: Iterable[str]) -> None:
        """'Only this time' grants are used up after one successful execution."""
        for s in scopes:
            if self.db.get_grant(s) == "once":
                self.db.remove_grant(s)

    def revoke(self, scope: str) -> None:
        self.db.remove_grant(scope)
        self.db.audit("user", "permission.revoke", {"scope": scope}, "revoked")

    def revoke_all(self) -> None:
        self.db.clear_grants()
        self.db.audit("user", "permission.revoke_all", {}, "lockdown")

    def describe(self) -> list[dict]:
        grants = self.db.all_grants()
        return [{"scope": s.name, "title": s.title, "why": s.why, "risk": s.risk,
                 "mode": grants.get(s.name, "not_asked")} for s in SCOPES.values()]
