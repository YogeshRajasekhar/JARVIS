"""
Free speech-to-text and text-to-speech options.

Where each runs (all zero cost):
  Phone app       STT: Android SpeechRecognizer (on-device packs, works offline)
                  TTS: Android TextToSpeech (offline)
  Virtual phone   STT: browser Web Speech API (Chrome/Edge)    TTS: browser speechSynthesis
  Server fallback STT: Groq Whisper API (free tier) or local faster-whisper if installed
                  TTS: Piper (offline neural voice, ~60 MB) if a voice is downloaded

The server endpoints exist so that any client without built-in speech (e.g. Firefox, or a
headless test) still gets voice. They are optional; the brain itself only sees text.
"""

from __future__ import annotations

import io
import os
import wave
from typing import Optional

import httpx


class STTUnavailable(Exception):
    pass


class GroqWhisperSTT:
    """Groq hosts Whisper large-v3-turbo on its free tier (OpenAI-compatible endpoint)."""

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        self.api_key, self.model = api_key, model

    def transcribe(self, audio: bytes, filename: str = "audio.webm") -> str:
        r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                       headers={"Authorization": f"Bearer {self.api_key}"},
                       data={"model": self.model, "language": "en", "response_format": "json"},
                       files={"file": (filename, audio)}, timeout=60)
        if r.status_code >= 400:
            raise STTUnavailable(f"Groq STT HTTP {r.status_code}")
        return r.json().get("text", "").strip()


class LocalWhisperSTT:
    """faster-whisper 'base.en' on CPU. Optional: pip install faster-whisper."""

    def __init__(self, model: str = "base.en"):
        from faster_whisper import WhisperModel  # noqa

        self.model = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, audio: bytes, filename: str = "audio.wav") -> str:
        segments, _ = self.model.transcribe(io.BytesIO(audio), language="en", vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


def build_stt(settings) -> Optional[object]:
    if settings.groq_api_key and settings.llm_mode != "offline":
        return GroqWhisperSTT(settings.groq_api_key)
    try:
        return LocalWhisperSTT()
    except Exception:
        return None


class PiperTTS:
    """Offline neural TTS. Voice files: scripts/download_models.py --voice."""

    def __init__(self, voice_path: str):
        from piper import PiperVoice

        if not os.path.exists(voice_path):
            raise FileNotFoundError(voice_path)
        self.voice = PiperVoice.load(voice_path)

    def synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            if hasattr(self.voice, "synthesize_wav"):
                self.voice.synthesize_wav(text, w)
            else:
                self.voice.synthesize(text, w)
        return buf.getvalue()


def build_tts(model_dir: str = "./models") -> Optional[PiperTTS]:
    path = os.path.join(model_dir, "en-us-lessac-low.onnx")
    try:
        return PiperTTS(path)
    except Exception:
        return None
