# JARVIS-Style Multi-Agent Personal Assistant — Build Map

## What's real vs. mocked in this sandbox, and why

I don't have network access to OpenAI, Sarvam, Google Calendar, Telegram, or a hosted Neo4j
from this environment (only a fixed allowlist: Anthropic's API, GitHub, PyPI, npm). So the build
is structured to be **honest about that boundary**:

| Component | In this sandbox | In your real deployment |
|---|---|---|
| LLM backend | **Claude API** (real, tested, live) | Swap to GPT-4o-mini/GPT-4o/Ollama via one config value — `LLMClient` is provider-agnostic |
| Graph memory | **networkx**, real and fully tested — not a mock | Optionally swap to Neo4j later; networkx is a legitimate real choice at personal scale, not just a stand-in |
| Calendar | **Mock client**, same method signatures as the real Google Calendar API | Swap in real OAuth2 client — `CalendarClient` interface designed so this is a drop-in replacement |
| Telegram | **Stub** (logs instead of sending) | Swap in real Bot API token |
| Guardrail | **Real**, fully local, no external dependency at all | No change needed — this was always meant to run local |

Everything with a real implementation has real tests that actually execute and assert on
real behavior — nothing here is "trust me, it would work."

## Build order (why this order)

1. **Guardrail** — zero external dependencies, purely local logic. Best first module: nothing
   to mock, fastest to get a real green test suite, and it's the safety layer everything else
   should be checked against from day one.
2. **Memory (graph)** — also fully local (networkx). Foundational — Scheduler, Planner, and
   Reflection all read/write through this.
3. **LLM client abstraction** — one real Claude-backed implementation, swappable interface.
4. **Scheduler** — mocked Calendar client, real LLM-based intent parsing.
5. **Memory Agent (NL→query layer)** — real Claude calls translating natural language into
   graph queries.
6. **Planner/Replanner** — Plan-and-Execute loop, priority-based interrupt logic.
7. **Supervisor** — LangGraph wiring everything above into one routed graph.
8. **Integration test** — one scripted end-to-end scenario touching every agent.
9. *(Stretch, later)* Reflection agent, fine-tuned Communication agent, live hosting.

## Directory structure

```
jarvis-assistant/
├── README.md                  # this file
├── requirements.txt
├── .env.example                # where real API keys go later
├── src/
│   ├── llm/client.py           # provider-agnostic LLM wrapper (Claude-backed here)
│   ├── memory/
│   │   ├── models.py            # Person, Meeting, Task, Commitment node/edge types
│   │   └── graph_store.py       # networkx-backed graph memory
│   ├── integrations/
│   │   ├── calendar_client.py   # interface + mock impl (Google Calendar-shaped)
│   │   └── telegram_client.py   # interface + stub impl
│   ├── agents/
│   │   ├── guardrail.py
│   │   ├── scheduler.py
│   │   ├── memory_agent.py
│   │   └── planner.py
│   └── supervisor.py            # LangGraph assembly
├── tests/
│   ├── test_guardrail.py
│   ├── test_memory_graph.py
│   ├── test_llm_client.py
│   ├── test_scheduler.py
│   ├── test_planner.py
│   └── test_integration_e2e.py
└── main.py                      # CLI entry point
```

## How to run it (once built)

```bash
cd jarvis-assistant
pip install -r requirements.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY (and later: OPENAI_API_KEY, etc.)
pytest tests/ -v                # run the full test suite
python main.py                  # interactive CLI to talk to the assistant
```

## What "learning this before an interview" should look like

As each module lands, I'll walk through: what it does, why it's structured that way, what the
tests actually verify (not just that they pass), and the one or two questions an interviewer is
most likely to ask about it. Building it correctly and understanding it are two different jobs —
this doc gets updated with both as we go, not just the former.
