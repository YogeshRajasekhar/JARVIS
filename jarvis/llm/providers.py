"""
Free-tier LLM providers behind one tiny interface, plus a fallback chain.

Interface (compatible with v1's `src.llm.client.LLMClient` protocol):

    complete(prompt, system=None, *, json=False, temperature=0.0, max_tokens=800,
             task=None, history=None) -> str

- `task` is a hint used only by the offline RuleLLM (cloud providers ignore it).
- `history` is an optional list of {"role": "user"|"assistant", "content": str}.

Design choices for free models:
- Temperature defaults to 0: routing and extraction must be repeatable.
- JSON mode is requested natively where the provider supports it, and silently dropped
  (prompt still demands JSON) if the provider rejects it for a given model.
- Rate limits are the normal state of a free tier, so a 429 is not an error to bubble up:
  the provider is put on cooldown and the FallbackLLM moves to the next one.
- A small cache avoids paying twice for identical deterministic calls.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Protocol, runtime_checkable

import httpx

log = logging.getLogger("jarvis.llm")


class LLMError(Exception):
    pass


class LLMRateLimited(LLMError):
    def __init__(self, msg: str, retry_after: float = 30.0):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMUnavailable(LLMError):
    """Network error, 5xx, auth error, missing key. Try the next provider."""


@runtime_checkable
class LLM(Protocol):
    name: str

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str: ...


def _messages(system: Optional[str], prompt: str, history: Optional[list[dict]]) -> list[dict]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in history or []:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": prompt})
    return msgs


class OpenAICompatibleLLM:
    """Groq, OpenRouter and Ollama all speak the OpenAI chat-completions dialect."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float = 30.0,
                 extra_headers: Optional[dict] = None, transport: Optional[httpx.BaseTransport] = None):
        self.name = name
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(extra_headers or {})
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)
        self._json_mode_supported = True

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        body = {
            "model": self.model,
            "messages": _messages(system, prompt, history),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json and self._json_mode_supported:
            body["response_format"] = {"type": "json_object"}
        try:
            r = self._client.post(self._url, json=body)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"{self.name}: network error {e}") from e

        if r.status_code == 400 and "response_format" in body:
            # Some models reject JSON mode. Remember that and retry once without it.
            self._json_mode_supported = False
            body.pop("response_format")
            r = self._client.post(self._url, json=body)
        if r.status_code == 429:
            retry = float(r.headers.get("retry-after", "30") or 30)
            raise LLMRateLimited(f"{self.name}: rate limited", retry_after=retry)
        if r.status_code >= 400:
            raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMUnavailable(f"{self.name}: malformed response {r.text[:200]}") from e


class GeminiLLM:
    """Google AI Studio (Gemini API) REST client. Free tier: Flash models."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.name = f"gemini:{model}"
        self.model = model
        self._client = httpx.Client(
            timeout=timeout,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            transport=transport,
        )

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        contents = []
        for m in history or []:
            role = "model" if m.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        body: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json:
            body["generationConfig"]["responseMimeType"] = "application/json"
        try:
            r = self._client.post(f"{self.BASE}/{self.model}:generateContent", json=body)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"{self.name}: network error {e}") from e
        if r.status_code == 429:
            raise LLMRateLimited(f"{self.name}: rate limited", retry_after=60)
        if r.status_code >= 400:
            raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError) as e:
            raise LLMUnavailable(f"{self.name}: malformed/blocked response {r.text[:200]}") from e


class ClaudeLLM:
    """Optional: wraps v1's Claude client (used only if ANTHROPIC_API_KEY is set)."""

    def __init__(self, api_key: str, model: str):
        from src.llm.client import ClaudeLLMClient  # lazy: anthropic SDK only needed here

        self.name = f"claude:{model}"
        self._inner = ClaudeLLMClient(model=model, api_key=api_key)

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        from src.llm.client import LLMError as V1Error

        try:
            return self._inner.complete(prompt, system=system, max_tokens=max_tokens, temperature=temperature)
        except V1Error as e:
            raise LLMUnavailable(str(e)) from e


class FallbackLLM:
    """
    Tries providers in order. A rate-limited provider is skipped until its cooldown expires;
    a failing provider is skipped for a short cooldown. The last provider is normally the
    offline RuleLLM, so the assistant degrades to "simpler" rather than "broken".
    """

    def __init__(self, providers: list, cache_size: int = 256, name: str = "fallback"):
        if not providers:
            raise ValueError("FallbackLLM needs at least one provider")
        self.providers = providers
        self.name = name
        self._cooldown_until: dict[str, float] = {}
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self.last_provider: Optional[str] = None

    def _key(self, prompt, system, kwargs) -> Optional[str]:
        if kwargs.get("temperature", 0.0) != 0.0 or kwargs.get("history"):
            return None
        raw = _json.dumps([prompt, system, kwargs.get("json"), kwargs.get("task")], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        key = self._key(prompt, system, kwargs)
        if key is not None:
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return self._cache[key]

        errors = []
        now = time.monotonic()
        for p in self.providers:
            if self._cooldown_until.get(p.name, 0) > now:
                errors.append(f"{p.name}: cooling down")
                continue
            try:
                out = p.complete(prompt, system=system, **kwargs)
                self.last_provider = p.name
                if key is not None:
                    with self._lock:
                        self._cache[key] = out
                        if len(self._cache) > self._cache_size:
                            self._cache.popitem(last=False)
                return out
            except LLMRateLimited as e:
                self._cooldown_until[p.name] = time.monotonic() + min(e.retry_after, 300)
                errors.append(str(e))
                log.warning("LLM %s rate limited, falling back", p.name)
            except LLMUnavailable as e:
                self._cooldown_until[p.name] = time.monotonic() + 20
                errors.append(str(e))
                log.warning("LLM %s unavailable: %s", p.name, e)
        raise LLMUnavailable("All LLM providers failed: " + " | ".join(errors))


def build_llms(settings) -> tuple[FallbackLLM, FallbackLLM]:
    """Returns (fast, smart) fallback chains from whatever keys are configured."""
    from jarvis.llm.rule_llm import RuleLLM

    fast, smart = [], []
    if settings.llm_mode != "offline":
        if settings.groq_api_key:
            g = "https://api.groq.com/openai/v1"
            fast.append(OpenAICompatibleLLM(f"groq:{settings.groq_fast_model}", g, settings.groq_api_key, settings.groq_fast_model))
            smart.append(OpenAICompatibleLLM(f"groq:{settings.groq_smart_model}", g, settings.groq_api_key, settings.groq_smart_model))
        if settings.gemini_api_key:
            fast.append(GeminiLLM(settings.gemini_api_key, settings.gemini_fast_model))
            smart.append(GeminiLLM(settings.gemini_api_key, settings.gemini_smart_model))
        if settings.openrouter_api_key:
            o = OpenAICompatibleLLM(
                f"openrouter:{settings.openrouter_model}", "https://openrouter.ai/api/v1",
                settings.openrouter_api_key, settings.openrouter_model,
                extra_headers={"X-Title": "JARVIS personal assistant"},
            )
            fast.append(o)
            smart.append(o)
        if settings.ollama_url:
            ol = OpenAICompatibleLLM(f"ollama:{settings.ollama_model}", settings.ollama_url.rstrip("/") + "/v1", "", settings.ollama_model, timeout=120)
            fast.append(ol)
            smart.append(ol)
        if settings.anthropic_api_key:
            try:
                fast.append(ClaudeLLM(settings.anthropic_api_key, "claude-haiku-4-5-20251001"))
                smart.append(ClaudeLLM(settings.anthropic_api_key, "claude-haiku-4-5-20251001"))
            except Exception as e:  # anthropic SDK missing etc.
                log.warning("Claude provider disabled: %s", e)
    rule = RuleLLM()
    fast.append(rule)
    smart.append(rule)
    return FallbackLLM(fast, name="fast"), FallbackLLM(smart, name="smart")
