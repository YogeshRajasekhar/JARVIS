import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.config import Settings  # noqa: E402
from jarvis.core.storage import Database  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
# Saturday 24 Oct 2026, 09:00 IST: fixed so every date assertion is reproducible.
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)

ALL_SCOPES = ["calendar.read", "calendar.write", "mail.read", "mail.send", "slack.read", "slack.send",
              "github.read", "github.write", "contacts.read", "location", "notifications.read",
              "alarms.write", "apps.launch", "settings.toggle", "phone.call", "sms.send"]


def offline_settings() -> Settings:
    s = Settings()
    s.llm_mode = "offline"
    for k in ("groq_api_key", "gemini_api_key", "openrouter_api_key", "anthropic_api_key", "ollama_url",
              "google_credentials_path", "slack_user_token", "github_token"):
        setattr(s, k, "")
    s.db_path = ":memory:"
    return s


@pytest.fixture
def svc():
    from jarvis.services import build_services

    return build_services(offline_settings(), db=Database(":memory:"), clock=lambda: NOW, seed=True)


@pytest.fixture
def granted(svc):
    for s in ALL_SCOPES:
        svc.broker.grant(s, "always")
    return svc


@pytest.fixture
def brain(granted):
    from jarvis.brain import Brain

    return Brain(granted)


@pytest.fixture
def fresh_brain(svc):
    """Brain with NO permissions granted."""
    from jarvis.brain import Brain

    return Brain(svc)
