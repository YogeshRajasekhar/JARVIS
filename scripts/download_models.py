"""
Downloads the free, open models JARVIS uses into ./models (about 3 MB, +60 MB with --voice).

  python scripts/download_models.py          # wake word ("hey jarvis") models
  python scripts/download_models.py --voice  # + Piper offline TTS voice (server-side speech)
"""

import argparse
import os
import tarfile
import urllib.request

WAKE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
VOICE = "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-us-lessac-low.tar.gz"
# British male voice for the JARVIS server voice (MIT-licensed Piper voice, about 63 MB).
GB = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/"


def fetch(url: str, dest: str) -> None:
    if os.path.exists(dest):
        print("ok  ", dest)
        return
    print("get ", url)
    urllib.request.urlretrieve(url, dest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="./models")
    ap.add_argument("--voice", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    for f in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"):
        fetch(WAKE + f, os.path.join(a.dir, f))
    if a.voice and not os.path.exists(os.path.join(a.dir, "en-us-lessac-low.onnx")):
        tgz = os.path.join(a.dir, "voice.tgz")
        fetch(VOICE, tgz)
        with tarfile.open(tgz) as t:
            t.extractall(a.dir, filter="data")
        os.remove(tgz)
    if a.voice:
        for f in ("en_GB-alan-medium.onnx", "en_GB-alan-medium.onnx.json"):
            try:
                fetch(GB + f, os.path.join(a.dir, f))
            except Exception as e:            # optional: the app falls back to the phone's voice
                print("skip", f, e)
    print("done")


if __name__ == "__main__":
    main()
