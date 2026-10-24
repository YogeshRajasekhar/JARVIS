"""
Google OAuth for Calendar + Gmail, with least-privilege scopes and encrypted token storage.

Scopes requested (and why only these):
  calendar.events   read/write events, not calendar settings or sharing
  gmail.readonly    read mail for summaries
  gmail.send        send mail you approved (cannot delete or modify existing mail)

One-time setup: `python scripts/google_auth.py` opens the consent page and stores the
refresh token encrypted in the JARVIS database. Revoke any time at
https://myaccount.google.com/permissions.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import httpx

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SECRET_NAME = "google_oauth"


class GoogleAuth:
    def __init__(self, db, client_id: str, client_secret: str):
        self.db = db
        self.client_id = client_id
        self.client_secret = client_secret
        self._access: Optional[str] = None
        self._expires = 0.0

    @classmethod
    def from_credentials_file(cls, db, path: str) -> "GoogleAuth":
        data = json.load(open(path))
        c = data.get("installed") or data.get("web") or data
        return cls(db, c["client_id"], c["client_secret"])

    def is_connected(self) -> bool:
        return self.db.secret_get(SECRET_NAME) is not None

    def store_tokens(self, tokens: dict) -> None:
        self.db.secret_set(SECRET_NAME, json.dumps({"refresh_token": tokens["refresh_token"]}))

    def _refresh(self) -> None:
        raw = self.db.secret_get(SECRET_NAME)
        if not raw:
            raise RuntimeError("Google is not connected. Run scripts/google_auth.py once.")
        rt = json.loads(raw)["refresh_token"]
        r = httpx.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        self._access = j["access_token"]
        self._expires = time.time() + int(j.get("expires_in", 3600)) - 60

    def session(self) -> httpx.Client:
        if not self._access or time.time() > self._expires:
            self._refresh()
        return httpx.Client(headers={"Authorization": f"Bearer {self._access}"}, timeout=20)

    def revoke(self) -> None:
        raw = self.db.secret_get(SECRET_NAME)
        if raw:
            try:
                httpx.post("https://oauth2.googleapis.com/revoke", params={"token": json.loads(raw)["refresh_token"]}, timeout=10)
            finally:
                self.db.execute("DELETE FROM secrets WHERE name=?", (SECRET_NAME,))
