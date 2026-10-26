"""Connected accounts (Google sign-in from the app, Slack/GitHub tokens), follow-ups and memory statements."""

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from conftest import NOW, offline_settings
from jarvis.core.storage import Database
from test_server_voice import H, make_app

CID = "1234567890-abcdefghijklmnop.apps.googleusercontent.com"
SEC = "GOCSPX-abcdefghijklmnopqrstuv"


class FakeResp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


@pytest.fixture
def client(tmp_path):
    app = make_app(tmp_path)
    return TestClient(app, base_url="https://jarvis.tail.ts.net"), app.state.svc


def test_google_flow_end_to_end(client, monkeypatch):
    c, svc = client
    st = c.get("/api/accounts", headers=H).json()
    assert st["google"]["connected"] is False
    assert st["google"]["redirect_uri"] == "https://jarvis.tail.ts.net/oauth/google/callback"
    # Starting before the client is saved explains what to do.
    r = c.post("/api/accounts/google/start", headers=H, json={})
    assert r.status_code == 400 and "client ID" in r.json()["detail"]
    # Bad client values are rejected with a readable reason.
    r = c.post("/api/accounts/google/client", headers=H, json={"client_id": "nope", "client_secret": SEC})
    assert r.status_code == 400 and "googleusercontent" in r.json()["detail"]
    # The downloaded JSON works too.
    r = c.post("/api/accounts/google/client", headers=H, json={"json_text": json.dumps({"web": {"client_id": CID, "client_secret": SEC}})})
    assert r.status_code == 200 and r.json()["google"]["client_saved"]
    assert SEC not in r.text                                   # the secret never comes back
    url = c.post("/api/accounts/google/start", headers=H, json={}).json()["url"]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://accounts.google.com/") and q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline" and "gmail.send" in q["scope"] and "openid" in q["scope"]
    assert q["redirect_uri"].endswith("/oauth/google/callback")

    calls = {}

    def fake_post(url, data=None, **k):
        calls["token"] = data
        return FakeResp(200, {"access_token": "ya29.x", "refresh_token": "1//r", "expires_in": 3600,
                              "scope": "openid email https://www.googleapis.com/auth/calendar.events "
                                       "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"})

    monkeypatch.setattr("jarvis.integrations.google_auth.httpx.post", fake_post)
    monkeypatch.setattr("jarvis.integrations.google_auth.httpx.get", lambda *a, **k: FakeResp(200, {"email": "yogesh@gmail.com"}))
    # A forged state is refused and changes nothing.
    r = c.get("/oauth/google/callback", params={"state": "forged", "code": "abc"})
    assert r.status_code == 400 and "expired" in r.text and svc.integration_modes["mail"] == "mock"
    r = c.get("/oauth/google/callback", params={"state": q["state"], "code": "4/abc"})
    assert r.status_code == 200 and "yogesh@gmail.com" in r.text
    assert calls["token"]["code_verifier"] and calls["token"]["redirect_uri"] == q["redirect_uri"]
    from jarvis.integrations.mail import GmailProvider
    assert isinstance(svc.mail, GmailProvider) and svc.integration_modes["calendar"] == "google"
    st = c.get("/api/accounts", headers=H).json()["google"]
    assert st["connected"] and st["email"] == "yogesh@gmail.com"
    # The state is single use.
    assert c.get("/oauth/google/callback", params={"state": q["state"], "code": "4/abc"}).status_code == 400
    # Survives a restart: a fresh service container on the same DB comes up on Gmail.
    from jarvis.services import build_services
    svc2 = build_services(offline_settings(), db=svc.db, clock=lambda: NOW, seed=False)
    assert svc2.integration_modes["mail"] == "google"
    # Disconnect goes back to "not connected".
    monkeypatch.setattr("jarvis.integrations.google_auth.httpx.post", lambda *a, **k: FakeResp(200, {}))
    st = c.post("/api/accounts/google/disconnect", headers=H, json={}).json()
    assert not st["google"]["connected"] and svc.integration_modes["mail"] == "mock"


def test_google_cancel_and_missing_scopes(client, monkeypatch):
    c, svc = client
    c.post("/api/accounts/google/client", headers=H, json={"client_id": CID, "client_secret": SEC})
    q = lambda: dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        c.post("/api/accounts/google/start", headers=H, json={}).json()["url"]).query))
    r = c.get("/oauth/google/callback", params={"state": q()["state"], "error": "access_denied"})
    assert r.status_code == 400 and "cancelled" in r.text
    monkeypatch.setattr("jarvis.integrations.google_auth.httpx.post", lambda *a, **k: FakeResp(
        200, {"access_token": "a", "refresh_token": "r", "scope": "openid email https://www.googleapis.com/auth/calendar.events"}))
    monkeypatch.setattr("jarvis.integrations.google_auth.httpx.get", lambda *a, **k: FakeResp(200, {"email": "y@gmail.com"}))
    r = c.get("/oauth/google/callback", params={"state": q()["state"], "code": "x"})
    assert "unticked" in r.text and "gmail.readonly" in r.text
    assert "unticked" in c.get("/api/accounts", headers=H).json()["google"]["last_error"]


def test_callback_escapes_and_accounts_need_auth(client):
    c, _ = client
    assert c.get("/api/accounts").status_code == 401
    assert c.post("/api/accounts/slack/connect", json={"token": "xoxp-123456789012345678901234"}).status_code == 401
    r = c.get("/oauth/google/callback", params={"state": "<script>alert(1)</script>"})
    assert "<script>" not in r.text


def test_lockdown_freezes_accounts(client):
    c, _ = client
    c.post("/api/lockdown", headers=H, json={})
    r = c.post("/api/accounts/google/client", headers=H, json={"client_id": CID, "client_secret": SEC})
    assert r.status_code == 423


def test_slack_and_github_tokens(client, monkeypatch):
    c, svc = client
    r = c.post("/api/accounts/slack/connect", headers=H, json={"token": "not-a-slack-token"})
    assert r.status_code == 400 and "xoxp" in r.json()["detail"]
    monkeypatch.setattr("jarvis.integrations.accounts.httpx.post", lambda *a, **k: FakeResp(200, {"ok": False, "error": "invalid_auth"}))
    r = c.post("/api/accounts/slack/connect", headers=H, json={"token": "xoxp-123456789012345678901234"})
    assert r.status_code == 400 and "invalid_auth" in r.json()["detail"]
    monkeypatch.setattr("jarvis.integrations.accounts.httpx.post", lambda *a, **k: FakeResp(200, {"ok": True, "team": "IITM Lab"}))
    st = c.post("/api/accounts/slack/connect", headers=H, json={"token": "xoxp-123456789012345678901234"}).json()
    assert st["slack"]["connected"] and st["slack"]["team"] == "IITM Lab" and svc.integration_modes["slack"] == "slack"
    monkeypatch.setattr("jarvis.integrations.accounts.httpx.get", lambda *a, **k: FakeResp(200, {"login": "YogeshRajasekhar"}))
    st = c.post("/api/accounts/github/connect", headers=H, json={"token": "github_pat_" + "a" * 30}).json()
    assert st["github"]["connected"] and st["github"]["user"] == "YogeshRajasekhar"
    assert "xoxp-1234" not in json.dumps(st)
    st = c.post("/api/accounts/slack/disconnect", headers=H, json={}).json()
    assert not st["slack"]["connected"]


def test_mail_without_google_says_so():
    from jarvis.brain import Brain
    from jarvis.services import build_services
    s = offline_settings()
    s.seed_demo_data = False
    svc = build_services(s, db=Database(":memory:"), clock=lambda: NOW, seed=False)
    for sc in ("mail.read", "mail.send", "contacts.read"):
        svc.broker.grant(sc, "always")
    b = Brain(svc)
    for t in ("read my latest emails", "email arun@example.com that I'll be late"):
        r = b.handle(t)
        assert "Connect Google" in r.reply and not r.requests


# ------------------------------------------------------------------ context: follow-ups and memory
def test_followup_is_rewritten(brain):
    brain.handle("what's on my calendar today")
    r = brain.handle("and tomorrow?")
    assert r.understood and "tomorrow" in r.understood.lower() and "calendar" in r.understood.lower()
    assert "tomorrow" in r.reply.lower()
    # A standalone message is not rewritten.
    assert brain.handle("set an alarm for 6am").understood is None


def test_statements_are_remembered_and_answered(brain):
    r = brain.handle("my sister Ananya studies at Anna University")
    assert r.agents == ["memory"] and "Ananya is your sister" in r.reply
    r = brain.handle("where does my sister study?")
    assert "Anna University" in r.reply
    brain.handle("Karthik is my roommate")
    assert "Karthik" in brain.handle("who is my roommate?").reply
    # Scheduling statements still go to the scheduler, not memory.
    from jarvis.agents.memory import is_statement
    assert not is_statement("my meeting with Arun is at 5")


# ------------------------------------------------------------------ server voice
def test_server_voice(client):
    pytest.importorskip("piper")
    c, _ = client
    vs = c.get("/api/tts/voices", headers=H).json()["voices"]
    if not vs:
        pytest.skip("no voice files")
    assert vs[0]["id"] == "jarvis"
    r = c.post("/api/tts", headers=H, json={"text": "Good morning. Two meetings today.", "voice": "jarvis"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) > 20000
    assert c.post("/api/tts", headers=H, json={"text": "x", "voice": "../etc"}).status_code == 422
    assert c.get("/api/status", headers=H).json()["server_voice"] is True


def test_colour_keeps_length_and_level():
    import numpy as np
    from jarvis.voice.speech import synthetic_colour
    x = (0.5 * np.sin(np.linspace(0, 2000, 22050 * 2))).astype(np.float32)
    y = synthetic_colour(x, 22050, 0.93)
    assert abs(len(y) - len(x) / 0.93) < 2 and float(np.max(np.abs(y))) <= 0.9


def test_call_me_schedules_a_ringing_call(brain):
    r = brain.handle("call me at 6pm about the laundry")
    assert "call you" in r.reply and "laundry" in r.reply
    last = brain.svc.device.log[-1] if hasattr(brain.svc.device, "log") else None
    if last:
        assert last[0] == "set_reminder" and last[1]["call"] is True
    assert "When" in brain.handle("call me").reply or "time" in brain.handle("call me").reply


def test_kokoro_voice_when_installed():
    pytest.importorskip("kokoro_onnx")
    import os
    from jarvis.voice.speech import available_voices, speak
    md = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
    if not any(v["id"] == "george" for v in available_voices(md)):
        pytest.skip("Kokoro model files not downloaded")
    wav = speak("Hello there.", "jarvis", md)
    assert wav[:4] == b"RIFF" and len(wav) > 20000
