"""
Minimal, dependency-light port of the openWakeWord streaming pipeline (onnxruntime + numpy).
This is the exact algorithm the Android Kotlin detector implements (android/.../WakeWordEngine.kt),
kept here so the port can be verified against real audio:  python scripts/wakeword_reference.py file.wav
"""

import sys
import wave
from collections import deque

import numpy as np
import onnxruntime as ort

FRAME = 1280          # 80 ms of 16 kHz audio
MEL_CTX = 160 * 3     # extra samples the melspectrogram model needs for its window


class RefWakeWord:
    def __init__(self, model_dir="./models", threshold=0.5):
        o = ort.SessionOptions()
        o.intra_op_num_threads = 1
        self.mel = ort.InferenceSession(f"{model_dir}/melspectrogram.onnx", o)
        self.emb = ort.InferenceSession(f"{model_dir}/embedding_model.onnx", o)
        self.ww = ort.InferenceSession(f"{model_dir}/hey_jarvis_v0.1.onnx", o)
        self.threshold = threshold
        self.reset()

    def _melspec(self, samples: np.ndarray) -> np.ndarray:
        out = self.mel.run(None, {"input": samples.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10.0 + 2.0          # (frames, 32)

    def _embed(self, mel76: np.ndarray) -> np.ndarray:
        return self.emb.run(None, {"input_1": mel76.astype(np.float32)[None, :, :, None]})[0].reshape(96)

    def reset(self):
        self.raw = deque(maxlen=FRAME + MEL_CTX)
        self.raw.extend([0] * (FRAME + MEL_CTX))
        self.mel_buf = np.ones((76, 32), dtype=np.float32)
        zero_emb = self._embed(self._melspec(np.zeros(76 * 160 + MEL_CTX, dtype=np.int16))[-76:])
        self.feat = deque([zero_emb] * 16, maxlen=16)
        self.n = 0

    def process(self, frame: np.ndarray) -> float:
        """frame: exactly 1280 int16 samples. Returns the wake score for this 80 ms."""
        self.raw.extend(frame.tolist())
        mel = self._melspec(np.array(self.raw, dtype=np.int16))
        self.mel_buf = np.vstack([self.mel_buf, mel])[-76:]
        self.feat.append(self._embed(self.mel_buf))
        score = float(self.ww.run(None, {"x.1": np.stack(self.feat)[None, :, :].astype(np.float32)})[0].reshape(-1)[0])
        self.n += 1
        return 0.0 if self.n < 5 else score


if __name__ == "__main__":
    det = RefWakeWord(sys.argv[2] if len(sys.argv) > 2 else "./models")
    a = np.frombuffer(wave.open(sys.argv[1]).readframes(10 ** 8), dtype=np.int16)
    a = np.concatenate([np.zeros(16000, np.int16), a, np.zeros(16000, np.int16)])
    best = max(det.process(a[i:i + FRAME]) for i in range(0, len(a) - FRAME + 1, FRAME))
    print(f"{sys.argv[1]}: max score {best:.3f}")
