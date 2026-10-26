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
    """Offline neural TTS (Piper). Voice files are fetched by scripts/download_models.py."""

    def __init__(self, voice_path: str):
        from piper import PiperVoice

        if not os.path.exists(voice_path):
            raise FileNotFoundError(voice_path)
        self.voice = PiperVoice.load(voice_path)

    def pcm(self, text: str, length_scale: float = 1.0) -> tuple["np.ndarray", int]:
        """Mono float32 samples in [-1, 1] and the sample rate."""
        import numpy as np
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            try:
                from piper import SynthesisConfig
                self.voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=length_scale))
            except (ImportError, TypeError):
                (self.voice.synthesize_wav if hasattr(self.voice, "synthesize_wav") else self.voice.synthesize)(text, w)
        buf.seek(0)
        with wave.open(buf, "rb") as r:
            rate, raw = r.getframerate(), r.readframes(r.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate

    def synthesize(self, text: str) -> bytes:
        x, rate = self.pcm(text)
        return to_wav(x, rate)


def to_wav(x, rate: int) -> bytes:
    import numpy as np
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def synthetic_colour(x, rate: int, pitch: float = 0.93):
    """
    JARVIS's own character: an original "calm synthetic assistant" treatment, not an imitation of any
    actor. Slightly lower pitch, a faint doubled (chorus) layer, a small bright room, gentle presence lift.
    The input should be synthesised `pitch` times faster, so duration ends up unchanged.
    """
    import numpy as np
    if x.size < rate // 20:
        return x
    # 1. lower the pitch by resampling (stretches time back to normal speed)
    n = int(len(x) / pitch)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    # 2. chorus: a copy delayed 9-15 ms with a slow wobble, mixed low
    t = np.arange(n)
    d = (0.012 + 0.003 * np.sin(2 * np.pi * 0.6 * t / rate)) * rate
    idx = np.clip(t - d, 0, n - 1)
    y = 0.86 * y + 0.22 * np.interp(idx, t, y).astype(np.float32)
    # 3. small room: a few short decaying reflections
    out = y.copy()
    for ms, g in ((23, 0.18), (37, 0.12), (53, 0.08), (79, 0.05)):
        k = int(rate * ms / 1000)
        out[k:] += g * y[:-k]
    # 4. presence: add a little of the high band (first difference) for a crisp, "digital" edge
    hi = np.concatenate([[0.0], np.diff(out)]).astype(np.float32)
    out = out + 0.35 * hi
    # 5. a soft ring-modulated shimmer, very low, for the synthetic sheen
    out += 0.025 * out * np.sin(2 * np.pi * 42 * t / rate).astype(np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.89).astype(np.float32)


VOICES = {
    # id: (file, label, colour)
    "jarvis": ("en_GB-alan-medium.onnx", "JARVIS (British, synthetic)", True),
    "british": ("en_GB-alan-medium.onnx", "British male, natural", False),
    "classic": ("en-us-lessac-low.onnx", "American, natural", False),
}
_tts_cache: dict = {}


def available_voices(model_dir: str = "./models") -> list[dict]:
    out = []
    for vid, (f, label, _) in VOICES.items():
        if os.path.exists(os.path.join(model_dir, f)):
            out.append({"id": vid, "label": label})
    if out and not any(v["id"] == "jarvis" for v in out) and os.path.exists(os.path.join(model_dir, VOICES["classic"][0])):
        out.insert(0, {"id": "jarvis", "label": "JARVIS (synthetic)"})      # colour over the fallback voice
    return out


def speak(text: str, voice: str = "jarvis", model_dir: str = "./models") -> Optional[bytes]:
    """WAV bytes for `text` in the chosen server voice, or None when no voice file is installed."""
    f, _, colour = VOICES.get(voice, VOICES["jarvis"])
    path = os.path.join(model_dir, f)
    if not os.path.exists(path):
        path = os.path.join(model_dir, VOICES["classic"][0])
    if path not in _tts_cache:
        try:
            _tts_cache[path] = PiperTTS(path)
        except Exception:
            return None
    eng = _tts_cache[path]
    if not colour:
        return eng.synthesize(text)
    x, rate = eng.pcm(text, length_scale=0.93)
    return to_wav(synthetic_colour(x, rate, 0.93), rate)


def build_tts(model_dir: str = "./models") -> Optional[PiperTTS]:
    path = os.path.join(model_dir, "en-us-lessac-low.onnx")
    try:
        return PiperTTS(path)
    except Exception:
        return None
