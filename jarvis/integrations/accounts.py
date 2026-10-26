"""
Connected accounts: Google (Gmail + Calendar), Slack and GitHub, managed from the app.

Google uses Google's own sign-in page (authorization code + PKCE). The app opens the page in the
phone's browser; Google sends the browser back to https://<server>/oauth/google/callback with a
one-time code; the server swaps it for a refresh token, stores it encrypted and switches the mail and
calendar tools over at once. No password ever touches JARVIS.

Slack and GitHub use tokens pasted once in Settings. Each token is checked with a real request
before it is stored (encrypted) and used.

Everything is swapped live: tools read svc.mail / svc.calendar / svc.slack / svc.github at call time.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

import httpx

from jarvis.integrations.google_auth import CLIENT_SECRET_NAME, SECRET_NAME, GoogleAuth, GoogleAuthError

log = logging.getLogger("jarvis.accounts")

FLOW_TTL = 600          # a sign-in link is valid for 10 minutes
SLACK_SECRET = "slack_token"
GITHUB_SECRET = "github_token"
CLIENT_ID_RX = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")


class AccountError(ValueError):
    pass


def parse_google_client(client_id: str = "", client_secret: str = "", json_text: str = "") -> tuple[str, str]:
    """Accepts either the two fields or the whole downloaded client_secret_....json."""
    if json_text.strip():
        try:
            d = json.loads(json_text)
        except json.JSONDecodeError:
            raise AccountError("That doesn't look like the JSON file Google gave you.")
        c = d.get("web") or d.get("installed") or d
        client_id, client_secret = c.get("client_id", ""), c.get("client_secret", "")
        if d.get("installed"):
            raise AccountError("This is a Desktop client. Create a 'Web application' client instead (step 3).")
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not CLIENT_ID_RX.match(client_id):
        raise AccountError("The client ID should end with .apps.googleusercontent.com.")
    if not (10 <= len(client_secret) <= 100) or re.search(r"\s", client_secret):
        raise AccountError("The client secret looks wrong. Copy it again from Google Cloud.")
    return client_id, client_secret


class AccountManager:
    def __init__(self, svc):
        self.svc = svc
        self.db = svc.db
        self._flows: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ startup
    def apply_saved(self) -> None:
        """Called once at startup: switch to whatever accounts were connected from the app."""
        g = GoogleAuth.from_db(self.db)
        if g and g.is_connected():
            self._use_google(g)
        tok = self.db.secret_get(SLACK_SECRET)
        if tok:
            self._use_slack(tok)
        raw = self.db.secret_get(GITHUB_SECRET)
        if raw:
            d = json.loads(raw)
            self._use_github(d["token"], d.get("user"))

    # ------------------------------------------------------------------ status
    def status(self, redirect_uri: str) -> dict:
        g = GoogleAuth.from_db(self.db)
        modes = self.svc.integration_modes
        google_on = modes.get("mail") == "google"
        email = None
        if google_on:
            auth = getattr(self.svc.mail, "auth", None)
            email = auth.account_email() if auth else None
        gh = json.loads(self.db.secret_get(GITHUB_SECRET) or "{}")
        return {
            "google": {"client_saved": g is not None, "connected": google_on, "email": email,
                       "redirect_uri": redirect_uri, "last_error": self.last_error,
                       "client_id_hint": (g.client_id[:12] + "...") if g else None},
            "slack": {"connected": modes.get("slack") == "slack",
                      "team": self.db.kv_get("slack_team"), "from_env": bool(self.svc.settings.slack_user_token)},
            "github": {"connected": modes.get("github") == "github", "user": gh.get("user") or
                       (self.svc.settings.github_user if modes.get("github") == "github" else None),
                       "from_env": bool(self.svc.settings.github_token)},
            "demo_data": self.svc.settings.seed_demo_data,
        }

    # ------------------------------------------------------------------ Google
    def save_google_client(self, client_id: str = "", client_secret: str = "", json_text: str = "") -> None:
        cid, sec = parse_google_client(client_id, client_secret, json_text)
        self.db.secret_set(CLIENT_SECRET_NAME, json.dumps({"client_id": cid, "client_secret": sec}))
        self.last_error = None

    def start_google(self, redirect_uri: str, login_hint: Optional[str] = None) -> str:
        g = GoogleAuth.from_db(self.db)
        if not g:
            raise AccountError("Add your Google client ID and secret first (steps 1 to 4).")
        f = GoogleAuth.new_flow()
        with self._lock:
            now = time.time()
            self._flows = {k: v for k, v in self._flows.items() if v["exp"] > now}
            if len(self._flows) > 5:
                raise AccountError("Too many sign-in attempts. Wait a few minutes.")
            self._flows[f["state"]] = {"verifier": f["verifier"], "redirect": redirect_uri, "exp": now + FLOW_TTL}
        self.last_error = None
        if login_hint and "@" not in login_hint:
            login_hint = None
        return g.auth_url(redirect_uri, f["state"], f["challenge"], login_hint)

    def finish_google(self, state: str, code: Optional[str], error: Optional[str]) -> str:
        """Returns the connected email. Raises AccountError with a message for the browser page."""
        with self._lock:
            flow = self._flows.pop(state or "", None)       # single use
        if not flow or flow["exp"] < time.time():
            raise AccountError("This sign-in link expired or was already used. Go back to JARVIS and tap Connect Google again.")
        if error:
            msg = "You cancelled the Google sign-in." if error == "access_denied" else f"Google said: {error}."
            self.last_error = msg
            raise AccountError(msg)
        if not code:
            raise AccountError("Google didn't send a sign-in code.")
        g = GoogleAuth.from_db(self.db)
        try:
            email = g.exchange(code, flow["redirect"], flow["verifier"])
        except GoogleAuthError as e:
            self.last_error = str(e)
            if g.is_connected():          # tokens stored but some scopes missing: still switch over
                self._use_google(g)
            raise AccountError(str(e))
        self._use_google(g)
        self.db.audit("accounts", "google.connect", {}, "done", "Google account connected")
        return email

    def disconnect_google(self) -> None:
        g = GoogleAuth.from_db(self.db)
        if g:
            try:
                g.revoke()
            except Exception as e:                        # revoke is best effort; the token is deleted anyway
                log.warning("Google revoke: %s", e)
                self.db.execute("DELETE FROM secrets WHERE name=?", (SECRET_NAME,))
        from jarvis.integrations.calendar import MockCalendar
        from jarvis.integrations.mail import MockMail
        self.svc.calendar, self.svc.mail = MockCalendar(), MockMail()
        self.svc.integration_modes["calendar"] = self.svc.integration_modes["mail"] = "mock"

    def _use_google(self, g: GoogleAuth) -> None:
        from jarvis.integrations.calendar import GoogleCalendar
        from jarvis.integrations.mail import GmailProvider
        self.svc.calendar = GoogleCalendar(g, self.svc.settings.timezone)
        self.svc.mail = GmailProvider(g)
        self.svc.integration_modes["calendar"] = self.svc.integration_modes["mail"] = "google"

    # ------------------------------------------------------------------ Slack / GitHub
    def connect_slack(self, token: str) -> str:
        token = token.strip()
        if not re.match(r"^xox[pb]-[A-Za-z0-9-]{20,}$", token):
            raise AccountError("A Slack user token starts with xoxp- (see the steps).")
        try:
            r = httpx.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"}, timeout=15).json()
        except httpx.HTTPError as e:
            raise AccountError(f"Couldn't reach Slack: {e.__class__.__name__}.")
        if not r.get("ok"):
            raise AccountError(f"Slack rejected the token ({r.get('error', 'unknown error')}).")
        self.db.secret_set(SLACK_SECRET, token)
        self.db.kv_set("slack_team", r.get("team"))
        self._use_slack(token)
        return r.get("team") or "Slack"

    def connect_github(self, token: str) -> str:
        token = token.strip()
        if not re.match(r"^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$", token):
            raise AccountError("A GitHub token starts with github_pat_ or ghp_.")
        try:
            r = httpx.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}",
                                                                 "Accept": "application/vnd.github+json"}, timeout=15)
        except httpx.HTTPError as e:
            raise AccountError(f"Couldn't reach GitHub: {e.__class__.__name__}.")
        if r.status_code != 200:
            raise AccountError(f"GitHub rejected the token (HTTP {r.status_code}).")
        user = r.json().get("login")
        self.db.secret_set(GITHUB_SECRET, json.dumps({"token": token, "user": user}))
        self._use_github(token, user)
        return user

    def disconnect(self, kind: str) -> None:
        if kind == "google":
            return self.disconnect_google()
        from jarvis.integrations.chat_apps import MockGitHub, MockSlack
        if kind == "slack":
            self.db.execute("DELETE FROM secrets WHERE name=?", (SLACK_SECRET,))
            self.svc.slack, self.svc.integration_modes["slack"] = MockSlack(), "mock"
        elif kind == "github":
            self.db.execute("DELETE FROM secrets WHERE name=?", (GITHUB_SECRET,))
            self.svc.github, self.svc.integration_modes["github"] = MockGitHub(self.svc.settings.github_user), "mock"
        else:
            raise AccountError("Unknown account.")

    def _use_slack(self, token: str) -> None:
        from jarvis.integrations.chat_apps import SlackWebAPI
        self.svc.slack, self.svc.integration_modes["slack"] = SlackWebAPI(token), "slack"

    def _use_github(self, token: str, user: Optional[str]) -> None:
        from jarvis.integrations.chat_apps import GitHubREST
        self.svc.github = GitHubREST(token, user or self.svc.settings.github_user)
        self.svc.integration_modes["github"] = "github"
