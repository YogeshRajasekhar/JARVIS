"""Model providers managed from the app: add/test/reorder/disable/remove, key secrecy, live switching."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import NOW, offline_settings
from jarvis.core.storage import Database
from jarvis.llm.providers import AnthropicLLM, FallbackLLM, LLMUnavailable, OpenAICompatibleLLM
from jarvis.llm.registry import LLMRegistry, RegistryError, check_base_url

TOKEN = "prov-token-1234567890"
H = {"Authorization": f"Bearer {TOKEN}"}
GOOD_KEY = "sk-test-GOODKEY-abcdef123456"


class FakeLLM:
    """Stands in for a cloud client. Fails unless built with the good key."""

    def __init__(self, entry, tier, key):
        self.family, self.tier, self.key = entry["type"], tier, key
        self.model = f"{entry['type']}-{tier}-model" if entry.get(f"model_{tier}", "auto") == "auto" else entry[f"model_{tier}"]
        self.name = f"{self.family}:{self.model}"

    def complete(self, prompt, system=None, **kw):
        if self.key != GOOD_KEY and self.family not in ("ollama",):
            raise LLMUnavailable(f"{self.name}: key rejected (HTTP 401)")
        return f"answer from {self.model}"

    def list_models(self):
        return ["m-small", "m-large", "whisper-1", "text-embedding-3"]


@pytest.fixture
def fake_clients(monkeypatch):
    real = LLMRegistry.client

    def client(self, e, tier, key=None):
        key = self._key_for(e) if key is None else key
        return FakeLLM(e, tier, key)

    monkeypatch.setattr(LLMRegistry, "client", client)
    yield
    monkeypatch.setattr(LLMRegistry, "client", real)


def online_settings(**keys):
    s = offline_settings()
    s.llm_mode = "auto"
    for k, v in keys.items():
        setattr(s, k, v)
    return s


def test_env_providers_listed_in_default_order(fake_clients):
    reg = LLMRegistry(online_settings(groq_api_key=GOOD_KEY, gemini_api_key=GOOD_KEY), Database())
    assert [e["id"] for e in reg.entries()] == ["env-groq", "env-gemini"]
    chain = reg.chain("fast")
    assert [c.family for c in chain[:-1]] == ["groq", "gemini"] and chain[-1].name == "rule-offline"


def test_add_tests_key_first_and_never_saves_a_bad_one(fake_clients):
    db = Database()
    reg = LLMRegistry(online_settings(groq_api_key=GOOD_KEY), db)
    with pytest.raises(RegistryError, match="rejected the key"):
        reg.add("openai", "sk-wrong-key-000000000")
    assert db.kv_get("llm_providers", []) == [] and db.query("SELECT * FROM secrets") == []
    out = reg.add("openai", GOOD_KEY)
    assert out["models"] == {"fast": "openai-fast-model", "smart": "openai-smart-model"}
    assert reg.entries()[0]["type"] == "openai"                      # new provider goes first
    raw = db.query("SELECT token FROM secrets")[0]["token"]
    assert GOOD_KEY.encode() not in raw                              # encrypted at rest
    desc = reg.describe()
    assert desc[0]["key_hint"] == "…3456" and GOOD_KEY not in json.dumps(desc)


def test_disable_reorder_remove(fake_clients):
    db = Database()
    reg = LLMRegistry(online_settings(groq_api_key=GOOD_KEY, gemini_api_key=GOOD_KEY), db)
    pid = reg.add("openai", GOOD_KEY)["id"]
    reg.update("env-groq", enabled=False)
    assert [c.family for c in reg.chain("smart")[:-1]] == ["openai", "gemini"]
    reg.reorder(["env-gemini", pid, "env-groq"])
    assert [c.family for c in reg.chain("smart")[:-1]] == ["gemini", "openai"]
    with pytest.raises(RegistryError, match=".env"):
        reg.remove("env-groq")
    reg.remove(pid)
    assert all(e["id"] != pid for e in reg.entries())
    assert db.secret_get(f"llm_key:{pid}") is None


def test_choose_model_is_tested_before_switching(fake_clients):
    db = Database()
    reg = LLMRegistry(online_settings(groq_api_key=GOOD_KEY), db)
    reg.update("env-groq", model_smart="m-large")
    assert reg.entries()[0]["model_smart"] == "m-large"
    with pytest.raises(RegistryError):
        reg.update("env-groq", model_smart="bad name with spaces")
    assert reg.models("env-groq") == ["m-large", "m-small"]          # non-chat models filtered out


def test_offline_mode_ignores_providers(fake_clients):
    s = online_settings(groq_api_key=GOOD_KEY)
    s.llm_mode = "offline"
    assert [c.name for c in LLMRegistry(s, Database()).chain("fast")] == ["rule-offline"]


@pytest.mark.parametrize("url,ok", [
    ("https://api.example.com/v1", True), ("http://api.example.com/v1", False),
    ("http://192.168.1.20:11434", True), ("http://100.101.102.103:11434", True),
    ("http://169.254.169.254/latest", False), ("https://metadata.google.internal/x", False),
    ("ftp://x", False), ("not a url", False),
])
def test_base_url_rules(url, ok):
    if ok:
        assert check_base_url(url, allow_private=True)
    else:
        with pytest.raises(RegistryError):
            check_base_url(url, allow_private=True)


def test_stt_picks_groq_then_openai(fake_clients):
    db = Database()
    reg = LLMRegistry(online_settings(), db)
    assert reg.stt() is None
    reg.add("openai", GOOD_KEY)
    s = reg.stt()
    assert s.provider == "openai" and s.model == "whisper-1" and "api.openai.com" in s.base


# ============================================================================ through the server
def make_app(**keys):
    from jarvis.integrations.device import SimulatedDevice
    from jarvis.server.app import create_app
    from jarvis.services import build_services

    s = online_settings(**keys)
    s.device_token = TOKEN
    svc = build_services(s, db=Database(), clock=lambda: NOW, seed=True, device=SimulatedDevice())
    return create_app(s, svc=svc, enable_wakeword=False)


def test_api_add_switches_live_and_never_returns_key(fake_clients):
    app = make_app()
    svc = app.state.svc
    c = TestClient(app)
    assert c.get("/api/llm", headers=H).json()["providers"] == []
    bad = c.post("/api/llm/providers", json={"type": "openai", "api_key": "sk-nope-nope-nope"}, headers=H)
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    r = c.post("/api/llm/providers", json={"type": "openai", "api_key": GOOD_KEY}, headers=H)
    assert r.status_code == 200 and GOOD_KEY not in r.text
    assert r.json()["providers"][0]["model_smart"] == "openai-smart-model"
    # The live chain the agents use was swapped in place: no restart needed.
    assert svc.smart_llm.complete("hi") == "answer from openai-smart-model"
    assert GOOD_KEY not in c.get("/api/llm", headers=H).text
    assert c.get("/api/audit", headers=H).json()["audit"][0]["tool"].startswith("llm.")


def test_api_requires_auth_and_respects_lockdown(fake_clients):
    c = TestClient(make_app())
    assert c.get("/api/llm").status_code == 401
    c.post("/api/lockdown", headers=H)
    r = c.post("/api/llm/providers", json={"type": "openai", "api_key": GOOD_KEY}, headers=H)
    assert r.status_code == 423


def test_api_validation(fake_clients):
    c = TestClient(make_app())
    assert c.post("/api/llm/providers", json={"type": "skynet", "api_key": GOOD_KEY}, headers=H).status_code == 400
    assert c.post("/api/llm/providers", json={"type": "openai", "api_key": "x" * 500}, headers=H).status_code == 422
    r = c.post("/api/llm/providers", json={"type": "custom", "api_key": GOOD_KEY, "base_url": "http://169.254.169.254"}, headers=H)
    assert r.status_code == 400


# ============================================================================ provider quirks
def _t(h):
    return httpx.MockTransport(h)


def test_openai_new_models_param_quirks():
    """Newer OpenAI models reject max_tokens and custom temperature: adapt once, then remember."""
    bodies = []

    def h(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "max_tokens" in b:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        if "temperature" in b:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0 with this model."}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    llm = OpenAICompatibleLLM("openai", "https://x/v1", "k", "gpt-test", transport=_t(h), family="openai")
    assert llm.complete("hi") == "hello"
    assert "max_completion_tokens" in bodies[-1] and "temperature" not in bodies[-1]
    n = len(bodies)
    assert llm.complete("again") == "hello" and len(bodies) == n + 1      # learned: one request now


def test_openai_auto_prefers_alias_over_snapshot():
    from jarvis.llm.providers import PREFS, pick_model
    ids = ["gpt-4o-mini", "gpt-4o-mini-2024-07-18", "gpt-4o", "dall-e-3", "whisper-1", "gpt-4o-realtime-preview",
           "text-embedding-3-small", "gpt-4o-mini-tts"]
    assert pick_model(ids, PREFS["openai"]["fast"]) == "gpt-4o-mini"
    assert pick_model(ids, PREFS["openai"]["smart"]) == "gpt-4o"


def test_anthropic_client():
    def h(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-x"}, {"id": "claude-haiku-x"}]})
        b = json.loads(req.content)
        assert req.headers["x-api-key"] == "k" and b["model"] == "claude-haiku-x" and b["system"] == "be brief"
        assert b["messages"][0]["role"] == "user"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi there"}]})

    llm = AnthropicLLM("k", "auto", tier="fast", transport=_t(h))
    assert llm.complete("hello", system="be brief", history=[{"role": "assistant", "content": "x"}]) == "hi there"


def test_fallback_replace_clears_state():
    class Boom:
        name = "boom"

        def complete(self, *a, **k):
            raise LLMUnavailable("down")

    class Ok:
        name = "ok"

        def complete(self, *a, **k):
            return "fine"

    f = FallbackLLM([Boom(), Ok()])
    f.complete("x")
    assert f.last_errors
    f.replace([Ok()])
    assert f.last_errors == {} and f.complete("x") == "fine"
