"""
Google OAuth for Calendar + Gmail, with least-privilege scopes and encrypted token storage.

Scopes requested (and why only these):
  calendar.events   read/write events, not calendar settings or sharing
  gmail.readonly    read mail for summaries
  gmail.send        send mail you approved (cannot delete or modify existing mail)

  openid email      only to show which Google account is connected

Connect from the app (Settings > Accounts > Connect Google): Google's own sign-in page opens in the
phone's browser, and the refresh token is stored encrypted in the JARVIS database. The older
`python scripts/google_auth.py` route still works. Revoke any time at https://myaccount.google.com/permissions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from typing import Optional

import httpx

SCOPES = [
    "openid", "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SECRET_NAME = "google_oauth"
CLIENT_SECRET_NAME = "google_client"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuthError(RuntimeError):
    pass


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

    @classmethod
    def from_db(cls, db) -> Optional["GoogleAuth"]:
        raw = db.secret_get(CLIENT_SECRET_NAME)
        if not raw:
            return None
        c = json.loads(raw)
        return cls(db, c["client_id"], c["client_secret"])

    def is_connected(self) -> bool:
        return self.db.secret_get(SECRET_NAME) is not None

    def account_email(self) -> Optional[str]:
        raw = self.db.secret_get(SECRET_NAME)
        return json.loads(raw).get("email") if raw else None

    def store_tokens(self, tokens: dict, email: Optional[str] = None) -> None:
        self.db.secret_set(SECRET_NAME, json.dumps({"refresh_token": tokens["refresh_token"], "email": email}))
        if tokens.get("access_token"):
            self._access = tokens["access_token"]
            self._expires = time.time() + int(tokens.get("expires_in", 3600)) - 60

    # ------------------------------------------------------------------ in-app sign-in (authorization code + PKCE)
    @staticmethod
    def new_flow() -> dict:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return {"state": secrets.token_urlsafe(32), "verifier": verifier, "challenge": challenge}

    def auth_url(self, redirect_uri: str, state: str, challenge: str, login_hint: Optional[str] = None) -> str:
        q = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
             "include_granted_scopes": "true", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        if login_hint:
            q["login_hint"] = login_hint
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> str:
        """Code -> tokens, stored encrypted. Returns the Google account's email."""
        r = httpx.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret, "code": code,
                                        "code_verifier": verifier, "redirect_uri": redirect_uri,
                                        "grant_type": "authorization_code"}, timeout=20)
        if r.status_code != 200:
            raise GoogleAuthError(_google_error(r))
        tok = r.json()
        if not tok.get("refresh_token"):
            raise GoogleAuthError("Google did not return a long-lived token. Remove JARVIS at "
                                  "myaccount.google.com/permissions and connect again.")
        granted = set((tok.get("scope") or "").split())
        missing = [s.rsplit("/", 1)[-1] for s in SCOPES[2:] if granted and s not in granted]
        email = None
        try:
            u = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=15)
            email = u.json().get("email") if u.status_code == 200 else None
        except httpx.HTTPError:
            pass
        self.store_tokens(tok, email)
        if missing:
            raise GoogleAuthError(f"Connected, but these permissions were left unticked: {', '.join(missing)}. "
                                  "Connect again and tick every box so JARVIS can read and send mail and manage events.")
        return email or "your Google account"

    def _refresh(self) -> None:
        raw = self.db.secret_get(SECRET_NAME)
        if not raw:
            raise GoogleAuthError("Google is not connected. Open Settings, Accounts, and tap Connect Google.")
        rt = json.loads(raw)["refresh_token"]
        r = httpx.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=20)
        if r.status_code != 200:
            raise GoogleAuthError("Google sign-in expired or was revoked (" + _google_error(r) + "). "
                                  "Open Settings, Accounts, and connect Google again.")
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


def _google_error(r) -> str:
    try:
        j = r.json()
        return j.get("error_description") or j.get("error") or f"HTTP {r.status_code}"
    except Exception:
        return f"HTTP {r.status_code}"
