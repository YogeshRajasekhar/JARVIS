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
    # Open-weight models only on Groq (free): Qwen3 for quick structured tasks (strong at JSON, fast on Groq),
    # gpt-oss-120b / Kimi K2 for conversation and planning, Llama as the fallback.
    "groq": {"fast": [r"^qwen/qwen3-32b$", r"qwen3", r"gpt-oss-20b", r"^llama-3\.1-8b-instant$", r"llama.*8b", r"qwen", r"llama"],
             "smart": [r"gpt-oss-120b", r"kimi-k2-instruct(-\d+)?$", r"^qwen/qwen3-32b$", r"^llama-3\.3-70b-versatile$",
                       r"llama.*70b", r"qwen", r"gpt-oss", r"llama"]},
    "gemini": {"fast": [r"^gemini-[\d.]+-flash-lite$", r"^gemini-[\d.]+-flash$", r"flash", r"gemma"],
               "smart": [r"^gemini-[\d.]+-flash$", r"^gemini-[\d.]+-flash-[\w-]*$", r"flash", r"gemma"]},
    "openrouter": {"fast": [r"qwen3.*:free$", r"qwen.*:free$", r"llama.*:free$", r":free$"],
                   "smart": [r"qwen3-235b.*:free$", r"gpt-oss-120b:free$", r"deepseek.*(v3|chat).*:free$", r"llama.*70b.*:free$", r":free$"]},
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
    return bool(model and re.search(r"gpt-oss|deepseek-r|qwq|qwen3|reasoning|-r1\b|magistral|^o[1-9](-|$)|^gpt-5", model, re.I))


_THINK = re.compile(r"<think>.*?</think>\s*|^.*?</think>\s*", re.S)


def strip_thinking(text: str) -> str:
    """Some open models (Qwen3, DeepSeek-R1) print their reasoning in <think> tags: keep only the answer."""
    return _THINK.sub("", text).strip() if "think>" in text else text


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


class LLMNoCredit(LLMUnavailable):
    """The key is valid but the account has no credit/quota (OpenAI 'insufficient_quota', Anthropic low balance).
    Looks like a rate limit (often HTTP 429) but waiting won't fix it."""


_NO_CREDIT_RX = re.compile(r"insufficient_quota|exceeded your current quota|credit balance is too low|"
                           r"insufficient (?:credits|balance)|requires more credits|payment required|billing", re.I)


def _no_credit(status: int, text: str) -> bool:
    return status in (400, 402, 403, 429) and bool(_NO_CREDIT_RX.search(text or ""))


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


_WAIT_RX = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)
_DUR_RX = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)(ms|s))?$")


def _dur(text: str) -> Optional[float]:
    m = _DUR_RX.match((text or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, v, unit = m.groups()
    sec = float(v or 0) / (1000 if unit == "ms" else 1)
    return int(h or 0) * 3600 + int(mi or 0) * 60 + sec


def _rate_limit_wait(r) -> tuple[float, bool]:
    """Seconds until the limit resets, and whether it's a daily limit. Reads Groq's message and headers."""
    body = r.text or ""
    daily = bool(re.search(r"per day|\(RPD\)|\(TPD\)|daily", body, re.I))
    m = _WAIT_RX.search(body)
    if m and any(m.groups()):
        h, mi, sec = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0), daily
    try:
        ra = float(r.headers.get("retry-after", ""))
        return ra, daily
    except ValueError:
        pass
    for h in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        d = _dur(r.headers.get(h, ""))
        if d is not None:
            return d, daily
    return 30.0, daily


def _fmt_wait(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


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
        # Free tiers (Groq) limit EACH MODEL separately, so when one model is rate limited the same key can
        # usually keep going on another model. model -> monotonic time its limit resets.
        self._limited: dict[str, float] = {}
        self._preferred: Optional[str] = self.model
        self._models_cache: tuple[float, list[str]] = (0.0, [])
        self.sleep = time.sleep            # tests replace this

    def list_models(self) -> list[str]:
        at, cached = self._models_cache
        if cached and time.monotonic() - at < 3600:
            return cached
        r = self._client.get(self._base + "/models")
        if r.status_code >= 400:
            raise LLMUnavailable(f"{self.family}: can't list models (HTTP {r.status_code}) {r.text[:120]}")
        ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
        self._models_cache = (time.monotonic(), ids)
        return ids

    def _limited_now(self) -> set:
        now = time.monotonic()
        return {m for m, until in self._limited.items() if until > now}

    def _switch_after_limit(self) -> bool:
        """Move to the next-best model that isn't rate limited. True if there is one."""
        prefs = PREFS.get(self.family, {}).get(self.tier, [])
        try:
            alt = pick_model(self.list_models(), prefs, avoid=self._tried | self._limited_now())
        except LLMUnavailable:
            return False
        if not alt or alt == self.model:
            return False
        log.warning("%s: %s is rate limited, using %s for now", self.family, self.model, alt)
        self.model, self.name = alt, f"{self.family}:{alt}"
        return True

    def resolve(self) -> str:
        prefs = PREFS.get(self.family, {}).get(self.tier, [])
        chosen = pick_model(self.list_models(), prefs, avoid=self._tried)
        if not chosen:
            raise LLMUnavailable(f"{self.family}: this key has no usable chat models")
        if self.model and self.model != chosen:
            log.warning("%s: model %s unavailable, switching to %s", self.family, self.model, chosen)
        self.model, self.name = chosen, f"{self.family}:{chosen}"
        if self._preferred is None or self._preferred in self._tried:
            self._preferred = chosen
        return chosen

    def _body(self, prompt, system, history, json, temperature, max_tokens) -> dict:
        body = {"model": self.model, "messages": _messages(system, prompt, history)}
        # Reasoning models think inside the token budget; Qwen3 with thinking off doesn't, and an oversized
        # max_tokens counts against Groq's tokens-per-minute limit, so only add room when it's needed.
        thinking = _is_reasoning(self.model) and not ("qwen3" in (self.model or "").lower() and self.family == "groq")
        budget = max_tokens + (1024 if thinking else 0)
        body[self._max_key] = budget          # reasoning models: thinking counts against the budget
        if self._temperature_ok:
            body["temperature"] = temperature
        if _is_reasoning(self.model) and self._reasoning_params:
            # Qwen3 on Groq: "none" turns thinking off (fast, direct answers); gpt-oss only knows low/medium/high.
            body["reasoning_effort"] = "none" if "qwen3" in self.model.lower() and self.family == "groq" else "low"
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
        # Go back to the preferred model once its rate limit has reset.
        if self._preferred and self.model != self._preferred and self._preferred not in self._limited_now() \
                and self._preferred not in self._tried:
            self.model, self.name = self._preferred, f"{self.family}:{self._preferred}"
        switched = False
        waited = False
        for _ in range(8):
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
            if _no_credit(r.status_code, r.text):
                raise LLMNoCredit(f"{self.name}: no credit on this account")
            if r.status_code == 429:
                wait, daily = _rate_limit_wait(r)
                # A per-minute limit that resets in a moment: wait briefly and retry the same model.
                if wait <= 3 and not daily and not waited:
                    waited = True
                    self.sleep(wait + 0.2)
                    continue
                # Otherwise this model is out for now; the same key's other models have their own limits.
                self._limited[self.model] = time.monotonic() + min(wait, 6 * 3600)
                if self._switch_after_limit():
                    continue
                kind = "daily" if daily else "per-minute"
                raise LLMRateLimited(f"{self.name}: rate limited ({kind} limit, resets in {_fmt_wait(wait)})",
                                     retry_after=min(wait, 300))
            if r.status_code in (401, 403):
                raise LLMUnavailable(f"{self.name}: key rejected (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise LLMUnavailable(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
            try:
                choice = r.json()["choices"][0]
                content = strip_thinking(choice["message"].get("content") or "")
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
                wait, daily = _rate_limit_wait(r)
                kind = "daily" if daily or "PerDay" in r.text else "per-minute"
                raise LLMRateLimited(f"{self.name}: rate limited ({kind} limit, resets in {_fmt_wait(wait)})",
                                     retry_after=min(wait, 300))
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
            if _no_credit(r.status_code, r.text):
                raise LLMNoCredit(f"{self.name}: no credit on this account")
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
                self._cooldown_until[p.name] = time.monotonic() + (3600 if isinstance(e, LLMNoCredit) else 20)
                errors.append(str(e))
                self.last_errors[p.name] = str(e)[:200]
                log.warning("LLM %s unavailable: %s", p.name, e)
        raise LLMUnavailable("All LLM providers failed: " + " | ".join(errors))


def build_llms(settings, db=None) -> tuple[FallbackLLM, FallbackLLM]:
    """Returns (fast, smart) fallback chains from .env keys plus any keys added in the app (when db is given)."""
    from jarvis.llm.registry import LLMRegistry

    reg = LLMRegistry(settings, db)
    return FallbackLLM(reg.chain("fast"), name="fast"), FallbackLLM(reg.chain("smart"), name="smart")
