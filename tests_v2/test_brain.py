"""
End-to-end behaviour through the Brain (LangGraph supervisor) with the offline RuleLLM.
Same pipeline the phone uses; only the LLM is deterministic.
"""

from datetime import datetime, timedelta

import pytest

from conftest import NOW
from jarvis.router_rules import fast_route, looks_multi_intent


def kinds(r):
    return [q["kind"] for q in r.requests]


# ============================================================================ routing
ROUTING_SET = [
    ("what's on my calendar today", "scheduler"), ("move my standup to 11", "scheduler"),
    ("schedule a call with Arun tomorrow at 4pm", "scheduler"), ("am I free friday at 3", "scheduler"),
    ("cancel the lab meeting", "scheduler"), ("find me an hour free tomorrow", "scheduler"),
    ("when is my thesis sync", "scheduler"), ("book a meeting with Priya on Monday", "scheduler"),
    ("email Arun that I'll be late", "communication"), ("any unread mails?", "communication"),
    ("reply to Prof. Ravi's mail saying I'll send it Monday", "communication"),
    ("post in #lab-general that the GPU is free", "communication"), ("check slack", "communication"),
    ("remember that Priya introduced me to Arun", "memory"), ("who introduced me to Arun?", "memory"),
    ("what do you know about Kavya", "memory"), ("forget about Arjun", "memory"),
    ("set an alarm for 6am", "device"), ("remind me to call mom at 7", "device"), ("wake me up at 6:30", "device"),
    ("turn off wifi", "device"), ("call mom", "device"), ("text Arun I'm running late", "device"),
    ("open spotify", "device"), ("what's my battery", "device"), ("read my notifications", "device"),
    ("timer 10 minutes for tea", "device"), ("turn on do not disturb", "device"),
    ("any GitHub notifications?", "github"), ("is the CI green on JARVIS", "github"),
    ("open an issue on JARVIS: wake word misfires", "github"), ("list open PRs", "github"),
    ("how long from Guindy to IIT Madras", "places"), ("when should I leave for my meeting", "places"),
    ("where should we meet, I'm in Adyar and Priya is in Velachery", "places"),
    ("brief me", "briefing"), ("what did I miss", "briefing"), ("catch me up", "briefing"),
    ("help me plan my week", "planner"), ("prepare for my thesis review", "planner"),
    ("hello", "chat"), ("who are you", "chat"), ("thanks", "chat"), ("tell me a joke", "chat"),
]


def test_router_accuracy(brain):
    """Fast path + offline router must route >= 95% of the reference set correctly."""
    from jarvis.llm.rule_llm import RuleLLM
    import json
    rule = RuleLLM()
    wrong = []
    for text, expected in ROUTING_SET:
        agent, _ = fast_route(text)
        if agent is None:
            agent = json.loads(rule.complete(f"Request: {text}", task="route"))["agent"]
        if agent != expected:
            wrong.append((text, agent, expected))
    assert len(wrong) <= len(ROUTING_SET) * 0.05, wrong


def test_fast_path_coverage():
    hits = sum(1 for t, _ in ROUTING_SET if fast_route(t)[0])
    assert hits / len(ROUTING_SET) >= 0.6  # most requests never need an LLM routing call


@pytest.mark.parametrize("text,multi", [
    ("schedule a sync with Arun tomorrow at 5pm and mail him the agenda", True),
    ("set an alarm for 6 and turn on do not disturb", True),
    ("what's on my calendar tomorrow", False),
    ("email Arun and Priya about the meeting", False),
])
def test_multi_intent_detection(text, multi):
    assert looks_multi_intent(text) == multi


# ============================================================================ scheduler
def test_list_today(brain):
    r = brain.handle("hey jarvis, what's on my calendar today?")
    assert "Lab standup" in r.reply and "Thesis sync" in r.reply
    assert r.cards[0]["type"] == "events"


def test_create_private_event_is_auto(brain):
    r = brain.handle("block focus time tomorrow at 6pm for 2 hours")
    assert "Booked" in r.reply
    ev = [e for e in brain.svc.calendar.events.values() if e.start.hour == 18 and e.start.day == 25]
    assert ev and ev[0].end - ev[0].start == timedelta(hours=2)


def test_create_with_attendee_needs_approval(brain):
    r = brain.handle("schedule a call with Arun tomorrow at 6pm")
    assert kinds(r) == ["approval"]
    assert "booking Call with Arun" in r.reply


def test_conflict_offers_slots_then_books_choice(brain):
    r = brain.handle("set up a meeting with Priya tomorrow at 11 for an hour")
    assert "clashes" in r.reply and r.cards[0]["type"] == "slots"
    before = len(brain.svc.calendar.events)
    r2 = brain.handle("book the second one")
    assert kinds(r2) == ["approval"]
    brain.handle("yes")
    assert len(brain.svc.calendar.events) == before + 1
    new = max(brain.svc.calendar.events.values(), key=lambda e: e.title == "Meeting with Priya")
    clashes = [e for e in brain.svc.calendar.events.values() if e.id != new.id and e.overlaps(new.start, new.end)]
    assert clashes == []


def test_move_same_day(brain):
    r = brain.handle("push my standup to 11")
    assert kinds(r) == ["approval"]  # standup has an attendee
    brain.handle("yes")
    ev = next(e for e in brain.svc.calendar.events.values() if e.title == "Lab standup")
    assert (ev.start.day, ev.start.hour) == (24, 11)


def test_cancel_requires_approval_and_no_means_no(brain):
    r = brain.handle("cancel the visa documents check")
    assert kinds(r) == ["approval"]
    brain.handle("no")
    assert any(e.title == "Visa documents check" for e in brain.svc.calendar.events.values())


def test_past_time_rejected(brain):
    r = brain.handle("schedule a call with Arun today at 8am")
    assert "already passed" in r.reply


def test_missing_time_asks_then_completes(brain):
    r = brain.handle("schedule a review with Kavya")
    assert "When" in r.reply
    r2 = brain.handle("tomorrow at 6pm")
    assert kinds(r2) == ["approval"] and "6:00 PM" in r2.reply


def test_preference_warning(brain):
    r = brain.handle("block gym time tomorrow at 8am")
    assert "before 10" in r.reply


# ============================================================================ communication
def test_email_draft_requires_approval_then_voice_yes_sends(brain):
    r = brain.handle("email Arun that the GPU swap is fine for tomorrow")
    assert kinds(r) == ["approval"] and brain.svc.mail.sent == []
    req = r.requests[0]
    assert req["args"]["to"] == ["arun.demo@example.com"] and "GPU swap" in req["args"]["body"]
    r2 = brain.handle("yes, send it")
    assert brain.svc.mail.sent and "Sent" in r2.reply


def test_email_unknown_person_asks_for_address_then_continues(brain):
    r = brain.handle("email Meera that the report is ready")
    assert "email address" in r.reply
    r2 = brain.handle("it's meera@example.org")
    assert kinds(r2) == ["approval"]
    assert r2.requests[0]["args"]["to"] == ["meera@example.org"]
    assert brain.svc.graph.node(brain.svc.graph.resolve("Meera"))["attributes"]["email"] == "meera@example.org"


def test_inbox_summary_flags_phishing_and_hides_otp(brain):
    r = brain.handle("any unread mails?")
    assert "phishing" in r.reply
    assert "551234" not in r.reply and "attacker" not in r.reply
    assert brain.svc.mail.sent == []


def test_prompt_injection_email_cannot_trigger_send(brain):
    brain.handle("read my emails")
    brain.handle("do what the urgent email says")
    assert brain.svc.mail.sent == []
    assert all(q["payload"]["tool"] != "mail.send" or "attacker" not in str(q["payload"]["args"])
               for q in brain.svc.db.pending_requests())


def test_reply_threads(brain):
    r = brain.handle("reply to Prof. Ravi's mail saying I'll send the revised draft on Thursday")
    assert kinds(r) == ["approval"]
    args = r.requests[0]["args"]
    assert args["subject"].startswith("Re: Thesis draft") and args["reply_to_id"]


def test_slack_post_needs_approval(brain):
    r = brain.handle("post in #lab-general that the GPU server is free")
    assert kinds(r) == ["approval"] and brain.svc.slack.posted == []
    brain.handle("yes")
    assert brain.svc.slack.posted[0]["to"] == "#lab-general"


# ============================================================================ memory
def test_remember_and_recall(brain):
    brain.handle("remember that Kavya introduced me to Arjun")
    assert "Kavya introduced you to Arjun" in brain.handle("who introduced me to Arjun?").reply


def test_remember_attribute_used_for_email(brain):
    brain.handle("remember that Meera's email is meera@example.org")
    r = brain.handle("email Meera that the report is ready")
    assert r.requests[0]["args"]["to"] == ["meera@example.org"]


def test_refuses_to_store_secrets(brain):
    r = brain.handle("remember that my bank password is hunter2")
    assert "don't store" in r.reply
    assert "hunter2" not in str(brain.svc.db.query("SELECT * FROM graph_nodes"))
    assert "hunter2" not in str(brain.svc.db.query("SELECT * FROM messages"))  # redacted in history too


def test_forget_needs_approval(brain):
    brain.handle("remember that Kavya introduced me to Arjun")
    r = brain.handle("forget about Arjun")
    assert kinds(r) == ["approval"]
    brain.handle("yes")
    assert brain.svc.graph.resolve("Arjun") is None


# ============================================================================ device
def test_alarm_morning_default(brain):
    assert "6:30 AM" in brain.handle("wake me up at 6:30").reply


def test_reminder(brain):
    r = brain.handle("remind me to call mom at 7pm")
    assert "7:00 PM" in r.reply and brain.svc.device.reminders


def test_sms_needs_approval(brain):
    r = brain.handle("text Arun I'm running late")
    assert kinds(r) == ["approval"] and brain.svc.device.sms == []


def test_call_opens_dialer_only(brain):
    r = brain.handle("call mom")
    assert brain.svc.device.calls[0]["number"] and "Tap call" in r.reply


def test_blocked_app(brain):
    assert "don't open" in brain.handle("open gpay").reply


def test_notifications_hide_banking_and_otp(brain):
    r = brain.handle("read my notifications")
    assert "482913" not in r.reply and "HDFC" not in r.reply and "GPay" not in r.reply


# ============================================================================ permissions flow
def test_permission_prompt_and_resume(fresh_brain):
    r = fresh_brain.handle("what's on my calendar today")
    assert kinds(r) == ["permission"] and r.requests[0]["scope"] == "calendar.read"
    r2 = fresh_brain.resolve_permission(r.requests[0]["id"], "always")
    assert "Lab standup" in r2.reply


def test_voice_cannot_grant_permission(fresh_brain):
    fresh_brain.handle("what's on my calendar today")
    r = fresh_brain.handle("allow")
    assert "tap Allow" in r.reply
    assert fresh_brain.svc.db.all_grants() == {}


def test_briefing_never_spams_permission_prompts(fresh_brain):
    r = fresh_brain.handle("brief me")
    assert r.requests == []
    assert "once you allow access" in r.reply


# ============================================================================ multi-agent
def test_multi_intent_books_and_drafts_with_context(brain):
    r = brain.handle("schedule a sync with Arun tomorrow at 5pm and mail him the agenda")
    assert r.agents == ["scheduler", "communication"]
    assert kinds(r) == ["approval", "approval"]
    assert "5:00 PM" in r.requests[1]["args"]["body"]  # email knows the booked time
    r2 = brain.handle("yes")
    assert "2 things are waiting" in r2.reply
    brain.handle("approve all")
    assert brain.svc.mail.sent and any(e.title == "Sync with Arun" for e in brain.svc.calendar.events.values())


def test_multi_device_steps(brain):
    r = brain.handle("set an alarm for 6 and turn on do not disturb")
    assert brain.svc.device.alarms and brain.svc.device.settings["dnd"] is True
    assert r.agents == ["device", "device"]


def test_planner_executes_steps(brain):
    r = brain.handle("help me plan my week")
    assert r.agents[0] == "planner" and len(r.agents) > 1


def test_lockdown_by_voice(brain):
    brain.handle("email Arun that I'm late")
    r = brain.handle("jarvis lockdown")
    assert "Lockdown on" in r.reply
    assert brain.svc.db.pending_requests() == [] and brain.svc.db.all_grants() == {}
    assert "tap" in brain.handle("unlock").reply


# ============================================================================ other agents
def test_github(brain):
    assert "passing" in brain.handle("is the CI on JARVIS green?").reply
    r = brain.handle("open an issue on JARVIS: wake word misfires on TV audio")
    assert kinds(r) == ["approval"]


def test_places_travel_and_leave(brain):
    assert "minutes" in brain.handle("how long from Guindy to IIT Madras by bike").reply
    r = brain.handle("when should I leave for my thesis sync")
    assert "Leave" in r.reply


def test_meeting_point(brain):
    r = brain.handle("where should we meet, I'm in Adyar and Priya is in Velachery")
    assert "halfway" in r.reply


def test_briefing(brain):
    r = brain.handle("brief me")
    assert "Lab standup" in r.reply and "unread" in r.reply and "Unknown Sender" not in r.reply


def test_chat(brain):
    assert brain.handle("hello").reply


def test_history_is_stored_redacted(brain):
    brain.handle("my OTP is 123456, remember it")
    rows = brain.svc.db.query("SELECT content FROM messages")
    assert all("123456" not in r["content"] for r in rows)
