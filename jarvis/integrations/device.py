"""
Device bridge: how the brain asks the phone to do things.

The brain never touches the phone directly. It sends a command over the authenticated
WebSocket; the phone app executes it with Android APIs (AlarmClock intents, ContactsContract,
FusedLocation, NotificationListenerService, ...) and returns a result.

Commands (the full contract, mirrored in server/static/phone.js and the Android plugin):
  set_alarm {hour, minute, label}         set_timer {seconds, label}
  set_reminder {at_iso, label}            open_app {app}
  call {number, name}                     send_sms {number, name, text}
  find_contact {name}                     get_location {}
  read_notifications {limit}              toggle {setting, state}
  battery {}

Defence in depth: notifications are filtered on the phone AND again here.
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from jarvis.core.permissions import is_blocked_app
from jarvis.core.redaction import redact

COMMANDS = {"set_alarm", "set_timer", "set_reminder", "open_app", "call", "send_sms", "find_contact",
            "get_location", "read_notifications", "toggle", "battery"}


class DeviceError(Exception):
    pass


def filter_notifications(items: list[dict]) -> list[dict]:
    """Drop banking/payment/password-app notifications and redact OTPs from the rest."""
    out = []
    for n in items:
        if is_blocked_app(n.get("app", "")):
            continue
        text = redact(f"{n.get('title', '')}: {n.get('text', '')}")
        if "[REDACTED:OTP]" in text:
            continue  # an OTP notification has no other useful content
        out.append({"app": n.get("app"), "title": redact(n.get("title", "")), "text": redact(n.get("text", "")),
                    "time": n.get("time")})
    return out


class DeviceBridge(ABC):
    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def call(self, command: str, args: dict, timeout: float = 15.0) -> dict: ...


class SimulatedDevice(DeviceBridge):
    """Python-side virtual phone used by tests and when no phone is connected."""

    def __init__(self):
        self.alarms: list[dict] = []
        self.timers: list[dict] = []
        self.reminders: list[dict] = []
        self.opened: list[str] = []
        self.calls: list[dict] = []
        self.sms: list[dict] = []
        self.settings = {"wifi": True, "bluetooth": False, "flashlight": False, "dnd": False, "silent": False}
        self.battery_level = 78
        self.location = {"lat": 12.9916, "lon": 80.2336, "name": "IIT Madras"}
        self.contacts = [
            {"name": "Arun Kumar", "phone": "+91 90000 11111", "email": "arun.demo@example.com"},
            {"name": "Priya S", "phone": "+91 90000 22222", "email": "priya.demo@example.com"},
            {"name": "Prof. Ravi", "phone": "+91 90000 33333", "email": "ravi.demo@example.com"},
            {"name": "Mom", "phone": "+91 90000 44444", "email": ""},
            {"name": "Kavya", "phone": "+91 90000 55555", "email": "kavya.demo@example.com"},
        ]
        self.notifications = [
            {"app": "WhatsApp", "title": "Arun", "text": "Lab meeting moved to 4?", "time": "21:50"},
            {"app": "HDFC Bank", "title": "Debit alert", "text": "Rs 450 debited from a/c XX1234", "time": "20:10"},
            {"app": "Messages", "title": "AX-SWIGGY", "text": "Your OTP is 482913. Do not share.", "time": "19:02"},
            {"app": "Gmail", "title": "Prof. Ravi", "text": "Thesis draft comments", "time": "18:30"},
            {"app": "GPay", "title": "Payment received", "text": "Rs 200 from Kavya", "time": "17:00"},
        ]
        self.apps = ["Spotify", "WhatsApp", "Gmail", "Maps", "Camera", "Chrome", "Slack", "GitHub", "GPay", "HDFC Bank"]
        self._connected = True

    @property
    def connected(self):
        return self._connected

    def call(self, command, args, timeout=15.0):
        if command not in COMMANDS:
            raise DeviceError(f"unknown command {command}")
        fn = getattr(self, f"_{command}")
        return fn(**args)

    def _set_alarm(self, hour, minute, label="Alarm", **_):
        a = {"id": uuid.uuid4().hex[:6], "hour": hour, "minute": minute, "label": label}
        self.alarms.append(a)
        return {"ok": True, "alarm": a}

    def _set_timer(self, seconds, label="Timer", **_):
        t = {"id": uuid.uuid4().hex[:6], "seconds": seconds, "label": label}
        self.timers.append(t)
        return {"ok": True, "timer": t}

    def _set_reminder(self, at_iso, label, **_):
        r = {"id": uuid.uuid4().hex[:6], "at": at_iso, "label": label}
        self.reminders.append(r)
        return {"ok": True, "reminder": r}

    def _open_app(self, app, **_):
        match = next((a for a in self.apps if a.lower() == app.lower() or app.lower() in a.lower()), None)
        if not match:
            return {"ok": False, "error": f"{app} is not installed"}
        self.opened.append(match)
        return {"ok": True, "app": match}

    def _call(self, number, name="", **_):
        self.calls.append({"number": number, "name": name})
        return {"ok": True, "note": "Dialer opened; you press call."}

    def _send_sms(self, number, text, name="", **_):
        self.sms.append({"number": number, "text": text, "name": name})
        return {"ok": True}

    def _find_contact(self, name, **_):
        q = name.lower().strip()
        hits = [c for c in self.contacts if q in c["name"].lower() or c["name"].lower().startswith(q)]
        return {"ok": True, "contacts": hits}

    def _get_location(self, **_):
        return {"ok": True, **self.location}

    def _read_notifications(self, limit=10, **_):
        return {"ok": True, "notifications": filter_notifications(self.notifications)[:limit]}

    def _toggle(self, setting, state, **_):
        if setting not in self.settings:
            return {"ok": False, "error": f"can't change {setting}"}
        self.settings[setting] = bool(state)
        return {"ok": True, "setting": setting, "state": bool(state)}

    def _battery(self, **_):
        return {"ok": True, "level": self.battery_level, "charging": False}


class RemoteDevice(DeviceBridge):
    """
    Bridge to a real phone (or the browser virtual phone) over the server's WebSocket.
    `send` pushes {"type": "device_command", id, command, args} to the client; the client
    answers with {"type": "device_result", id, result}. Thread-safe; the brain runs in a
    worker thread while the WebSocket lives on the event loop.
    """

    def __init__(self, send: Callable[[dict], None], fallback: Optional[DeviceBridge] = None):
        self._send = send
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self.fallback = fallback
        self._is_connected = False

    @property
    def connected(self):
        return self._is_connected

    def set_connected(self, value: bool):
        self._is_connected = value

    def call(self, command, args, timeout=15.0):
        if command not in COMMANDS:
            raise DeviceError(f"unknown command {command}")
        if not self._is_connected:
            if self.fallback:
                return self.fallback.call(command, args, timeout)
            raise DeviceError("Phone is not connected")
        cid = uuid.uuid4().hex[:10]
        ev, box = threading.Event(), {}
        self._pending[cid] = (ev, box)
        try:
            self._send({"type": "device_command", "id": cid, "command": command, "args": args})
            if not ev.wait(timeout):
                raise DeviceError(f"Phone did not answer {command} within {timeout:.0f}s")
            result = box.get("result", {})
            if command == "read_notifications":
                result["notifications"] = filter_notifications(result.get("notifications", []))
            return result
        finally:
            self._pending.pop(cid, None)

    def deliver(self, cid: str, result: dict) -> None:
        item = self._pending.get(cid)
        if item:
            item[1]["result"] = result
            item[0].set()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="minutes")
