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
import re
from typing import Optional

import httpx


class STTUnavailable(Exception):
    pass


class WhisperAPISTT:
    """
    Whisper behind an OpenAI-compatible /audio/transcriptions endpoint: Groq (free tier,
    whisper-large-v3-turbo) or OpenAI (paid, whisper-1). verbose_json gives per-segment confidence,
    which clean_transcript uses to drop invented text.
    """

    def __init__(self, base_url: str, api_key: str, model: str, provider: str = "groq"):
        self.base, self.api_key, self.model, self.provider = base_url.rstrip("/"), api_key, model, provider

    def transcribe(self, audio: bytes, filename: str = "audio.webm", prompt: Optional[str] = None) -> str:
        data = {"model": self.model, "language": "en", "response_format": "verbose_json", "temperature": "0"}
        if prompt:
            data["prompt"] = prompt[:400]
        r = httpx.post(f"{self.base}/audio/transcriptions", headers={"Authorization": f"Bearer {self.api_key}"},
                       data=data, files={"file": (filename, audio)}, timeout=60)
        if r.status_code >= 400:
            raise STTUnavailable(f"{self.provider} STT HTTP {r.status_code} {r.text[:120]}")
        j = r.json()
        return clean_transcript(j.get("text", ""), j.get("segments"), prompt)


def GroqWhisperSTT(api_key: str, model: str = "whisper-large-v3-turbo") -> WhisperAPISTT:  # noqa: N802 (compat)
    return WhisperAPISTT("https://api.groq.com/openai/v1", api_key, model, provider="groq")


class LocalWhisperSTT:
    """faster-whisper 'base.en' on CPU. Optional: pip install faster-whisper."""

    def __init__(self, model: str = "base.en"):
        from faster_whisper import WhisperModel  # noqa

        self.model = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, audio: bytes, filename: str = "audio.wav", prompt: Optional[str] = None) -> str:
        segments, _ = self.model.transcribe(io.BytesIO(audio), language="en", vad_filter=True, initial_prompt=prompt)
        return " ".join(s.text.strip() for s in segments).strip()


# Phrases Whisper is known to invent from silence or noise (it learned them from video subtitles).
_PHANTOMS = re.compile(
    r"^(thank you\.?|thanks for watching[.!]?|thank you for watching[.!]?|please subscribe[.!]?|"
    r"subtitles by .*|you\.?|bye\.?|\.+|\s*)$", re.I)


def clean_transcript(text: str, segments: Optional[list] = None, prompt: Optional[str] = None) -> str:
    """
    Drop the parts of a Whisper transcript that are likely invented:
    - segments Whisper itself marks as probably-not-speech or very low confidence,
    - well-known phantom phrases ("Thanks for watching"),
    - echoes of the vocabulary hint (Whisper repeats its prompt when the audio is unclear).
    Returns "" when nothing trustworthy is left, so the app says "didn't catch that" instead of acting.
    """
    if segments:
        kept = [sg.get("text", "") for sg in segments
                if sg.get("no_speech_prob", 0) < 0.6 and sg.get("avg_logprob", 0) > -1.0
                and sg.get("compression_ratio", 1) < 2.4]
        text = " ".join(t.strip() for t in kept)
    text = re.sub(r"\s+", " ", text or "").strip()
    if _PHANTOMS.match(text):
        return ""
    if prompt:
        words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2]
        hint = set(re.findall(r"[a-z]+", prompt.lower()))
        if words and sum(w in hint for w in words) / len(words) > 0.8 and len(words) >= 3:
            return ""          # mostly the hint's own words: an echo, not what was said
    return text


def vocabulary_prompt(user_name: str, names: list[str]) -> str:
    """
    Only proper nouns go in the hint (names of people, projects, places). Generic words like
    "calendar, remind me" were removed: Whisper copies its hint into the transcript when audio is unclear.
    """
    words = ", ".join(dict.fromkeys(n for n in [user_name, "JARVIS", *names] if n and len(n) < 40))
    return words[:400]


def build_stt(settings, registry=None) -> Optional[object]:
    """Server transcription: first enabled provider with Whisper (see LLMRegistry.stt), else local faster-whisper."""
    if registry is None:
        from jarvis.llm.registry import LLMRegistry
        registry = LLMRegistry(settings)
    engine = registry.stt()
    if engine is not None:
        return engine
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
