"""python -m jarvis.server  [--host 127.0.0.1] [--port 8765]"""

import argparse
import logging
import os

import uvicorn

from jarvis.config import Settings
from jarvis.server.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("JARVIS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_PORT", "8765")))
    ap.add_argument("--models", default=os.environ.get("JARVIS_MODELS", "./models"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    app = create_app(settings, model_dir=a.models)
    svc = app.state.svc
    print("\n  JARVIS is up.")
    print(f"  Open  http://{a.host}:{a.port}/  (virtual phone)")
    print(f"  Pairing token: {app.state.token}")
    print(f"  LLM chain: {' -> '.join(p.name for p in svc.fast_llm.providers)}")
    print(f"  Integrations: {svc.integration_modes}\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
