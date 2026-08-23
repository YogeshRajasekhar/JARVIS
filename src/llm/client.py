"""
Provider-agnostic LLM wrapper, Claude-backed here.

Design rationale:
- The rest of the codebase (Scheduler, Memory Agent, Planner, Supervisor) depends
  only on the `LLMClient` protocol, never on `anthropic` directly. That's what
  makes "swap to GPT-4o-mini/Ollama later" a one-file change instead of a
  refactor touching every agent — the same reasoning as `CalendarClient` being
  an interface with a mock implementation behind it.
- Cost-tiering is a per-agent choice, not a global default: this mirrors an
  earlier decision on this project to pick GPT-4o vs GPT-4o-mini per agent by
  task complexity, translated onto Claude's tiers. Structured/narrow tasks
  (intent parsing, NL->query translation, routing) get the cheap, fast model;
  the one agent doing genuine multi-step reasoning under conflicting
  constraints (Planner/Replanner, arbitrating priority) keeps the strongest
  model. There is deliberately no hardcoded default model on the client
  constructor — a caller that forgets to think about cost tier should get a
  loud error, not a silent slide onto the expensive model.
- Errors are translated into typed exceptions (`LLMAuthError`, `LLMRequestError`)
  rather than letting raw SDK exceptions leak into agent code. Agents that
  catch `LLMRequestError` don't need to know anything about the `anthropic`
  package's exception hierarchy.
"""

import os
import time
from typing import Optional, Protocol, runtime_checkable

import anthropic
from dotenv import load_dotenv

load_dotenv()


# Cost-tier model identifiers, exposed as constants so agents state their
# intent ("I am a narrow/structured task" vs "I am doing real reasoning")
# rather than hardcoding a model string each agent would need to remember to
# update. See module docstring for which agent uses which tier and why.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-5"


class LLMError(Exception):
    """Base class for all errors raised by this module."""


class LLMAuthError(LLMError):
    """Raised when the API key is missing or rejected. Not retryable."""


class LLMRequestError(LLMError):
    """
    Raised when a request ultimately fails after retries (rate limit
    exhausted, timeout, malformed response, or any other SDK-level failure).
    Agents catch this instead of any `anthropic`-specific exception type.
    """


@runtime_checkable
class LLMClient(Protocol):
    """
    Minimal interface every LLM-backed agent codes against. Any provider
    (Claude, OpenAI, a local Ollama model) can implement this and be a
    drop-in replacement for agents — none of them import `anthropic` or any
    other provider SDK directly.
    """

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        """Send one prompt, return the model's text response as a plain string."""
        ...


class ClaudeLLMClient:
    """
    `LLMClient` implementation backed by the Anthropic API.

    `model` has no default — pass `HAIKU_MODEL` or `SONNET_MODEL` (or another
    valid model id) explicitly. This is intentional: a constructor default
    would inevitably be the "safe-sounding" strongest model, and every call
    site that didn't think about cost tier would silently pay for it. Making
    the caller choose is the cheap way to enforce the tiering discipline
    described in the module docstring.
    """

    MAX_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1.0

    def __init__(self, model: str, api_key: Optional[str] = None):
        if not model:
            raise ValueError(
                "ClaudeLLMClient requires an explicit model id (e.g. HAIKU_MODEL or "
                "SONNET_MODEL) — there is no default, so callers must consciously "
                "choose a cost tier."
            )
        self.model = model
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise LLMAuthError(
                "ANTHROPIC_API_KEY is not set (checked constructor arg and environment). "
                "Copy .env.example to .env and fill it in."
            )
        # The SDK doesn't validate the key at construction time (no network
        # call happens here) — a bad key surfaces as AuthenticationError on
        # the first real request, which `complete()` below translates.
        self._client = anthropic.Anthropic(api_key=resolved_key)

    def complete(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        max_tokens = kwargs.pop("max_tokens", 1024)
        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            try:
                message = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system if system is not None else anthropic.NOT_GIVEN,
                    messages=[{"role": "user", "content": prompt}],
                    **kwargs,
                )
                return "".join(
                    block.text for block in message.content if block.type == "text"
                )
            except anthropic.AuthenticationError as e:
                # Not retryable — bad key isn't going to fix itself on retry.
                raise LLMAuthError(f"Anthropic API rejected credentials: {e}") from e
            except anthropic.RateLimitError as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BASE_BACKOFF_SECONDS * (2**attempt))
                    continue
            except anthropic.APIError as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BASE_BACKOFF_SECONDS * (2**attempt))
                    continue

        raise LLMRequestError(
            f"Claude API request failed after {self.MAX_RETRIES} attempts: {last_error}"
        ) from last_error
