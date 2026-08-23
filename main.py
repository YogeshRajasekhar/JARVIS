"""
Interactive CLI entry point — wires the real Claude-backed LLM client, the
mock calendar, and the graph memory store into a Supervisor, and runs a
simple read-eval-print loop.

This is intentionally thin: all the actual logic lives in src/. This file's
only job is construction and I/O.
"""

import os
import sys

from src.agents.memory_agent import MemoryAgent
from src.agents.scheduler import SchedulerAgent
from src.integrations.calendar_client import MockCalendarClient
from src.llm.client import ClaudeLLMClient, HAIKU_MODEL, SONNET_MODEL, LLMAuthError
from src.memory.graph_store import GraphMemoryStore
from src.supervisor import Supervisor

MEMORY_PERSIST_PATH = "./data/memory_graph.json"


def build_supervisor() -> Supervisor:
    router_llm = ClaudeLLMClient(model=HAIKU_MODEL)
    scheduler_llm = ClaudeLLMClient(model=HAIKU_MODEL)
    memory_llm = ClaudeLLMClient(model=HAIKU_MODEL)
    planner_llm = ClaudeLLMClient(model=SONNET_MODEL)

    os.makedirs(os.path.dirname(MEMORY_PERSIST_PATH), exist_ok=True)
    memory_store = GraphMemoryStore(persist_path=MEMORY_PERSIST_PATH)
    calendar_client = MockCalendarClient()

    scheduler = SchedulerAgent(scheduler_llm, calendar_client, memory_store=memory_store)
    memory_agent = MemoryAgent(memory_llm, memory_store)

    return Supervisor(router_llm, planner_llm, scheduler, memory_agent), memory_store


def main() -> int:
    try:
        supervisor, memory_store = build_supervisor()
    except LLMAuthError as e:
        print(f"Couldn't start: {e}", file=sys.stderr)
        return 1

    print("JARVIS-style assistant. Type 'exit' to quit.")
    try:
        while True:
            try:
                user_input = input("> ").strip()
            except EOFError:
                break
            if user_input.lower() in ("exit", "quit"):
                break
            if not user_input:
                continue

            if supervisor.pending_approval is not None:
                if user_input.lower() in ("y", "yes"):
                    print(supervisor.respond_to_approval(True))
                elif user_input.lower() in ("n", "no"):
                    print(supervisor.respond_to_approval(False))
                else:
                    print("There's a pending action awaiting approval — reply yes or no.")
                continue

            response = supervisor.handle_message(user_input)
            print(response)
            if supervisor.pending_approval is not None:
                print("(reply yes/no to approve or discard this action)")
    finally:
        memory_store.save()

    return 0


if __name__ == "__main__":
    sys.exit(main())
