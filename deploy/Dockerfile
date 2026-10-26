FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
# default-jre-headless: signs app updates on THIS server (the signing key never leaves /data).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
ENV JAVA_TOOL_OPTIONS="-Xmx384m -Djava.io.tmpdir=/tmp"

WORKDIR /app
COPY requirements-v2.txt .
RUN pip install --no-cache-dir -r requirements-v2.txt
COPY src ./src
COPY jarvis ./jarvis
COPY scripts ./scripts
# Server voices are optional; each engine installs on its own so one failure doesn't drop the other.
RUN pip install --no-cache-dir piper-tts==1.8.0 || echo "piper unavailable"
RUN pip install --no-cache-dir kokoro-onnx==0.4.9 || echo "kokoro unavailable"
RUN python scripts/download_models.py --dir /app/models --voice

# Run as an unprivileged user; the only writable place is /data (the volume).
RUN useradd --create-home --uid 10001 jarvis && mkdir -p /data && chown jarvis /data
USER jarvis
ENV JARVIS_DB_PATH=/data/jarvis.db JARVIS_MODELS=/app/models JARVIS_HOST=0.0.0.0 JARVIS_PORT=8765
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://127.0.0.1:8765/api/health || exit 1
CMD ["python", "-m", "jarvis.server"]
