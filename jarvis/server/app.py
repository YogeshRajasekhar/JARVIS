"""
JARVIS server: FastAPI + WebSocket in front of the Brain.

Security model (the server is the most attackable part, so it is locked down):
- Every API call and the WebSocket require the device token (constant-time compare).
  The token is sent in a header / first WS message, never in a URL (URLs end up in logs).
- Brute force: 5 bad tokens from one IP -> that IP is blocked for 15 minutes.
- No CORS unless an origin is explicitly allowed (the Android app's origin).
- Strict security headers + CSP: no inline scripts, no third-party scripts, no framing.
- Turns are serialised (one conversation, one brain lock), so two devices or a fast double
  tap can't interleave half-finished agent flows.
- Bind to 127.0.0.1 by default; expose only through a private network (Tailscale) or TLS proxy.

Run: python -m jarvis.server        (prints the pairing token on first start)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.core.permissions import PermStatus
from jarvis.integrations.device import RemoteDevice, SimulatedDevice
from jarvis.services import build_services

log = logging.getLogger("jarvis.server")
STATIC = os.path.join(os.path.dirname(__file__), "static")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' ws: wss:; "
       "media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    source: str = "text"


class PermissionIn(BaseModel):
    mode: str = Field(pattern="^(once|always|deny)$")


class ApprovalIn(BaseModel):
    approved: bool
    edits: Optional[dict] = None
    via: str = Field("tap", pattern="^(tap|biometric|voice)$")


class ScopeIn(BaseModel):
    scope: str


class UnlockIn(BaseModel):
    confirm: str


class Guard:
    """Token check + per-IP lockout after repeated failures."""

    def __init__(self, token: str, max_fail: int = 5, block_s: int = 900):
        self.token = token
        self.max_fail, self.block_s = max_fail, block_s
        self.fails: dict[str, list[float]] = defaultdict(list)
        self.blocked_until: dict[str, float] = {}

    def check(self, ip: str, presented: Optional[str]) -> bool:
        now = time.time()
        if self.blocked_until.get(ip, 0) > now:
            return False
        if presented and hmac.compare_digest(presented.encode(), self.token.encode()):
            self.fails.pop(ip, None)
            return True
        self.fails[ip] = [t for t in self.fails[ip] if now - t < self.block_s] + [now]
        if len(self.fails[ip]) >= self.max_fail:
            self.blocked_until[ip] = now + self.block_s
            log.warning("blocking %s after %d bad tokens", ip, self.max_fail)
        return False


class Hub:
    """Holds the single active phone connection and pushes events to it."""

    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def send_threadsafe(self, msg: dict) -> None:
        if self.ws is None or self.loop is None:
            raise RuntimeError("no phone connected")
        fut = asyncio.run_coroutine_threadsafe(self.ws.send_json(msg), self.loop)
        fut.result(timeout=5)

    async def push(self, msg: dict) -> None:
        if self.ws is not None:
            try:
                await self.ws.send_json(msg)
            except Exception:
                pass


def _resolve_token(settings: Settings, svc) -> str:
    if settings.device_token:
        return settings.device_token
    tok = svc.db.kv_get("_device_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        svc.db.kv_set("_device_token", tok)
    return tok


def create_app(settings: Optional[Settings] = None, svc=None, enable_wakeword: bool = True,
               model_dir: str = "./models") -> FastAPI:
    settings = settings or Settings()
    hub = Hub()
    sim = SimulatedDevice() if settings.seed_demo_data else None
    bridge = RemoteDevice(hub.send_threadsafe, fallback=sim)
    if svc is None:
        svc = build_services(settings, device=bridge)
    else:
        svc.device = bridge
    brain = Brain(svc)
    token = _resolve_token(settings, svc)
    guard = Guard(token)
    brain_lock = threading.Lock()

    wake_factory = None
    if enable_wakeword:
        try:
            from jarvis.voice.wakeword import WakeWordDetector, models_present
            if models_present(model_dir):
                wake_factory = lambda: WakeWordDetector(model_dir)  # noqa: E731
        except Exception as e:  # openwakeword not installed
            log.warning("wake word disabled: %s", e)

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.svc, app.state.brain, app.state.token, app.state.hub, app.state.bridge = svc, brain, token, hub, bridge

    origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), geolocation=(self), microphone=(self)",
            "Cache-Control": "no-store",
        })
        return resp

    def auth(request: Request):
        h = request.headers.get("authorization", "")
        presented = h[7:] if h.lower().startswith("bearer ") else None
        if not guard.check(request.client.host if request.client else "?", presented):
            raise HTTPException(401, "unauthorised")

    def run_turn(fn, *a, **kw) -> dict:
        with brain_lock:
            res = fn(*a, **kw)
        d = res.to_dict()
        d["pending"] = brain.pending()
        return d

    # ------------------------------------------------------------------ static
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    # ------------------------------------------------------------------ REST (same API as the WebSocket)
    @app.post("/api/message", dependencies=[Depends(auth)])
    async def message(m: MessageIn):
        return await asyncio.to_thread(run_turn, brain.handle, m.text)

    @app.get("/api/pending", dependencies=[Depends(auth)])
    async def pending():
        return {"pending": brain.pending()}

    @app.post("/api/permission/{rid}", dependencies=[Depends(auth)])
    async def permission(rid: str, p: PermissionIn):
        return await asyncio.to_thread(run_turn, brain.resolve_permission, rid, p.mode)

    @app.post("/api/approval/{rid}", dependencies=[Depends(auth)])
    async def approval(rid: str, a: ApprovalIn):
        return await asyncio.to_thread(run_turn, brain.resolve_approval, rid, a.approved, a.edits, a.via)

    @app.get("/api/permissions", dependencies=[Depends(auth)])
    async def permissions():
        return {"permissions": svc.broker.describe(), "locked": svc.executor.locked_down}

    @app.post("/api/permissions/revoke", dependencies=[Depends(auth)])
    async def revoke(s: ScopeIn):
        svc.broker.revoke(s.scope)
        return {"ok": True}

    @app.post("/api/lockdown", dependencies=[Depends(auth)])
    async def lockdown():
        return {"cancelled": svc.executor.lockdown()}

    @app.post("/api/unlock", dependencies=[Depends(auth)])
    async def unlock(u: UnlockIn):
        if u.confirm != "UNLOCK":
            raise HTTPException(400, "type UNLOCK to confirm")
        svc.executor.unlock()
        return {"ok": True}

    @app.get("/api/audit", dependencies=[Depends(auth)])
    async def audit():
        return {"audit": svc.db.audit_log(100)}

    @app.get("/api/history", dependencies=[Depends(auth)])
    async def history():
        return {"messages": svc.db.recent_messages(40)}

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def status():
        return {"integrations": svc.integration_modes, "graph": svc.graph.stats(), "phone_connected": bridge.connected,
                "llm_fast": [p.name for p in svc.fast_llm.providers], "llm_last": svc.fast_llm.last_provider,
                "wake_word": wake_factory is not None, "user": settings.user_name}

    @app.post("/api/tts", dependencies=[Depends(auth)])
    async def tts(m: MessageIn):
        from jarvis.voice.speech import build_tts
        engine = await asyncio.to_thread(build_tts, model_dir)
        if engine is None:
            raise HTTPException(503, "server TTS not installed; the phone speaks locally")
        wav = await asyncio.to_thread(engine.synthesize, m.text[:600])
        return Response(wav, media_type="audio/wav")

    @app.post("/api/stt", dependencies=[Depends(auth)])
    async def stt(request: Request):
        from jarvis.voice.speech import build_stt
        audio = await request.body()
        if len(audio) > 5_000_000:
            raise HTTPException(413, "audio too long")
        engine = await asyncio.to_thread(build_stt, settings)
        if engine is None:
            raise HTTPException(503, "no server STT configured; the phone transcribes locally")
        text = await asyncio.to_thread(engine.transcribe, audio, request.headers.get("x-filename", "audio.webm"))
        return {"text": text}

    # ------------------------------------------------------------------ WebSocket (the phone's live channel)
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        ip = ws.client.host if ws.client else "?"
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
        except Exception:
            await ws.close(code=4401)
            return
        if first.get("type") != "auth" or not guard.check(ip, first.get("token")):
            await ws.send_json({"type": "error", "error": "unauthorised"})
            await ws.close(code=4401)
            return
        hub.ws, hub.loop = ws, asyncio.get_running_loop()
        bridge.set_connected(first.get("device", True) is not False)
        detector = wake_factory() if wake_factory else None
        await ws.send_json({"type": "hello", "user": settings.user_name, "wake_word": detector is not None,
                            "pending": brain.pending()})

        async def turn(fn, *a):
            res = await asyncio.to_thread(run_turn, fn, *a)
            await ws.send_json({"type": "reply", **res})

        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if detector is not None and detector.feed(msg["bytes"]):
                        await ws.send_json({"type": "wake", "score": round(detector.last_score, 3)})
                    continue
                try:
                    data = json.loads(msg.get("text") or "{}")
                except json.JSONDecodeError:
                    continue
                t = data.get("type")
                if t == "message" and isinstance(data.get("text"), str) and 0 < len(data["text"]) <= 2000:
                    asyncio.create_task(turn(brain.handle, data["text"]))
                elif t == "device_result":
                    bridge.deliver(str(data.get("id")), data.get("result") or {})
                elif t == "permission" and data.get("mode") in ("once", "always", "deny"):
                    asyncio.create_task(turn(brain.resolve_permission, str(data.get("id")), data["mode"]))
                elif t == "approval":
                    via = data.get("via") if data.get("via") in ("tap", "biometric", "voice") else "tap"
                    asyncio.create_task(turn(brain.resolve_approval, str(data.get("id")), bool(data.get("approved")),
                                             data.get("edits") if isinstance(data.get("edits"), dict) else None, via))
                elif t == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            if hub.ws is ws:
                hub.ws = None
                bridge.set_connected(False)

    # ------------------------------------------------------------------ proactive nudges
    async def proactive_loop():
        while True:
            await asyncio.sleep(30)
            try:
                if svc.broker.check(["calendar.read"]).status != PermStatus.GRANTED or hub.ws is None:
                    continue
                now = svc.now()
                events = await asyncio.to_thread(svc.calendar.list_events, now, now + timedelta(minutes=15))
                told = set(svc.db.kv_get("_nudged", []) or [])
                for e in events:
                    if e.id in told or e.start < now:
                        continue
                    mins = int((e.start - now).total_seconds() // 60)
                    if mins <= 10:
                        told.add(e.id)
                        await hub.push({"type": "notify", "speak": True,
                                        "text": f"{e.title} starts in {mins} minutes" + (f" at {e.location}." if e.location else ".")})
                svc.db.kv_set("_nudged", list(told)[-200:])
            except Exception as ex:  # never kill the loop
                log.warning("proactive loop: %s", ex)

    async def backup_loop():
        """Daily database backup next to the database (7 kept)."""
        if svc.db.path == ":memory:":
            return
        folder = os.path.join(os.path.dirname(os.path.abspath(svc.db.path)), "backups")
        while True:
            try:
                await asyncio.to_thread(svc.db.backup, folder)
            except Exception as ex:
                log.warning("backup failed: %s", ex)
            await asyncio.sleep(24 * 3600)

    @app.on_event("startup")
    async def _start():
        app.state.proactive = asyncio.create_task(proactive_loop())
        app.state.backups = asyncio.create_task(backup_loop())

    return app
