"""
Permission broker, approval queue and guardrail: the parts that protect the phone.
Every test here is a failure mode we are proving cannot happen.
"""

import threading
import time

import pytest

from src.agents.guardrail import ActionSeverity, TargetSensitivity
from jarvis.core.actions import Tool
from jarvis.core.permissions import PermStatus, is_blocked_app


def test_permission_prompt_then_grant_once_is_consumed(svc):
    out = svc.executor.propose("device.set_alarm", {"hour": 6, "minute": 0, "label": "x"}, "device")
    assert out.status == "needs_permission"
    assert out.request["scope"] == "alarms.write"
    res = svc.executor.resolve_permission(out.request["id"], "once")
    assert res.status == "done"
    assert svc.device.alarms[-1]["hour"] == 6
    # "Only this time": the next alarm asks again.
    assert svc.executor.propose("device.set_alarm", {"hour": 7, "minute": 0}, "device").status == "needs_permission"


def test_permission_deny_is_remembered(svc):
    out = svc.executor.propose("device.location", {}, "places")
    svc.executor.resolve_permission(out.request["id"], "deny")
    again = svc.executor.propose("device.location", {}, "places")
    assert again.status == "denied"
    assert svc.db.pending_requests() == []  # no nagging


def test_permission_request_answered_twice(svc):
    out = svc.executor.propose("device.location", {}, "places")
    svc.executor.resolve_permission(out.request["id"], "always")
    assert svc.executor.resolve_permission(out.request["id"], "always").status == "error"


@pytest.mark.parametrize("scope", ["sms.read", "payments", "passwords", "banking", "accessibility.full"])
def test_blocked_scopes_can_never_be_granted(svc, scope):
    with pytest.raises(PermissionError):
        svc.broker.grant(scope, "always")
    assert svc.broker.check([scope]).status == PermStatus.BLOCKED


def test_unknown_tool_is_blocked(svc):
    assert svc.executor.propose("shell.exec", {"cmd": "rm -rf /"}, "chat").status == "blocked"


@pytest.mark.parametrize("app", ["GPay", "Google Pay", "PhonePe", "Paytm", "HDFC Bank", "SBI YONO", "1Password",
                                 "Google Authenticator", "Zerodha Kite", "mAadhaar"])
def test_blocked_apps(granted, app):
    assert is_blocked_app(app)
    out = granted.executor.propose("device.open_app", {"app": app}, "device")
    assert out.status == "blocked"
    assert app not in granted.device.opened


@pytest.mark.parametrize("app", ["Spotify", "WhatsApp", "Maps", "Chrome"])
def test_normal_apps_open(granted, app):
    assert granted.executor.propose("device.open_app", {"app": app}, "device").status == "done"


def test_sending_always_needs_approval_even_to_known_contact(granted):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    assert out.status == "needs_approval"
    assert granted.mail.sent == []


def test_approval_is_idempotent(granted):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    rid = out.request["id"]
    assert granted.executor.resolve_approval(rid, True).status == "done"
    assert granted.executor.resolve_approval(rid, True).status == "error"
    assert len(granted.mail.sent) == 1  # never sent twice


def test_concurrent_approvals_send_once(granted):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    rid = out.request["id"]
    threads = [threading.Thread(target=granted.executor.resolve_approval, args=(rid, True)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(granted.mail.sent) == 1


def test_unknown_recipient_needs_on_screen_confirmation(granted):
    out = granted.executor.propose("mail.send", {"to": ["stranger@unknown.net"], "subject": "s", "body": "b"}, "communication")
    assert out.request["strong_auth"] is True
    voice = granted.executor.resolve_approval(out.request["id"], True, via="voice")
    assert voice.status == "needs_approval" and granted.mail.sent == []
    assert granted.executor.resolve_approval(out.request["id"], True, via="tap").status == "done"


def test_edits_limited_to_editable_fields(granted):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    granted.executor.resolve_approval(out.request["id"], True, edits={"body": "edited", "to": ["evil@x.com"]})
    sent = granted.mail.sent[-1]
    assert sent["body"] == "edited" and sent["to"] == ["arun.demo@example.com"]


def test_approval_expires(granted, monkeypatch):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)
    assert granted.executor.resolve_approval(out.request["id"], True).status == "expired"
    assert granted.mail.sent == []


def test_permission_revoked_between_proposal_and_approval(granted):
    out = granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    granted.broker.revoke("mail.send")
    assert granted.executor.resolve_approval(out.request["id"], True).status == "denied"
    assert granted.mail.sent == []


def test_lockdown_revokes_everything_and_blocks_writes(granted):
    granted.executor.propose("mail.send", {"to": ["arun.demo@example.com"], "subject": "s", "body": "b"}, "communication")
    n = granted.executor.lockdown()
    assert n == 1
    assert granted.db.all_grants() == {}
    assert granted.executor.propose("device.set_alarm", {"hour": 1, "minute": 0}, "device").status == "blocked"
    granted.executor.unlock()
    assert granted.executor.propose("device.set_alarm", {"hour": 1, "minute": 0}, "device").status == "needs_permission"


def test_untrusted_origin_forces_approval(granted):
    out = granted.executor.propose("device.set_alarm", {"hour": 3, "minute": 0}, "communication", untrusted=True)
    assert out.status == "needs_approval"
    assert granted.device.alarms == []


def test_self_reversible_write_is_auto(granted):
    assert granted.executor.propose("device.set_alarm", {"hour": 6, "minute": 0}, "device").status == "done"


def test_irreversible_always_needs_approval(granted):
    ev = granted.calendar.list_events(granted.now().replace(hour=0), granted.now().replace(hour=23))[0]
    out = granted.executor.propose("calendar.cancel", {"event_id": ev.id, "title": ev.title, "start": ev.start.isoformat()}, "scheduler")
    assert out.status == "needs_approval"
    assert ev.id in granted.calendar.events


def test_resource_lock_serialises_writes(granted):
    order = []

    def slow(a):
        order.append(("start", a["i"]))
        time.sleep(0.05)
        order.append(("end", a["i"]))
        return {}

    granted.executor.register(Tool("test.slow", run=slow, describe=lambda a: "x", severity=ActionSeverity.WRITE,
                                   resource=lambda a: "shared"))
    ts = [threading.Thread(target=granted.executor.propose, args=("test.slow", {"i": i}, "t")) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    for k in range(0, 6, 2):  # every start is immediately followed by its own end
        assert order[k][0] == "start" and order[k + 1] == ("end", order[k][1])


def test_audit_log_records_everything(granted):
    granted.executor.propose("device.set_alarm", {"hour": 6, "minute": 0}, "device")
    granted.executor.propose("device.open_app", {"app": "GPay"}, "device")
    outcomes = [r["outcome"] for r in granted.db.audit_log()]
    assert "done" in outcomes and "blocked" in outcomes


def test_tool_failure_is_contained(granted):
    granted.executor.register(Tool("test.boom", run=lambda a: 1 / 0, describe=lambda a: "x"))
    out = granted.executor.propose("test.boom", {}, "t")
    assert out.status == "error"


def test_tool_registration_rejects_unknown_scope(granted):
    with pytest.raises(ValueError):
        granted.executor.register(Tool("test.bad", run=lambda a: 1, describe=lambda a: "x", scopes=("god.mode",)))


def test_notifications_filtered_on_brain_side(granted):
    out = granted.executor.propose("device.notifications", {}, "device")
    texts = " ".join(n["text"] for n in out.data["notifications"])
    apps = {n["app"] for n in out.data["notifications"]}
    assert "482913" not in texts
    assert not apps & {"HDFC Bank", "GPay"}
