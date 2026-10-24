"""
Scores each configured free LLM provider on JARVIS's real prompts, so you can pick (and order)
providers with evidence instead of guessing.

  python scripts/eval_llm.py            # every provider with a key in .env, plus the offline engine
  python scripts/eval_llm.py --only groq

Measures: routing accuracy, extraction accuracy, valid-JSON rate (before repair), median latency.
Uses ~120 requests per provider (well inside free daily limits).
"""

import argparse
import statistics
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests_v2")

from jarvis.config import Settings  # noqa: E402
from jarvis.llm.providers import GeminiLLM, OpenAICompatibleLLM  # noqa: E402
from jarvis.llm.rule_llm import RuleLLM  # noqa: E402
from jarvis.llm.schemas import CalendarOut, DeviceOut, MessageOut, RouteOut  # noqa: E402
from jarvis.llm.structured import complete_json, parse_model  # noqa: E402
from jarvis import prompts  # noqa: E402
from test_brain import ROUTING_SET  # noqa: E402

EXTRACT = [
    (CalendarOut, prompts.CALENDAR, "calendar", "schedule a call with Arun tomorrow at 4pm for 30 minutes",
     lambda o: o.action == "create" and "4" in (o.when or "") and "tomorrow" in (o.when or "").lower() and o.duration_minutes == 30),
    (CalendarOut, prompts.CALENDAR, "calendar", "push my standup to 11", lambda o: o.action == "move" and "standup" in (o.target or "").lower()),
    (CalendarOut, prompts.CALENDAR, "calendar", "cancel the lab meeting on friday", lambda o: o.action == "cancel"),
    (CalendarOut, prompts.CALENDAR, "calendar", "what do I have next monday", lambda o: o.action == "list" and "monday" in (o.when or "").lower()),
    (CalendarOut, prompts.CALENDAR, "calendar", "find me two free hours tomorrow afternoon", lambda o: o.action == "find_slot" and o.duration_minutes == 120),
    (DeviceOut, prompts.DEVICE, "device", "wake me up at 6:30", lambda o: o.action == "set_alarm" and "6:30" in (o.time or "")),
    (DeviceOut, prompts.DEVICE, "device", "remind me to submit the visa form tomorrow at 9am", lambda o: o.action == "set_reminder" and "visa" in (o.label or "").lower()),
    (DeviceOut, prompts.DEVICE, "device", "turn off bluetooth", lambda o: o.action == "toggle" and o.setting == "bluetooth" and o.state is False),
    (DeviceOut, prompts.DEVICE, "device", "text Priya that I'm 10 minutes late", lambda o: o.action == "send_sms" and "priya" in (o.contact or "").lower()),
    (MessageOut, prompts.MESSAGE, "message", "email Prof. Ravi that the revised draft will reach him by Thursday",
     lambda o: o.action == "draft_email" and any("ravi" in t.lower() for t in o.to) and "thursday" in (o.intent or "").lower()),
    (MessageOut, prompts.MESSAGE, "message", "any unread mails from TU Darmstadt?", lambda o: o.action in ("read_inbox", "search_email")),
    (MessageOut, prompts.MESSAGE, "message", "post in #jarvis-dev that the APK build is green", lambda o: o.action == "draft_slack" and o.channel == "jarvis-dev"),
]


def providers(s: Settings, only: str | None):
    out = []
    if s.groq_api_key:
        g = "https://api.groq.com/openai/v1"
        out += [OpenAICompatibleLLM(f"groq:{m}", g, s.groq_api_key, m) for m in (s.groq_fast_model, s.groq_smart_model)]
    if s.gemini_api_key:
        out += [GeminiLLM(s.gemini_api_key, m) for m in (s.gemini_fast_model, s.gemini_smart_model)]
    if s.openrouter_api_key:
        out.append(OpenAICompatibleLLM(f"openrouter:{s.openrouter_model}", "https://openrouter.ai/api/v1", s.openrouter_api_key, s.openrouter_model))
    if s.ollama_url:
        out.append(OpenAICompatibleLLM(f"ollama:{s.ollama_model}", s.ollama_url.rstrip("/") + "/v1", "", s.ollama_model, timeout=120))
    out.append(RuleLLM())
    return [p for p in out if not only or only in p.name]


def evaluate(llm) -> dict:
    lat, valid, route_ok, ext_ok = [], 0, 0, 0
    total = len(ROUTING_SET) + len(EXTRACT)
    for text, expected in ROUTING_SET:
        t0 = time.time()
        raw = llm.complete(f"Request: {text}", system=prompts.ROUTER, json=True, task="route")
        lat.append(time.time() - t0)
        try:
            parse_model(raw, RouteOut)
            valid += 1
        except Exception:
            pass
        try:
            route_ok += complete_json(llm, RouteOut, prompts.ROUTER, f"Request: {text}", task="route").agent == expected
        except Exception:
            pass
    for model, system, task, text, check in EXTRACT:
        prompt = f"Current time: Saturday 24 October 2026, 09:00 AM\n\nRequest: {text}"
        t0 = time.time()
        try:
            o = complete_json(llm, model, system, prompt, task=task)
            valid += 1
            ext_ok += bool(check(o))
        except Exception:
            pass
        lat.append(time.time() - t0)
    return {"routing": route_ok / len(ROUTING_SET), "extraction": ext_ok / len(EXTRACT),
            "valid_json": valid / total, "p50_latency_s": statistics.median(lat)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    a = ap.parse_args()
    print(f"{'provider':45} routing  extract  json   p50")
    for p in providers(Settings(), a.only):
        try:
            r = evaluate(p)
            print(f"{p.name:45} {r['routing']:6.0%}  {r['extraction']:6.0%}  {r['valid_json']:5.0%}  {r['p50_latency_s']:.2f}s")
        except Exception as e:
            print(f"{p.name:45} failed: {e}")


if __name__ == "__main__":
    main()
