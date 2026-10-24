"""Server (auth, REST, WebSocket device bridge) and voice (wake word, TTS)."""

import os
import threading
import time
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import NOW, offline_settings
from jarvis.core.storage import Database

HERE = os.path.dirname(__file__)
MODELS = os.path.join(os.path.dirname(HERE), "models")
TOKEN = "test-token-abcdef123"


def make_app(tmp_path, wake=False):
    from jarvis.server.app import create_app
    from jarvis.services import build_services
    from jarvis.integrations.device import RemoteDevice, SimulatedDevice

    s = offline_settings()
    s.device_token = TOKEN
    # Build services ourselves so the clock is fixed, then let the app wire the bridge.
    svc = build_services(s, db=Database(":memory:"), clock=lambda: NOW, seed=True, device=RemoteDevice(lambda m: None))
    app = create_app(s, svc=svc, enable_wakeword=wake, model_dir=MODELS)
    svc.device = app.state.bridge
    app.state.bridge.fallback = SimulatedDevice()
    for sc in ("calendar.read", "mail.read", "mail.send", "contacts.read", "alarms.write"):
        svc.broker.grant(sc, "always")
    return app


H = {"Authorization": f"Bearer {TOKEN}"}


def test_auth_required_and_lockout(tmp_path):
    c = TestClient(make_app(tmp_path))
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/status").status_code == 401
    for _ in range(5):
        assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Locked out now, even with the right token.
    assert c.get("/api/status", headers=H).status_code == 401


def test_security_headers(tmp_path):
    r = TestClient(make_app(tmp_path)).get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_rest_message_and_approval(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.post("/api/message", json={"text": "email Arun that I'll be late"}, headers=H).json()
    assert r["pending"] and r["pending"][0]["kind"] == "approval"
    rid = r["pending"][0]["id"]
    r2 = c.post(f"/api/approval/{rid}", json={"approved": True, "via": "tap"}, headers=H).json()
    assert "Sent" in r2["reply"] and r2["pending"] == []


def test_message_validation(tmp_path):
    c = TestClient(make_app(tmp_path))
    assert c.post("/api/message", json={"text": "x" * 5000}, headers=H).status_code == 422
    assert c.post("/api/permission/abc", json={"mode": "forever"}, headers=H).status_code == 422


def test_unlock_requires_typed_confirmation(tmp_path):
    c = TestClient(make_app(tmp_path))
    c.post("/api/lockdown", headers=H)
    assert c.post("/api/unlock", json={"confirm": "yes"}, headers=H).status_code == 400
    assert c.post("/api/unlock", json={"confirm": "UNLOCK"}, headers=H).status_code == 200


def test_ws_rejects_bad_token(tmp_path):
    c = TestClient(make_app(tmp_path))
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        assert ws.receive_json()["type"] == "error"


def test_ws_device_command_roundtrip(tmp_path):
    """The brain asks the connected phone to set an alarm; the phone executes and answers."""
    c = TestClient(make_app(tmp_path))
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "device": True})
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "message", "text": "wake me up at 6:30"})
        cmd = ws.receive_json()
        assert cmd["type"] == "device_command" and cmd["command"] == "set_alarm"
        assert (cmd["args"]["hour"], cmd["args"]["minute"]) == (6, 30)
        ws.send_json({"type": "device_result", "id": cmd["id"], "result": {"ok": True}})
        reply = ws.receive_json()
        assert reply["type"] == "reply" and "6:30 AM" in reply["reply"]


def test_ws_permission_flow(tmp_path):
    app = make_app(tmp_path)
    app.state.svc.broker.revoke("calendar.read")
    c = TestClient(app)
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "device": True})
        ws.receive_json()
        ws.send_json({"type": "message", "text": "what's on my calendar today"})
        r = ws.receive_json()
        perm = next(p for p in r["pending"] if p["kind"] == "permission")
        ws.send_json({"type": "permission", "id": perm["id"], "mode": "always"})
        r2 = ws.receive_json()
        assert "Lab standup" in r2["reply"]


# ============================================================================ voice
needs_models = pytest.mark.skipif(not os.path.exists(os.path.join(MODELS, "hey_jarvis_v0.1.onnx")),
                                  reason="run scripts/download_models.py")


def _pcm(name):
    with wave.open(os.path.join(HERE, "data", name)) as w:
        assert w.getframerate() == 16000
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


@needs_models
@pytest.mark.parametrize("name,expected", [("hey_jarvis.wav", True), ("hey_jarvis2.wav", True),
                                           ("negative.wav", False), ("speech_negative.wav", False)])
def test_wake_word(name, expected):
    from jarvis.voice.wakeword import WakeWordDetector
    det = WakeWordDetector(MODELS)
    sil = np.zeros(16000, dtype=np.int16)
    audio = np.concatenate([sil, _pcm(name), sil])
    fired = [det.feed(audio[i:i + 512].tobytes(), now=i / 16000) for i in range(0, len(audio), 512)]
    assert any(fired) == expected
    assert sum(fired) <= 1  # one utterance -> one wake


@needs_models
def test_wake_word_over_websocket(tmp_path):
    c = TestClient(make_app(tmp_path, wake=True))
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "device": True})
        assert ws.receive_json()["wake_word"] is True
        audio = np.concatenate([np.zeros(16000, np.int16), _pcm("hey_jarvis.wav"), np.zeros(16000, np.int16)])
        for i in range(0, len(audio), 1280):
            ws.send_bytes(audio[i:i + 1280].tobytes())
        ws.send_json({"type": "ping"})
        msgs = [ws.receive_json(), ws.receive_json()]
        assert any(m["type"] == "wake" for m in msgs)


@pytest.mark.skipif(not os.path.exists(os.path.join(MODELS, "en-us-lessac-low.onnx")), reason="no Piper voice")
def test_server_tts(tmp_path):
    c = TestClient(make_app(tmp_path))
    r = c.post("/api/tts", json={"text": "Good morning, Yogesh."}, headers=H)
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) > 10000
