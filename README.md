# JARVIS - a personal, voice-first, multi-agent assistant

Say "Hey JARVIS", talk or type, and JARVIS handles your calendar, email, Slack, GitHub,
reminders, places and phone, using only free models and free services. It acts on your phone
only through permissions you grant, and it never sends a message, cancels a meeting or forgets
something without showing you exactly what it will do first.

```
 Phone app / virtual phone  ── wake word (on-device) ─ speech-to-text ─ text ─┐   ┌─ text-to-speech
   permission prompts · approval cards · device actions                       │   │
 ─────────────────────────────── TLS WebSocket + device token ────────────────┼───┼──────────────
 Brain (LangGraph):  preprocess ─► split multi-intent ─► route (rules, then LLM) ─► agent ─► reply
   agents: Scheduler · Communication · Memory · Device · GitHub · Places · Briefing · Planner · Chat
   every side effect ─► Guardrail ─► Permission broker ─► Approval inbox ─► resource lock ─► audit
 ──────────────────────────────────────────────────────────────────────────────────────────────
 SQLite: knowledge graph · conversation history (full-text search) · approvals · grants · audit
```

## What's in this repo

| Path | What it is |
|---|---|
| `jarvis/` | **v2**: the brain, 9 agents, permission broker, approval queue, storage, integrations, voice, server |
| `jarvis/server/static/` | The phone UI. Runs as the **virtual phone** in a browser and, unchanged, inside the Android app |
| `mobile/` | Android app (Capacitor shell + native Java plugin: wake word, speech, biometrics, device actions) |
| `deploy/` | Free cloud deployment: Docker + Tailscale (private HTTPS, no open ports) on a free VM |
| `scripts/` | Model download, Google sign-in, LLM provider evaluation, wake-word reference port |
| `tests_v2/` | 146 tests: core, security, multi-agent scenarios, server, wake word, browser end-to-end |
| `src/`, `tests/` | **v1** (resume version): Supervisor, Scheduler, Memory, Planner, Guardrail. Still passing, reused by v2 |
| `docs/PLAN.md` | The plan, and the critique of it |
| `docs/DEPLOY.md` | Step by step: free VM, Tailscale, API keys, Google/Slack/GitHub, installing the APK |

## Run the virtual phone on your laptop (5 minutes)

```bash
pip install -r requirements-v2.txt
python scripts/download_models.py          # "hey jarvis" wake-word models, ~3 MB
cp .env.example .env                       # add GROQ_API_KEY (free) for real language understanding
python -m jarvis.server                    # prints the pairing token
```

Open http://127.0.0.1:8765 in Chrome, paste the token, then type, hold the mic, or switch on
"Hey JARVIS". With no API key it runs in offline mode (rule engine), which handles direct
commands; add a free Groq or Gemini key for everything else. The demo world (fake calendar,
inbox, contacts) is clearly marked with `example.com` addresses.

Try:
- "what's on my calendar today", "set up a meeting with Priya tomorrow at 11" (conflict, then pick a slot)
- "schedule a sync with Arun tomorrow at 5pm and mail him the agenda" (two agents, two approvals)
- "any unread mails?" (one email is a phishing/prompt-injection attempt; JARVIS flags and ignores it)
- "remember that Kavya introduced me to Arjun", then "who introduced me to Arjun?"
- "wake me up at 6:30", "read my notifications" (OTP and bank alerts are hidden), "open gpay" (refused)
- "jarvis lockdown" (revokes everything; unlocking needs a tap and typing UNLOCK)

## The agents

| Agent | Does | Safety |
|---|---|---|
| Supervisor (brain) | Routes with a keyword fast path (most requests need no LLM call), falls back to the LLM router, splits multi-intent requests, asks one question when unsure, carries facts between steps | Serialises turns; low confidence asks instead of guessing |
| Scheduler | List, check, create, move, cancel, find free slots; resolves clashes by offering the 3 nearest free slots that respect your preferences | Events with other people and all cancellations need approval |
| Communication | Inbox summaries, search, drafts, replies (threaded), Slack read/post | Never sends without an approval card; unknown recipients need on-screen confirmation; email bodies are untrusted data |
| Memory | Graph RAG over people, projects, tasks, commitments, places, preferences | Refuses passwords, OTPs, card/ID numbers; forgetting needs approval |
| Device | Alarms, timers, reminders, open apps, call (dialer), SMS (composer), contacts, location, notifications, quick settings, battery | Payment/banking/password apps blocked; OTP/bank notifications hidden twice (phone and server) |
| GitHub | Notifications, PRs, issues, CI status; create issues and comments | Writes need approval |
| Places | Find places, travel time, fair meeting point, when to leave for your next event | Location needs permission |
| Briefing | Day summary from sources you already allowed | Never triggers a burst of permission prompts |
| Planner | Breaks goals into steps, orders them by urgency x importance (v1 logic), runs them through the other agents | Each step still goes through permissions and approvals |
| Guardrail | Deterministic risk scoring (v1) plus v2 rules | Not an LLM, can't be talked out of its job |
| Chat | The JARVIS persona for everything else | No tools at all |

## Built for free, smaller models

- Fallback chain: Groq, then Gemini, then OpenRouter, then optional Ollama, then the offline rule
  engine. Rate-limited providers cool down automatically; identical calls are cached.
- The model only extracts what you said ("tomorrow at 4"); code does dates, ids and permissions.
- Every prompt follows one template: role, exact JSON shape, numbered rules, worked examples of the
  confusing cases, and what to do when unsure (`jarvis/prompts.py`).
- Pydantic validation with lenient fixes, then one repair round that shows the model its own error.
- `python scripts/eval_llm.py` scores each provider you have keys for on JARVIS's real prompts.

## Security, in short

Permission broker with Android-style prompts and a hard deny-list; approval inbox that is
idempotent, expiring and limited to editable fields; untrusted-content firebreak against prompt
injection; redaction of OTPs, cards, Aadhaar/PAN, passwords and tokens before anything is stored or
sent to an LLM; encrypted OAuth tokens; device-token auth with brute-force lockout; strict CSP;
lockdown switch; full audit log. On the phone: on-device wake word, Keystore storage,
`FLAG_SECURE`, HTTPS only, no SMS/call-log/storage/accessibility permissions. Details: `docs/PLAN.md` section 6.

## Tests

```bash
pytest                              # v1 + v2 (272 tests); browser e2e runs if Playwright is installed
python scripts/wakeword_reference.py tests_v2/data/hey_jarvis.wav   # verifies the Android wake-word port
```
