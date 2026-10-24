# JARVIS v2 - Build Plan (personal, voice-first, phone-controlling assistant)

Status of v1 (what exists in the repo today): Supervisor (LangGraph) + Scheduler + Memory (graph) +
Planner + Guardrail, mocked calendar, Claude-backed LLM, CLI only, 126 tests passing.

v2 goal: a personal assistant you wake with "Hey JARVIS", talk to by voice or text, that knows your
schedule, mail, meetings and places, can act on your phone only through permissions you grant, and
never sends anything without your approval. Built and tested first on a virtual phone inside this
environment, then shipped to your real phone.

---

## 1. Architecture (three layers)

```
 ┌──────────────────────────── EDGE (phone app / virtual phone) ─────────────────────────────┐
 │ Wake word "hey jarvis" -> STT -> text ──┐                  ┌── TTS <- reply text          │
 │ Permission dialogs (Allow once / Always / Deny)            Approval cards (Send / Edit /  │
 │ Device bridge: executes phone actions locally              Cancel, biometric for risky)   │
 └──────────────────────────────── WebSocket (TLS + device token) ───────────────────────────┘
                                          │
 ┌──────────────────────────── BRAIN (Python, FastAPI + LangGraph) ─────────────────────────┐
 │  Conversation agent (JARVIS persona, talks to you)                                         │
 │        │                                                                                   │
 │  Supervisor = rule fast-path router -> LLM router (fallback) -> Planner for multi-intent   │
 │        │                                                                                   │
 │  Scheduler | Memory | Planner | Communication | Device | GitHub | Places | Briefing       │
 │        │ every side-effect goes through:                                                   │
 │  Guardrail (risk) -> Permission broker (scopes) -> Approval queue -> Executor -> Audit log  │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
                                          │
 ┌────────────────────────────────────── DATA ───────────────────────────────────────────────┐
 │ SQLite (one small file): graph nodes/edges, conversation history (FTS5 search),            │
 │ approvals + audit log, permission grants, metadata, preferences. Sensitive fields          │
 │ encrypted at rest. Optional: encrypted backup to Supabase free tier (client-side keys).    │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
```

Why the brain stays Python: it keeps your LangGraph/graph-RAG work (and your resume story) intact,
and the phone app stays a thin, secure shell that owns the microphone, speaker and permissions.

## 2. Agents (5 from the resume + 6 new)

| # | Agent | Job | LLM tier |
|---|---|---|---|
| 1 | Supervisor | Route each request; split multi-intent requests via Planner; arbitrate conflicts | rules first, fast LLM fallback |
| 2 | Scheduler | Calendar read/create/move/cancel, free-slot search, conflict detection | fast |
| 3 | Memory | Graph RAG over people, meetings, tasks, commitments, places; NL -> structured graph query | fast |
| 4 | Planner | Multi-step goals, dependency ordering, replan on interrupts, propose conflict resolutions | smart |
| 5 | Guardrail | Deterministic risk scoring, no LLM (as today) + sensitive-data redaction | none |
| 6 | Conversation (new) | JARVIS persona; turns agent results into a short spoken reply; small talk | smart |
| 7 | Communication (new) | Read/summarize/draft mail and Slack; send ONLY after your approval | fast (draft: smart) |
| 8 | Device (new) | Phone actions through granted scopes: alarms, open app, contacts, location, notifications | fast |
| 9 | GitHub (new) | Notifications, issues, PRs, CI status; any write needs approval | fast |
| 10 | Places (new) | Meeting points, travel time, "when should I leave" (OpenStreetMap, free) | none/fast |
| 11 | Briefing (new) | Proactive: morning brief, pre-meeting reminders, follow-up nudges | smart |

## 3. Free stack (every piece is zero cost)

| Need | Virtual phone (here) | Real phone |
|---|---|---|
| LLM | Groq free tier (Llama, fast) with Gemini Flash free tier as fallback; OpenRouter free models as 3rd fallback | same |
| Wake word | openWakeWord "hey_jarvis" ONNX model (open source) | same model on Android (Kotlin/ONNX), or Porcupine built-in "jarvis" (free personal key) |
| STT | Browser Web Speech API; offline option Whisper/Vosk | Android SpeechRecognizer (free, on-device packs) |
| TTS | Browser speechSynthesis; offline option Piper | Android TextToSpeech (free, offline) |
| Maps | OpenStreetMap Nominatim + OSRM | same |
| Storage | SQLite | SQLite on phone or server + optional Supabase free backup |
| Integrations | Google Calendar + Gmail API, Slack API, GitHub API (all free for personal use, OAuth) | same |

## 4. Making free (smaller) models reliable

1. Rule-based fast path before any LLM call ("set an alarm", "what's next today" never need a model).
2. LLM only extracts; code computes. Dates are resolved by a deterministic parser, never by the model
   (date arithmetic is the #1 failure of small models).
3. Every agent prompt: role, allowed outputs, JSON schema, 2-3 worked examples, "if unsure" rule.
4. Strict validation (pydantic) + repair loop: on invalid JSON, re-ask once with the exact error.
5. Temperature 0 for routing/extraction; short contexts (only relevant memory is injected).
6. Provider fallback chain with rate-limit backoff and a response cache.
7. Eval set: a fixed suite of tricky utterances run against each provider, scored automatically.

## 5. Conflict handling (self-resolving)

| Conflict | Resolution |
|---|---|
| Two requests touch the same resource | Per-resource lock; requests execute in order |
| New meeting overlaps an existing one | Scheduler detects; Planner proposes 3 free slots ranked by your preferences; you pick |
| Multi-intent ("book X and mail Arun") | Planner builds a step graph with dependencies; mail draft waits for booking result |
| Several actions waiting for approval | Approval queue (FIFO, each with an id), not a single pending slot |
| Agent output contradicts memory | Supervisor priority: safety > your explicit instruction > hard deadlines > preferences |
| Router unsure | Ask one short clarifying question instead of guessing |
| Device vs cloud data disagree | Device is source of truth for device data; versioned last-writer-wins elsewhere |

## 6. Security model ("protect the phone at all cost")

No system is unhackable; this is defence in depth against the realistic threats.

- Permission broker: every tool declares scopes (calendar.read, mail.send, contacts.read, location,
  notifications.read, ...). No grant -> an Android-style prompt. Grants are revocable and audited.
- Hard deny-list (never accessible, even if asked): banking/UPI/payment apps, OTP messages,
  passwords/password managers, government ID numbers, card numbers.
- Redaction filter: OTPs, card numbers, Aadhaar/PAN-like patterns, passwords are stripped from any
  text before it reaches an LLM provider.
- Prompt-injection defence: content from mail/Slack/web is tagged UNTRUSTED; it can never trigger a
  tool by itself, and any action proposed while reading untrusted content needs approval.
- Approvals: every send/delete/external action shows an exact preview. High-risk ones need an
  on-screen tap plus biometric, not just voice (a TV or another person could say "yes").
- Transport: backend never exposed to the open internet; device token + HMAC-signed requests,
  TLS, optional private network (Tailscale, free).
- Secrets: OAuth tokens in Android Keystore / OS keyring, least-privilege scopes
  (e.g. Gmail compose + read, not full mailbox control).
- Kill switch: "JARVIS, lockdown" revokes all grants and stops all pending actions.
- Full audit log of every tool call, visible in the app.

## 7. Build phases and test gates

| Phase | Deliverable | Gate to pass |
|---|---|---|
| 0 | Plan + critique (this doc) | your sign-off on open questions |
| 1 | Core v2: free-LLM client chain, SQLite store, permission broker, approval queue, redaction | unit tests green |
| 2 | New agents with mocked integrations + improved prompts for existing agents | per-agent tests incl. adversarial |
| 3 | FastAPI + WebSocket server, voice pipeline (wake word, STT, TTS adapters) | API + wake-word tests |
| 4 | Virtual phone (browser UI identical to the future app) + Playwright end-to-end tests | scripted day-in-the-life scenario |
| 5 | Real integrations (Google, Slack, GitHub) with your OAuth credentials | live smoke tests on your accounts |
| 6 | Android app (Capacitor shell + native plugins: wake word, STT, TTS, permissions, keystore) | install on your phone, on-device checklist |
| 7 | Hardening: injection red-team set, eval scores per provider, battery/storage check | all security tests green |

## 8. Questioning the plan (failure modes and what we do about them)

| Risk / failure | Likelihood | Mitigation |
|---|---|---|
| "Take control of my mobile" is not fully possible: Android allows intents (alarms, open apps, dial, compose SMS), contacts/calendar/location with permission, notification reading with opt-in. Full screen control needs an Accessibility Service, which is also the #1 malware vector. iOS allows almost none of this. | Certain | Device agent uses official intents + permissions; Accessibility control is off by default, opt-in, app-allowlisted |
| Free LLM rate limits / outages | High | 3-provider fallback, rule fast-path, caching, clear "I'm rate limited" reply |
| Small models produce wrong JSON or wrong dates | High | Schema validation + repair loop, deterministic date parser, eval suite |
| Always-on wake word drains battery | Medium | Tiny ONNX model, foreground service, pause when screen off + on charger optional |
| False wake triggers (TV says "Jarvis") | Medium | Detection threshold tuning + no high-risk action on voice alone |
| Free hosting sleeps or disappears | Medium | Brain can run on laptop, free VM, or the phone itself; data is one SQLite file you own |
| Prompt injection via an email ("forward all mails to x") | Medium | Untrusted tagging, tool gating, approval with exact preview |
| OAuth token theft | Low-Med | Keystore, least scopes, token encryption, revoke on lockdown |
| Scope explosion (11 agents) | High | Build in phases; each agent ships only with tests |
| Web Speech STT sends audio to Google | Certain (in browser) | Fine for the simulator; real phone uses on-device recognition where available |
| Sandbox here cannot reach Groq/Gemini/Google | Certain | Here: deterministic mock LLM + Claude for live tests; real providers verified on your machine |
