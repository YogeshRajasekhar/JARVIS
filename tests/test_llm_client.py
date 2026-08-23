"""
Tests for the LLM client wrapper.

Split deliberately into two groups, per the project's own standard: unit
tests against a mocked `anthropic` client (no network, deterministic, run
every time) and one real integration test (marked so it's skipped rather
than failing when no API key is configured — see conftest.py).
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
import httpx
import pytest

from src.llm.client import (
    ClaudeLLMClient,
    HAIKU_MODEL,
    SONNET_MODEL,
    LLMAuthError,
    LLMRequestError,
)


def make_text_block(text):
    return SimpleNamespace(type="text", text=text)


def make_message(text):
    return SimpleNamespace(content=[make_text_block(text)])


def make_api_error(error_cls, status_code, message="error"):
    response = httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return error_cls(message, response=response, body=None)


# ---- Unit tests (mocked, no network) ----


def test_model_is_required():
    """No silent default onto the expensive tier — an empty model must error."""
    with pytest.raises(ValueError):
        ClaudeLLMClient(model="")


def test_missing_api_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMAuthError):
        ClaudeLLMClient(model=HAIKU_MODEL, api_key=None)


def test_complete_returns_text_and_passes_system_prompt():
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(return_value=make_message("hello"))

    result = client.complete("hi", system="be terse")

    assert result == "hello"
    _, kwargs = client._client.messages.create.call_args
    assert kwargs["system"] == "be terse"
    assert kwargs["model"] == HAIKU_MODEL
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_complete_without_system_uses_not_given():
    """No system prompt should mean 'omitted', not 'empty string sent to the API'."""
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(return_value=make_message("hi there"))

    client.complete("hi")

    _, kwargs = client._client.messages.create.call_args
    assert kwargs["system"] is anthropic.NOT_GIVEN


def test_complete_passes_through_max_tokens_and_extra_kwargs():
    client = ClaudeLLMClient(model=SONNET_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(return_value=make_message("ok"))

    client.complete("hi", max_tokens=42, temperature=0.1)

    _, kwargs = client._client.messages.create.call_args
    assert kwargs["max_tokens"] == 42
    assert kwargs["temperature"] == 0.1


def test_default_max_tokens_used_when_not_specified():
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(return_value=make_message("ok"))

    client.complete("hi")

    _, kwargs = client._client.messages.create.call_args
    assert kwargs["max_tokens"] == 1024


def test_concatenates_multiple_text_blocks():
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(
        return_value=SimpleNamespace(
            content=[make_text_block("foo"), make_text_block("bar")]
        )
    )
    assert client.complete("hi") == "foobar"


def test_auth_error_is_not_retried():
    """A bad key won't fix itself on retry — should fail fast, not burn attempts."""
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    mock_create = MagicMock(
        side_effect=make_api_error(anthropic.AuthenticationError, 401, "bad key")
    )
    client._client.messages.create = mock_create

    with pytest.raises(LLMAuthError):
        client.complete("hi")

    assert mock_create.call_count == 1


def test_rate_limit_is_retried_then_raises_request_error(monkeypatch):
    monkeypatch.setattr("src.llm.client.time.sleep", lambda _: None)
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    mock_create = MagicMock(
        side_effect=make_api_error(anthropic.RateLimitError, 429, "rate limited")
    )
    client._client.messages.create = mock_create

    with pytest.raises(LLMRequestError):
        client.complete("hi")

    assert mock_create.call_count == ClaudeLLMClient.MAX_RETRIES


def test_rate_limit_recovers_on_retry(monkeypatch):
    """A transient rate limit that clears on a later attempt should still succeed."""
    monkeypatch.setattr("src.llm.client.time.sleep", lambda _: None)
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    mock_create = MagicMock(
        side_effect=[
            make_api_error(anthropic.RateLimitError, 429, "rate limited"),
            make_message("recovered"),
        ]
    )
    client._client.messages.create = mock_create

    result = client.complete("hi")

    assert result == "recovered"
    assert mock_create.call_count == 2


def test_raw_sdk_exception_never_leaks_unhandled(monkeypatch):
    """
    Adversarial: even a generic APIError (not auth, not rate-limit) must come
    out as our typed LLMRequestError, never the raw anthropic exception.
    """
    monkeypatch.setattr("src.llm.client.time.sleep", lambda _: None)
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(
        side_effect=make_api_error(anthropic.InternalServerError, 500, "boom")
    )

    with pytest.raises(LLMRequestError):
        client.complete("hi")


def test_raw_sdk_exception_type_not_raised_directly(monkeypatch):
    """Belt-and-suspenders: the exception instance raised must not be the SDK's own type."""
    monkeypatch.setattr("src.llm.client.time.sleep", lambda _: None)
    client = ClaudeLLMClient(model=HAIKU_MODEL, api_key="sk-test-fake")
    client._client.messages.create = MagicMock(
        side_effect=make_api_error(anthropic.InternalServerError, 500, "boom")
    )

    try:
        client.complete("hi")
        pytest.fail("expected LLMRequestError")
    except LLMRequestError as e:
        assert not isinstance(e, anthropic.AnthropicError)


# ---- Integration test (real network call, opt-in) ----


@pytest.mark.integration
def test_live_call_returns_nonempty_string():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping live integration test")
    client = ClaudeLLMClient(model=HAIKU_MODEL)
    result = client.complete("Reply with exactly one word: hello", max_tokens=10)
    assert isinstance(result, str)
    assert len(result.strip()) > 0
