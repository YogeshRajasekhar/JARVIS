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
import re
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


# Free-tier model names change every few months (and an account can be restricted to some of them).
# So providers can be given "auto" (or a name that later disappears): they then ask the provider which
# models this key can use and pick the best match for the tier below.
_NOT_CHAT = re.compile(r"whisper|tts|orpheus|guard|embed|imagen|image|veo|audio|live|transcri|moderation|aqa|learnlm|"
                       r"computer-use|robotics|native-audio|dall-e|davinci|babbage|realtime|search|codex|instruct|"
                       r"sora|ocr|rerank|vision-preview|-speech|playai", re.I)
PREFS = {
    "groq": {"fast": [r"^llama-3\.1-8b-instant$", r"gpt-oss-20b", r"llama.*8b", r"qwen", r"gpt-oss", r"llama"],
             "smart": [r"^llama-3\.3-70b-versatile$", r"gpt-oss-120b", r"llama.*70b", r"qwen", r"gpt-oss", r"llama"]},
    "gemini": {"fast": [r"^gemini-[\d.]+-flash-lite$", r"^gemini-[\d.]+-flash$", r"flash", r"gemma"],
               "smart": [r"^gemini-[\d.]+-flash$", r"^gemini-[\d.]+-flash-[\w-]*$", r"flash", r"gemma"]},
    "openrouter": {"fast": [r"llama.*:free$", r"qwen.*:free$", r":free$"], "smart": [r"llama.*70b.*:free$", r"gpt-oss-120b:free$", r":free$"]},
    # Paid providers: a small model for routing/extraction, the main model for conversation/planning.
    "openai": {"fast": [r"^gpt-[\d.]+o?-mini$", r"^gpt-[\d.]+o?-nano$", r"^gpt-[\d.o]+-mini", r"mini"],
               "smart": [r"^gpt-[\d.]+o?$", r"^gpt-[\d.]+o?-chat", r"^gpt-[\d.o]+$", r"^gpt-"]},
    "anthropic": {"fast": [r"haiku"], "smart": [r"sonnet", r"opus"]},
    "mistral": {"fast": [r"^mistral-small-latest$", r"small", r"ministral"], "smart": [r"^mistral-large-latest$", r"large", r"medium"]},
    "deepseek": {"fast": [r"^deepseek-chat$"], "smart": [r"^deepseek-chat$"]},
    "together": {"fast": [r"llama.*8b.*instruct", r"8b"], "smart": [r"llama.*70b.*instruct", r"70b"]},
}

_DATE = re.compile(r"-?\d{4}-\d{2}-\d{2}|-\d{8}|-\d{4}(?=$|-)")


def _rank(model_id: str) -> tuple:
    """Newer version first; stable aliases before preview/experimental and before dated snapshots."""
    plain = _DATE.sub("", model_id)
    nums = tuple(float(x) for x in re.findall(r"\d+(?:\.\d+)?", plain)[:3]) or (0.0,)
    unstable = 1 if re.search(r"preview|exp|beta|alpha", model_id, re.I) else 0
    dated = 1 if _DATE.search(model_id) else 0
    return (-unstable, -dated, nums)


def pick_model(available: list[str], prefs: list[str], avoid: Optional[set] = None) -> Optional[str]:
    cands = [m for m in available if not _NOT_CHAT.search(m) and m not in (avoid or set())]
    for pat in prefs:
        hits = [m for m in cands if re.search(pat, m, re.I)]
        if hits:
            return max(hits, key=_rank)
    return max(cands, key=_rank) if cands else None


def _is_reasoning(model: Optional[str]) -> bool:
    return bool(model and re.search(r"gpt-oss|deepseek-r|qwq|reasoning|-r1\b|magistral|^o[1-9](-|$)|^gpt-5", model, re.I))


def _model_missing(status: int, text: str) -> bool:
    if status == 404:
        return True
    return status == 400 and re.search(r"model[^.]{0,80}(does not exist|not found|decommissioned|deprecated|no longer)",
                                       text, re.I) is not None


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
                 extra_headers: Optional[dict] = None, transport: Optional[httpx.BaseTransport] = None,
                 family: Optional[str] = None, tier: str = "fast"):
        self.family = family or name.split(":")[0]
        self.tier = tier
        self.model = None if not model or model == "auto" else model
        self.name = f"{self.family}:{self.model or 'auto'}"
        self._base = base_url.rstrip("/")
        self._url = self._base + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(extra_headers or {})
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)
        self._json_mode_supported = True
        self._reasoning_params = True
        self._temperature_ok = True
        self._max_key = "max_tokens"
        self._tried: set = set()

    def list_models(self) -> list[str]:
        r = self._client.get(self._base + "/models")
        if r.status_code >= 400:
            raise LLMUnavailable(f"{self.family}: can't list models (HTTP {r.status_code}) {r.text[:120]}")
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]

    def resolve(self) -> str:
        prefs = PREFS.get(self.family, {}).get(self.tier, [])
        chosen = pick_model(self.list_models(), prefs, avoid=self._tried)
        if not chosen:
            raise LLMUnavailable(f"{self.family}: this key has no usable chat models")
        if self.model and self.model != chosen:
            log.warning("%s: model %s unavailable, switching to %s", self.family, self.model, chosen)
        self.model, self.name = chosen, f"{self.family}:{chosen}"
        return chosen

    def _body(self, prompt, system, history, json, temperature, max_tokens) -> dict:
        body = {"model": self.model, "messages": _messages(system, prompt, history)}
        budget = max_tokens + 1024 if _is_reasoning(self.model) else max_tokens
        body[self._max_key] = budget          # reasoning models: thinking counts against the budget
        if self._temperature_ok:
            body["temperature"] = temperature
        if _is_reasoning(self.model) and self._reasoning_params:
            body["reasoning_effort"] = "low"
            if self.family == "groq":
                body["reasoning_format"] = "hidden"
        if json and self._json_mode_supported:
            body["response_format"] = {"type": "json_object"}
        return body

    def _adapt(self, text: str, body: dict) -> bool:
        """Learn from a 400 which optional parameter this model rejects. True if something changed."""
        t = text.lower()
        if "max_tokens" in t and "max_completion_tokens" in t and self._max_key == "max_tokens":
            self._max_key = "max_completion_tokens"       # newer OpenAI models
            return True
        if "temperature" in t and self._temperature_ok and "temperature" in body:
            self._temperature_ok = False                  # reasoning models only allow the default
            return True
        if ("reasoning" in t) and self._reasoning_params and "reasoning_effort" in body:
            self._reasoning_params = False
            return True
        if ("response_format" in t or "json" in t) and self._json_mode_supported and "response_format" in body:
            self._json_mode_supported = False
            return True
        return False

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        if self.model is None:
            self.resolve()
        switched = False
        for _ in range(6):
            body = self._body(prompt, system, history, json, temperature, max_tokens)
            try:
                r = self._client.post(self._url, json=body)
            except httpx.HTTPError as e:
                raise LLMUnavailable(f"{self.name}: network error {e}") from e
            if _model_missing(r.status_code, r.text) and not switched:
                switched = True
                self._tried.add(self.model)
                self.resolve()           # model retired or not allowed for this key: pick another
                continue
            if r.status_code == 400 and self._adapt(r.text, body):
                continue
            if r.status_code == 429:
                retry = float(r.headers.get("retry-after", "30") or 30)
                raise LLMRateLimited(f"{self.name}: rate limited", retry_after=retry)
            if r.status_code in (401, 403):
                raise LLMUnavailable(f"{self.name}: key rejected (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
            try:
                choice = r.json()["choices"][0]
                content = choice["message"].get("content") or ""
            except (KeyError, IndexError, ValueError, TypeError) as e:
                raise LLMUnavailable(f"{self.name}: malformed response {r.text[:200]}") from e
            if not content.strip():
                # An empty answer is a failure, not a result: let the fallback chain try the next provider.
                raise LLMUnavailable(f"{self.name}: empty reply (finish_reason={choice.get('finish_reason')})")
            return content
        raise LLMUnavailable(f"{self.name}: request kept failing")


class GeminiLLM:
    """Google AI Studio (Gemini API) REST client. Free tier: Flash models."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str, timeout: float = 45.0,
                 transport: Optional[httpx.BaseTransport] = None, tier: str = "fast"):
        self.family, self.tier = "gemini", tier
        self._thinking_params = True
        self.model = None if not model or model == "auto" else model
        self.name = f"gemini:{self.model or 'auto'}"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            transport=transport,
        )
        self._tried: set = set()

    def list_models(self) -> list[str]:
        out, token = [], None
        for _ in range(5):
            params = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            r = self._client.get(self.BASE, params=params)
            if r.status_code >= 400:
                raise LLMUnavailable(f"gemini: can't list models (HTTP {r.status_code}) {r.text[:160]}")
            j = r.json()
            out += [m["name"].split("/", 1)[-1] for m in j.get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
            token = j.get("nextPageToken")
            if not token:
                break
        return out

    def resolve(self) -> str:
        chosen = pick_model(self.list_models(), PREFS["gemini"][self.tier], avoid=self._tried)
        if not chosen:
            raise LLMUnavailable("gemini: this key has no usable text models")
        if self.model and self.model != chosen:
            log.warning("gemini: model %s unavailable, switching to %s", self.model, chosen)
        self.model, self.name = chosen, f"gemini:{chosen}"
        return chosen

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        if self.model is None:
            self.resolve()
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
        for attempt in range(2):
            gen = body["generationConfig"]
            gen.pop("thinkingConfig", None)
            if self._thinking_params:
                # Gemini 2.5+/3.x think before answering by default, which is slow and eats the token budget.
                # A personal assistant's short tasks don't need it.
                if re.match(r"gemini-2\.5", self.model or ""):
                    gen["thinkingConfig"] = {"thinkingBudget": 0 if "pro" not in self.model else 128}
                elif re.match(r"gemini-[3-9]", self.model or ""):
                    gen["thinkingConfig"] = {"thinkingLevel": "low"}
            try:
                r = self._client.post(f"{self.BASE}/{self.model}:generateContent", json=body)
            except httpx.HTTPError as e:
                raise LLMUnavailable(f"{self.name}: network error {e}") from e
            if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in gen:
                self._thinking_params = False   # this model doesn't accept the option: retry without it
                gen.pop("thinkingConfig", None)
                r = self._client.post(f"{self.BASE}/{self.model}:generateContent", json=body)
            if _model_missing(r.status_code, r.text) and attempt == 0:
                self._tried.add(self.model)
                self.resolve()
                continue
            if r.status_code == 429:
                raise LLMRateLimited(f"{self.name}: rate limited", retry_after=60)
            if r.status_code >= 400:
                raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            except (KeyError, IndexError, ValueError) as e:
                raise LLMUnavailable(f"{self.name}: malformed/blocked response {r.text[:200]}") from e
            if not text.strip():
                raise LLMUnavailable(f"{self.name}: empty reply")
            return text
        raise LLMUnavailable(f"{self.name}: no working model")


class AnthropicLLM:
    """Anthropic Messages API over plain HTTPS (no SDK needed)."""

    BASE = "https://api.anthropic.com/v1"

    def __init__(self, api_key: str, model: str = "auto", tier: str = "fast", timeout: float = 45.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.family, self.tier = "anthropic", tier
        self.model = None if not model or model == "auto" else model
        self.name = f"anthropic:{self.model or 'auto'}"
        self._client = httpx.Client(timeout=timeout, transport=transport, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        self._tried: set = set()

    def list_models(self) -> list[str]:
        r = self._client.get(self.BASE + "/models", params={"limit": 100})
        if r.status_code >= 400:
            raise LLMUnavailable(f"anthropic: can't list models (HTTP {r.status_code}) {r.text[:160]}")
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]

    def resolve(self) -> str:
        chosen = pick_model(self.list_models(), PREFS["anthropic"][self.tier], avoid=self._tried)
        if not chosen:
            raise LLMUnavailable("anthropic: this key has no usable models")
        self.model, self.name = chosen, f"anthropic:{chosen}"
        return chosen

    def complete(self, prompt: str, system: Optional[str] = None, *, json: bool = False,
                 temperature: float = 0.0, max_tokens: int = 800, history=None, **_ignored) -> str:
        if self.model is None:
            self.resolve()
        msgs = [{"role": m["role"], "content": m["content"]} for m in (history or [])
                if m.get("role") in ("user", "assistant") and m.get("content")]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)                                  # the API requires a user turn first
        msgs.append({"role": "user", "content": prompt})
        for attempt in range(2):
            body = {"model": self.model, "max_tokens": max_tokens, "messages": msgs, "temperature": temperature}
            if system:
                body["system"] = system
            try:
                r = self._client.post(self.BASE + "/messages", json=body)
            except httpx.HTTPError as e:
                raise LLMUnavailable(f"{self.name}: network error {e}") from e
            if _model_missing(r.status_code, r.text) and attempt == 0:
                self._tried.add(self.model)
                self.resolve()
                continue
            if r.status_code in (429, 529):
                raise LLMRateLimited(f"{self.name}: rate limited", retry_after=float(r.headers.get("retry-after", "30") or 30))
            if r.status_code in (401, 403):
                raise LLMUnavailable(f"{self.name}: key rejected (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            if not text.strip():
                raise LLMUnavailable(f"{self.name}: empty reply")
            return text
        raise LLMUnavailable(f"{self.name}: no working model")


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
        self.last_errors: dict[str, str] = {}

    def replace(self, providers: list) -> None:
        """Swap the provider list in place (settings changed in the app). Agents keep their reference."""
        with self._lock:
            self.providers = list(providers)
            self._cooldown_until.clear()
            self._cache.clear()
            self.last_errors.clear()
            self.last_provider = None

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
                if getattr(p, "is_offline_engine", False) and errors:
                    kwargs = {**kwargs, "cloud_errors": errors}   # lets the offline engine explain itself
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
                self.last_errors[p.name] = str(e)[:200]
                log.warning("LLM %s rate limited, falling back", p.name)
            except LLMUnavailable as e:
                self._cooldown_until[p.name] = time.monotonic() + 20
                errors.append(str(e))
                self.last_errors[p.name] = str(e)[:200]
                log.warning("LLM %s unavailable: %s", p.name, e)
        raise LLMUnavailable("All LLM providers failed: " + " | ".join(errors))


def build_llms(settings, db=None) -> tuple[FallbackLLM, FallbackLLM]:
    """Returns (fast, smart) fallback chains from .env keys plus any keys added in the app (when db is given)."""
    from jarvis.llm.registry import LLMRegistry

    reg = LLMRegistry(settings, db)
    return FallbackLLM(reg.chain("fast"), name="fast"), FallbackLLM(reg.chain("smart"), name="smart")
