"""
Language-model provider registry: which providers JARVIS uses, in what order, with which models.

Two sources, merged:
  .env on the server   GROQ_API_KEY, GEMINI_API_KEY, ... (read-only from the app; can be disabled/reordered)
  the app's Settings   keys pasted on the phone. Stored ENCRYPTED in the database (Fernet, JARVIS_DATA_KEY),
                       never sent back to the phone (only the last 4 characters, so you can tell keys apart).

The chain order is the user's choice; the offline rule engine is always last. Changing anything rebuilds the
chain in place, so the next request already uses it. No restart, no SSH.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

from jarvis.llm.providers import (AnthropicLLM, GeminiLLM, LLMUnavailable, OpenAICompatibleLLM, _NOT_CHAT,
                                  pick_model, PREFS)

log = logging.getLogger("jarvis.llm.registry")

# type -> how to talk to it. "kind" picks the client class.
CATALOG: dict[str, dict] = {
    "openai": {"label": "OpenAI", "kind": "openai", "base": "https://api.openai.com/v1",
               "help": "https://platform.openai.com/api-keys", "stt": "whisper-1"},
    "anthropic": {"label": "Anthropic (Claude)", "kind": "anthropic", "help": "https://console.anthropic.com/settings/keys"},
    "groq": {"label": "Groq", "kind": "openai", "base": "https://api.groq.com/openai/v1",
             "help": "https://console.groq.com/keys", "stt": "whisper-large-v3"},
    "gemini": {"label": "Google Gemini", "kind": "gemini", "help": "https://aistudio.google.com/app/apikey"},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "base": "https://openrouter.ai/api/v1",
                   "help": "https://openrouter.ai/keys"},
    "mistral": {"label": "Mistral", "kind": "openai", "base": "https://api.mistral.ai/v1",
                "help": "https://console.mistral.ai/api-keys"},
    "deepseek": {"label": "DeepSeek", "kind": "openai", "base": "https://api.deepseek.com/v1",
                 "help": "https://platform.deepseek.com/api_keys"},
    "together": {"label": "Together AI", "kind": "openai", "base": "https://api.together.xyz/v1",
                 "help": "https://api.together.ai/settings/api-keys"},
    "ollama": {"label": "Ollama (your own computer)", "kind": "openai", "needs_base": True, "no_key": True},
    "custom": {"label": "Other OpenAI-compatible", "kind": "openai", "needs_base": True},
}
ENV_ORDER = ["groq", "gemini", "openrouter", "ollama", "anthropic", "openai"]


class RegistryError(Exception):
    """A user-facing problem with a provider setting (shown in the app as-is)."""


def check_base_url(url: str, allow_private: bool) -> str:
    """HTTPS only (plain HTTP just for local Ollama), never cloud-metadata addresses."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        raise RegistryError("Enter a full address, like https://api.example.com/v1")
    host = u.hostname.lower()
    if host in ("metadata.google.internal", "metadata") or host.startswith("169.254."):
        raise RegistryError("That address is not allowed.")
    private = host in ("localhost",) or host.endswith(".ts.net") or host.endswith(".local")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise RegistryError("That address is not allowed.")
        private = private or ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        pass
    if u.scheme == "http" and not (allow_private and private):
        raise RegistryError("Use https:// (plain http is only allowed for Ollama on your own network).")
    return url.strip().rstrip("/")


def check_key(key: str) -> str:
    key = (key or "").strip().strip('"').strip("'")
    if not (8 <= len(key) <= 400) or re.search(r"\s", key):
        raise RegistryError("That doesn't look like an API key. Paste the whole key, without spaces.")
    return key


def _model_field(v: Optional[str]) -> str:
    v = (v or "auto").strip()
    if not re.fullmatch(r"[\w.:/@+-]{1,120}", v):
        raise RegistryError("Model names can only contain letters, numbers and . : / - _")
    return v


class LLMRegistry:
    def __init__(self, settings, db=None):
        self.settings = settings
        self.db = db

    # ------------------------------------------------------------------ stored state
    def _app_entries(self) -> list[dict]:
        return list(self.db.kv_get("llm_providers", []) or []) if self.db else []

    def _save_app_entries(self, entries: list[dict]) -> None:
        self.db.kv_set("llm_providers", entries)

    def _env_entries(self) -> list[dict]:
        s = self.settings
        found = {
            "groq": (s.groq_api_key, None, s.groq_fast_model, s.groq_smart_model),
            "gemini": (s.gemini_api_key, None, s.gemini_fast_model, s.gemini_smart_model),
            "openrouter": (s.openrouter_api_key, None, s.openrouter_model, s.openrouter_model),
            "ollama": ("", s.ollama_url.rstrip("/") + "/v1" if s.ollama_url else None, s.ollama_model, s.ollama_model),
            "anthropic": (s.anthropic_api_key, None, "auto", "auto"),
            "openai": (getattr(s, "openai_api_key", ""), None, "auto", "auto"),
        }
        out = []
        for t in ENV_ORDER:
            key, base, mf, ms = found[t]
            if key or (t == "ollama" and base):
                out.append({"id": f"env-{t}", "type": t, "base_url": base, "model_fast": mf or "auto",
                            "model_smart": ms or "auto", "source": "env"})
        return out

    def entries(self) -> list[dict]:
        """All providers in the user's chosen order, each with 'enabled'."""
        disabled = set(self.db.kv_get("llm_disabled", []) or []) if self.db else set()
        overrides = self.db.kv_get("llm_env_models", {}) or {} if self.db else {}
        allp = []
        for e in self._env_entries():
            e.update(overrides.get(e["id"], {}))
            allp.append(e)
        allp += self._app_entries()
        order = self.db.kv_get("llm_order", []) if self.db else []
        rank = {pid: i for i, pid in enumerate(order or [])}
        allp = [e for _, e in sorted(enumerate(allp), key=lambda ie: (rank.get(ie[1]["id"], 10_000), ie[0]))]
        for e in allp:
            e["enabled"] = e["id"] not in disabled
        return allp

    def _key_for(self, e: dict) -> str:
        if CATALOG[e["type"]].get("no_key"):
            return ""
        if e.get("source") == "env":
            s = self.settings
            return {"groq": s.groq_api_key, "gemini": s.gemini_api_key, "openrouter": s.openrouter_api_key,
                    "anthropic": s.anthropic_api_key, "openai": getattr(s, "openai_api_key", "")}.get(e["type"], "")
        return (self.db.secret_get(f"llm_key:{e['id']}") or "") if self.db else ""

    # ------------------------------------------------------------------ building clients
    def client(self, e: dict, tier: str, key: Optional[str] = None):
        key = self._key_for(e) if key is None else key
        cat = CATALOG[e["type"]]
        model = e.get(f"model_{tier}") or "auto"
        if cat["kind"] == "gemini":
            return GeminiLLM(key, model, tier=tier)
        if cat["kind"] == "anthropic":
            return AnthropicLLM(key, model, tier=tier)
        base = e.get("base_url") or cat.get("base")
        family = e["type"] if e["type"] in PREFS else "custom"
        extra = {"X-Title": "JARVIS personal assistant"} if e["type"] == "openrouter" else None
        return OpenAICompatibleLLM(e["type"], base, key, model, family=family, tier=tier, extra_headers=extra,
                                   timeout=120 if e["type"] == "ollama" else 45)

    def chain(self, tier: str) -> list:
        from jarvis.llm.rule_llm import RuleLLM

        out = []
        if self.settings.llm_mode != "offline":
            for e in self.entries():
                if not e["enabled"]:
                    continue
                try:
                    c = self.client(e, tier)
                    c.registry_id = e["id"]
                    out.append(c)
                except Exception as ex:  # a broken entry must never take the whole chain down
                    log.warning("provider %s skipped: %s", e["id"], ex)
        out.append(RuleLLM())
        return out

    def apply(self, svc) -> None:
        """Rebuild the live chains in place."""
        svc.fast_llm.replace(self.chain("fast"))
        svc.smart_llm.replace(self.chain("smart"))

    # ------------------------------------------------------------------ testing
    def test(self, e: dict, key: Optional[str] = None) -> dict:
        """Live check of both tiers. Returns the models actually used; raises RegistryError with a clear reason."""
        used = {}
        for tier in ("fast", "smart"):
            c = self.client(e, tier, key)
            try:
                out = c.complete("Reply with the single word OK.", max_tokens=20)
            except LLMUnavailable as ex:
                msg = str(ex)
                if "key rejected" in msg or "401" in msg or "403" in msg:
                    raise RegistryError(f"{CATALOG[e['type']]['label']} rejected the key. Check you copied all of it.")
                if "can't list models" in msg:
                    raise RegistryError(f"Couldn't read the model list from {CATALOG[e['type']]['label']}: {msg[-120:]}")
                raise RegistryError(f"{CATALOG[e['type']]['label']} didn't answer: {msg[-160:]}")
            except Exception as ex:
                if "rate limited" in str(ex):
                    raise RegistryError("The key works but is rate-limited right now. Try again in a minute.")
                raise RegistryError(f"Test failed: {str(ex)[-160:]}")
            if not out.strip():
                raise RegistryError("The model answered with nothing. Pick a different model.")
            used[tier] = c.model
        return used

    def models(self, pid: str) -> list[str]:
        e = self._get(pid)
        c = self.client(e, "smart")
        if not hasattr(c, "list_models"):
            return []
        try:
            ids = c.list_models()
        except LLMUnavailable as ex:
            raise RegistryError(str(ex)[-160:])
        return sorted({m for m in ids if not _NOT_CHAT.search(m)})

    # ------------------------------------------------------------------ edits (all audited)
    def _get(self, pid: str) -> dict:
        for e in self.entries():
            if e["id"] == pid:
                return e
        raise RegistryError("No such provider.")

    def add(self, ptype: str, api_key: str = "", base_url: str = "", model_fast: str = "auto",
            model_smart: str = "auto") -> dict:
        if ptype not in CATALOG:
            raise RegistryError("Unknown provider type.")
        cat = CATALOG[ptype]
        key = "" if cat.get("no_key") else check_key(api_key)
        base = check_base_url(base_url, allow_private=ptype == "ollama") if cat.get("needs_base") else None
        if ptype == "ollama" and base and not base.endswith("/v1"):
            base += "/v1"
        e = {"id": f"{ptype}-{secrets.token_hex(3)}", "type": ptype, "base_url": base,
             "model_fast": _model_field(model_fast), "model_smart": _model_field(model_smart),
             "source": "app", "added": time.time()}
        used = self.test(e, key)                       # never save a key that doesn't work
        if key:
            self.db.secret_set(f"llm_key:{e['id']}", key)
        entries = self._app_entries() + [e]
        self._save_app_entries(entries)
        order = [x["id"] for x in self.entries() if x["id"] != e["id"]]
        self.db.kv_set("llm_order", [e["id"]] + order)   # a newly added provider becomes the first choice
        self.db.audit("user", "llm.add", {"type": ptype, "id": e["id"]}, "done")
        return {"id": e["id"], "models": used}

    def update(self, pid: str, enabled: Optional[bool] = None, model_fast: Optional[str] = None,
               model_smart: Optional[str] = None) -> dict:
        e = self._get(pid)
        if enabled is not None:
            disabled = set(self.db.kv_get("llm_disabled", []) or [])
            (disabled.discard if enabled else disabled.add)(pid)
            self.db.kv_set("llm_disabled", sorted(disabled))
        changes = {}
        if model_fast is not None:
            changes["model_fast"] = _model_field(model_fast)
        if model_smart is not None:
            changes["model_smart"] = _model_field(model_smart)
        used = None
        if changes:
            trial = {**e, **changes}
            used = self.test(trial)                    # don't switch to a model that doesn't answer
            if e["source"] == "env":
                ov = self.db.kv_get("llm_env_models", {}) or {}
                ov[pid] = {**ov.get(pid, {}), **changes}
                self.db.kv_set("llm_env_models", ov)
            else:
                entries = self._app_entries()
                for x in entries:
                    if x["id"] == pid:
                        x.update(changes)
                self._save_app_entries(entries)
        self.db.audit("user", "llm.update", {"id": pid, "enabled": enabled, **changes}, "done")
        return {"id": pid, "models": used}

    def remove(self, pid: str) -> None:
        e = self._get(pid)
        if e["source"] == "env":
            raise RegistryError("This key comes from the server's .env file. Disable it here, or remove it from .env.")
        self._save_app_entries([x for x in self._app_entries() if x["id"] != pid])
        self.db.execute("DELETE FROM secrets WHERE name=?", (f"llm_key:{pid}",))
        self.db.kv_set("llm_order", [x for x in (self.db.kv_get("llm_order", []) or []) if x != pid])
        self.db.audit("user", "llm.remove", {"id": pid}, "done")

    def reorder(self, ids: list[str]) -> None:
        known = {e["id"] for e in self.entries()}
        if set(ids) - known:
            raise RegistryError("Unknown provider in the new order.")
        self.db.kv_set("llm_order", list(ids) + [i for i in known if i not in ids])
        self.db.audit("user", "llm.reorder", {"order": ids}, "done")

    # ------------------------------------------------------------------ speech to text
    def stt(self):
        """First enabled provider that offers Whisper (Groq is free; OpenAI is paid)."""
        from jarvis.voice.speech import WhisperAPISTT

        if self.settings.llm_mode == "offline":
            return None
        for e in self.entries():
            model = CATALOG[e["type"]].get("stt")
            if e["enabled"] and model:
                key = self._key_for(e)
                if key:
                    return WhisperAPISTT(CATALOG[e["type"]]["base"], key, model, provider=e["type"])
        return None

    # ------------------------------------------------------------------ for the app
    def describe(self, live=None) -> list[dict]:
        live_by_id = {}
        for chain in live or []:
            for c in chain.providers:
                rid = getattr(c, "registry_id", None)
                if rid:
                    live_by_id.setdefault(rid, {})[c.tier] = c.model
        errors = {}
        for chain in live or []:
            for name, msg in chain.last_errors.items():
                errors[name.split(":")[0]] = msg
        out = []
        for e in self.entries():
            key = self._key_for(e)
            cat = CATALOG[e["type"]]
            m = live_by_id.get(e["id"], {})
            out.append({
                "id": e["id"], "type": e["type"], "label": cat["label"], "source": e["source"],
                "enabled": e["enabled"], "key_hint": f"…{key[-4:]}" if len(key) >= 8 else "",
                "base_url": e.get("base_url") if cat.get("needs_base") else None,
                "model_fast": m.get("fast") or e.get("model_fast", "auto"),
                "model_smart": m.get("smart") or e.get("model_smart", "auto"),
                "configured_fast": e.get("model_fast", "auto"), "configured_smart": e.get("model_smart", "auto"),
                "last_error": errors.get(e["type"]), "stt": bool(cat.get("stt")),
            })
        return out

    @staticmethod
    def catalog() -> list[dict]:
        return [{"type": t, "label": c["label"], "help": c.get("help"), "needs_base": bool(c.get("needs_base")),
                 "no_key": bool(c.get("no_key"))} for t, c in CATALOG.items()]
