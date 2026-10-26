"""Reel features: overnight triage + report, "is anyone mad?", morning-call settings, About you, recall."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import NOW, offline_settings
from jarvis.agents.triage import DRAFT_TTL, InboxTriage


def add_overnight_mail(svc):
    m = svc.mail
    m.add("Kavya", "kavya.demo@example.com", "Dataset?", "Hi, can you share the cloth dataset link today?", NOW - timedelta(hours=3))
    m.add("Dr. Mehta", "mehta.demo@example.com", "Report", "I've asked three times now and still no report. This is unacceptable.",
          NOW - timedelta(hours=2))
    m.add("Shop", "noreply@shop.example.com", "Your order #4412 has shipped", "Tracking inside.", NOW - timedelta(hours=1))


def test_triage_sorts_mail_and_drafts_replies_for_approval(granted):
    add_overnight_mail(granted)
    stats = InboxTriage(granted).run(draft=True)
    assert stats["suspicious"] == 1 and "Dr. Mehta" in stats["upset"]
    pending = [p["payload"] for p in granted.db.pending_requests() if p["payload"]["tool"] == "mail.send"]
    to = sorted(a for p in pending for a in p["args"]["to"])
    assert "kavya.demo@example.com" in to and "mehta.demo@example.com" in to
    assert not any("noreply" in a for a in to)                         # no reply to automatic mail
    assert not any("attacker" in str(p) for p in pending)               # phishing never drafted for
    assert granted.mail.sent == []                                      # nothing sent
    p = pending[0]
    import time as _t
    assert p["untrusted"] and p["expires"] > _t.time() + DRAFT_TTL - 120     # still there in the morning
    again = InboxTriage(granted).run(draft=True)
    assert again["processed"] == 0                                      # each email handled once


def test_triage_needs_permission(svc):
    add_overnight_mail(svc)
    assert InboxTriage(svc).run(draft=True)["processed"] == 0
    assert svc.db.pending_requests() == []                              # and no permission spam at night


def test_overnight_report(brain):
    svc = brain.svc
    add_overnight_mail(svc)
    InboxTriage(svc).run(draft=True)
    svc.github.commits = [{"sha": "abc1234", "message": "Fix wake word on Samsung", "author": "EC",
                           "time": NOW - timedelta(hours=6, minutes=46)}]
    r = brain.handle("overnight report")
    t = r.reply
    assert t.startswith("Since we last spoke") and "need" in t and "waiting for your approval" in t
    assert "Dr. Mehta sounded upset" in t and "phishing" in t
    assert "Fix wake word on Samsung" in t and "passing" in t
    assert "Nothing needed you" not in t                                # there was something to do


def test_quiet_night_says_nothing_needed_you(brain):
    for e in brain.svc.mail.inbox:
        e.unread = False
    for p in brain.svc.db.pending_requests():
        brain.svc.db.resolve_request(p["id"], "rejected")
    r = brain.handle("what happened overnight?")
    assert r.reply.startswith("Since we last spoke") and "Nothing needed you" in r.reply


def test_is_anyone_mad(brain):
    assert "Nobody is upset" in brain.handle("is anyone mad at me?").reply
    add_overnight_mail(brain.svc)
    assert "Dr. Mehta" in brain.handle("is anyone mad?").reply


# ============================================================================ settings through the server
TOKEN = "night-token-1234567890"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    from jarvis.core.storage import Database
    from jarvis.integrations.device import SimulatedDevice
    from jarvis.server.app import create_app
    from jarvis.services import build_services

    s = offline_settings()
    s.device_token = TOKEN
    s.update_repo = ""
    svc = build_services(s, db=Database(), clock=lambda: NOW, seed=True, device=SimulatedDevice())
    app = create_app(s, svc=svc, enable_wakeword=False)
    return TestClient(app), svc


def test_brief_settings(client):
    c, _ = client
    assert c.get("/api/brief", headers=H).json() == {"enabled": False, "time": "06:00", "drafts": True}
    r = c.post("/api/brief", json={"enabled": True, "time": "05:45", "drafts": False}, headers=H)
    assert r.json() == {"enabled": True, "time": "05:45", "drafts": False}
    assert c.post("/api/brief", json={"enabled": True, "time": "25:00"}, headers=H).status_code == 422
    assert c.get("/api/brief").status_code == 401


def test_about_you_is_redacted_and_used(client, monkeypatch):
    c, svc = client
    r = c.post("/api/profile", json={"about": "PhD student. Keep emails short. My OTP is 482913."}, headers=H)
    assert "482913" not in r.json()["about"] and "Keep emails short" in r.json()["about"]
    seen = {}
    real = svc.smart_llm.complete

    def spy(prompt, system=None, **kw):
        if kw.get("task") == "chat":
            seen["system"] = system
        return real(prompt, system=system, **kw)

    monkeypatch.setattr(svc.smart_llm, "complete", spy)
    from jarvis.brain import Brain
    b = Brain(svc)
    b.handle("I love the cloth manipulation project")
    b.handle("tell me a joke about cloth manipulation")
    assert "Keep emails short" in seen["system"]
    assert "Earlier conversations" in seen["system"] and "cloth manipulation project" in seen["system"]
