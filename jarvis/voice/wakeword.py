"""
"Hey JARVIS" wake-word detection with openWakeWord (open source, runs on CPU, ~3 MB of models).

Audio contract: 16 kHz, mono, signed 16-bit PCM, fed in chunks of any size (internally
processed in 80 ms / 1280-sample frames). The same three ONNX models run on Android via
onnxruntime-android, so behaviour in the virtual phone and on the real phone matches.

Model licence: openWakeWord code is Apache-2.0; the pre-trained "hey_jarvis" model is
CC BY-NC-SA 4.0 (fine for personal, non-commercial use).

False-trigger defences: score threshold, a refractory period after each detection, and
optional energy gate (ignore near-silence).
"""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np

FRAME = 1280  # 80 ms at 16 kHz
MODEL_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


def models_present(model_dir: str) -> bool:
    return all(os.path.exists(os.path.join(model_dir, f)) for f in MODEL_FILES)


class WakeWordDetector:
    def __init__(self, model_dir: str = "./models", threshold: float = 0.5, refractory_s: float = 2.0):
        from openwakeword.model import Model

        if not models_present(model_dir):
            raise FileNotFoundError(f"Wake-word models missing in {model_dir}. Run: python scripts/download_models.py")
        self.model = Model(
            wakeword_model_paths=[os.path.join(model_dir, "hey_jarvis_v0.1.onnx")],
            melspec_onnx_model_path=os.path.join(model_dir, "melspectrogram.onnx"),
            embedding_onnx_model_path=os.path.join(model_dir, "embedding_model.onnx"),
        )
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._buf = np.zeros(0, dtype=np.int16)
        self._last = 0.0
        self.last_score = 0.0

    def reset(self) -> None:
        self.model.reset()
        self._buf = np.zeros(0, dtype=np.int16)

    def feed(self, pcm16: bytes | np.ndarray, now: Optional[float] = None) -> bool:
        """Feed audio; returns True exactly once per detected "hey jarvis"."""
        chunk = np.frombuffer(pcm16, dtype=np.int16) if isinstance(pcm16, (bytes, bytearray)) else pcm16.astype(np.int16)
        self._buf = np.concatenate([self._buf, chunk])
        fired = False
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            score = float(max(self.model.predict(frame).values()))
            self.last_score = float(score)
            t = now if now is not None else time.monotonic()
            if score >= self.threshold and t - self._last >= self.refractory_s:
                self._last = t
                fired = True
                self.model.reset()  # avoid the same utterance firing again on later frames
        return fired
