# JARVIS-Style Multi-Agent Personal Assistant — Build Map

## Status: all 7 planned modules built, 126 tests passing (2 live-API tests skip without a key)

## What's real vs. mocked in this sandbox, and why

I don't have network access to OpenAI, Sarvam, Google Calendar, Telegram, or a hosted Neo4j
from this environment (only a fixed allowlist: Anthropic's API, GitHub, PyPI, npm). So the build
is structured to be **honest about that boundary**:

| Component | In this sandbox | In your real deployment |
|---|---|---|
| LLM backend | **Claude API** (real, tested — haiku tier for Scheduler/Memory/Router, sonnet tier for Planner) | Swap to GPT-4o-mini/GPT-4o/Ollama via one config value — `LLMClient` is provider-agnostic |
| Graph memory | **networkx**, real and fully tested — not a mock | Optionally swap to Neo4j later; networkx is a legitimate real choice at personal scale, not just a stand-in |
| Calendar | **Mock client**, same method signatures as the real Google Calendar API | Swap in real OAuth2 client — `CalendarClient` interface designed so this is a drop-in replacement |
| Scheduler agent | **Real** — LLM intent parsing + Guardrail-gated create/update/delete + conflict detection | No change needed |
| Memory agent | **Real** — LLM→structured-op translation (allow-listed), Guardrail-gated writes | No change needed |
| Planner agent | **Real** — LLM plan decomposition (sonnet tier); interrupt/replan logic is pure local code, no LLM | No change needed |
| Supervisor | **Real** — LangGraph `StateGraph` routing to Scheduler/Memory/Planner + a dedicated approval node | No change needed |
| Guardrail | **Real**, fully local, no external dependency at all | No change needed — this was always meant to run local |
| Telegram | **Not built** — out of scope for this pass; would slot in as a `CommunicationClient` interface + stub, same pattern as `CalendarClient` | Swap in real Bot API token |

Everything with a real implementation has real tests that actually execute and assert on
real behavior — nothing here is "trust me, it would work." The two live-API tests (one for
the LLM client, one supervisor end-to-end test) are marked `@pytest.mark.integration` and are
skipped automatically when `ANTHROPIC_API_KEY` isn't set, rather than failing.

## Build order (why this order) — ✅ all complete

1. ✅ **Guardrail** — zero external dependencies, purely local logic. Best first module: nothing
   to mock, fastest to get a real green test suite, and it's the safety layer everything else
   should be checked against from day one.
2. ✅ **Memory (graph)** — also fully local (networkx). Foundational — Scheduler, Planner, and
   the Memory Agent all read/write through this.
3. ✅ **LLM client abstraction** — one real Claude-backed implementation, swappable interface,
   with explicit per-agent cost tiering (haiku for structured/narrow tasks, sonnet for the
   Planner's genuine multi-step reasoning).
4. ✅ **Scheduler** — mocked Calendar client, real LLM-based intent parsing, Guardrail-gated
   writes, and conflict detection on create.
5. ✅ **Memory Agent (NL→query layer)** — real Claude calls translating natural language into
   an allow-listed structured operation, executed deterministically against the graph store;
   writes are Guardrail-gated the same way the Scheduler's are.
6. ✅ **Planner/Replanner** — Plan-and-Execute loop, priority-based interrupt logic (pure,
   deterministic, no LLM call for the interrupt decision itself).
7. ✅ **Supervisor** — LangGraph `StateGraph` wiring everything above into one routed graph,
   with a dedicated node for surfacing and resolving pending Guardrail approvals.
8. ✅ **Integration test** — one scripted end-to-end scenario touching every agent
   (`tests/test_integration_e2e.py`), asserting on state at each stage.
9. *(Stretch, not built this pass)* Telegram integration, Reflection agent, fine-tuned
   Communication agent, live hosting.

## Directory structure

```
jarvis-assistant/
├── README.md                  # this file
├── requirements.txt
├── .env.example                # where real API keys go
├── main.py                     # interactive CLI entry point
├── src/
│   ├── llm/client.py           # provider-agnostic LLM wrapper (Claude-backed here)
│   ├── memory/
│   │   ├── models.py            # Person, Meeting, Task, Commitment node/edge types
│   │   └── graph_store.py       # networkx-backed graph memory
│   ├── integrations/
│   │   └── calendar_client.py   # interface + mock impl (Google Calendar-shaped)
│   ├── agents/
│   │   ├── guardrail.py
│   │   ├── scheduler.py
│   │   ├── memory_agent.py
│   │   └── planner.py
│   └── supervisor.py            # LangGraph assembly
├── tests/
│   ├── conftest.py              # registers the `integration` pytest marker
│   ├── test_guardrail.py
│   ├── test_memory_graph.py
│   ├── test_llm_client.py
│   ├── test_calendar_client.py
│   ├── test_scheduler.py
│   ├── test_memory_agent.py
│   ├── test_planner.py
│   ├── test_supervisor.py
│   └── test_integration_e2e.py
```

## How to run it

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY
pytest tests/ -v                # run the full test suite (126 pass; 2 live-API tests
                                 # skip automatically without a key)
python main.py                  # interactive CLI to talk to the assistant
```

## What "learning this before an interview" should look like

Every module's docstring explains the *why*, not just the what — the design rationale behind
each significant choice (why the Guardrail isn't an LLM call, why `interrupt_and_replan` uses
strict-greater-than for ties, why `GraphMemoryStore.neighbors()` only follows outgoing edges and
what that means for how questions have to be phrased, why the LLM in both the Scheduler and
Memory Agent only ever produces a small validated structured object rather than anything
executed directly) is written into the code itself, not just this doc. Read the module
docstrings first, then the tests — the tests are written to demonstrate *why* a design choice
matters (most are named and commented around a specific failure mode they're proving doesn't
happen), not just that the happy path works.
