"""
Central configuration, read once from environment variables (or a .env file).

Every value has a safe default so the system boots in "offline" mode with no keys at all:
in that mode the deterministic RuleLLM answers instead of a cloud model, which is what the
test-suite and the virtual phone use inside the sandbox.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # ---- LLM providers (all have free tiers) ----
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL"))

    # Fast tier = routing / extraction. Smart tier = planning / conversation.
    groq_fast_model: str = field(default_factory=lambda: _env("GROQ_FAST_MODEL", "llama-3.1-8b-instant"))
    groq_smart_model: str = field(default_factory=lambda: _env("GROQ_SMART_MODEL", "llama-3.3-70b-versatile"))
    gemini_fast_model: str = field(default_factory=lambda: _env("GEMINI_FAST_MODEL", "gemini-2.5-flash-lite"))
    gemini_smart_model: str = field(default_factory=lambda: _env("GEMINI_SMART_MODEL", "gemini-2.5-flash"))
    openrouter_model: str = field(
        default_factory=lambda: _env("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    )
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.1:8b"))

    # "auto" = use whatever providers have keys, falling back to RuleLLM; "offline" = RuleLLM only.
    llm_mode: str = field(default_factory=lambda: _env("JARVIS_LLM_MODE", "auto"))

    # ---- Identity / locale ----
    user_name: str = field(default_factory=lambda: _env("JARVIS_USER_NAME", "Yogesh"))
    timezone: str = field(default_factory=lambda: _env("JARVIS_TIMEZONE", "Asia/Kolkata"))

    # ---- Storage ----
    db_path: str = field(default_factory=lambda: _env("JARVIS_DB_PATH", "./data/jarvis.db"))
    # 32-byte urlsafe base64 key for field encryption at rest. Generated on first run if empty.
    data_key: str = field(default_factory=lambda: _env("JARVIS_DATA_KEY"))

    # ---- Server / device auth ----
    # Shared secret that the phone app must present. Generated on first run if empty.
    device_token: str = field(default_factory=lambda: _env("JARVIS_DEVICE_TOKEN"))
    allowed_origins: str = field(default_factory=lambda: _env("JARVIS_ALLOWED_ORIGINS", ""))

    # ---- Integrations (empty = use the mock client) ----
    google_credentials_path: str = field(default_factory=lambda: _env("GOOGLE_CREDENTIALS_PATH"))
    google_token_path: str = field(default_factory=lambda: _env("GOOGLE_TOKEN_PATH", "./data/google_token.json"))
    slack_user_token: str = field(default_factory=lambda: _env("SLACK_USER_TOKEN"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_user: str = field(default_factory=lambda: _env("GITHUB_USER", "YogeshRajasekhar"))

    # Mock integrations are seeded with demo data so the virtual phone has something to show.
    seed_demo_data: bool = field(default_factory=lambda: _env("JARVIS_SEED_DEMO", "1") == "1")


def get_settings() -> Settings:
    return Settings()
