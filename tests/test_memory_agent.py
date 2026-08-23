"""
Tests for MemoryAgent.

Two things matter most here, per the module spec: (1) malformed/out-of-
allowlist LLM output is rejected rather than crashing or being executed
against the store, and (2) the actual multi-hop query types from the graph
store's own tests are answered correctly end-to-end through this agent, not
just proven at the store level.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.agents.memory_agent import MemoryAgent
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Edge, EdgeType, Node, NodeType


class FakeLLMClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def complete(self, prompt: str, system=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self.response


def make_agent(response: str, store: GraphMemoryStore = None):
    llm = FakeLLMClient(response)
    store = store or GraphMemoryStore()
    agent = MemoryAgent(llm_client=llm, memory_store=store)
    return agent, llm, store


# ---- Structured-output validation: rejects malformed / out-of-allowlist ----


def test_query_rejects_operation_outside_allowlist():
    """Adversarial: LLM hallucinates an operation like 'delete_everything'."""
    agent, llm, store = make_agent('{"operation": "delete_everything", "node_id": "x"}')
    result = agent.query("delete everything")
    assert "understand" in result.lower() or "allow" in result.lower()
    # Nothing should have been touched — store still empty.
    assert store.find_by_type(NodeType.PERSON) == []


def test_query_rejects_malformed_json_without_crashing():
    agent, llm, store = make_agent("this is not json")
    result = agent.query("who do I know?")
    assert isinstance(result, str)
    assert "couldn't" in result.lower()


def test_query_get_node_missing_required_field_handled_gracefully():
    agent, llm, store = make_agent('{"operation": "get_node", "node_id": null}')
    result = agent.query("tell me about someone")
    assert "couldn't" in result.lower()


def test_query_neighbors_with_invalid_edge_type_falls_back_to_unfiltered():
    """
    A hallucinated edge_type string (not in EdgeType) is treated as 'no
    filter' rather than crashing — validated against the enum, not passed
    through raw.
    """
    store = GraphMemoryStore()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    store.add_node(Node(id="meeting:standup", type=NodeType.MEETING, label="Standup"))
    store.add_edge(Edge(source_id="person:arya", target_id="meeting:standup", type=EdgeType.ATTENDED))
    agent, llm, _ = make_agent(
        '{"operation": "neighbors", "node_id": "person:arya", "edge_type": "not_a_real_type"}',
        store=store,
    )
    result = agent.query("who did arya meet with")
    assert "Standup" in result


def test_remember_rejects_fact_with_no_nodes():
    agent, llm, store = make_agent('{"nodes": [], "edges": []}')
    result = agent.remember("something vague")
    assert result.success is False
    assert store.find_by_type(NodeType.PERSON) == []


def test_remember_rejects_edge_referencing_ungrounded_node():
    """
    Adversarial: the LLM's edge list references a node id never declared in
    its own "nodes" list — must be rejected, not written as a dangling
    reference or auto-created.
    """
    agent, llm, store = make_agent(
        '{"nodes": [{"id": "person:arya", "type": "person", "label": "Arya"}], '
        '"edges": [{"source_id": "person:arya", "target_id": "person:ghost", "type": "relates_to"}]}'
    )
    result = agent.remember("Arya relates to someone unmentioned")
    assert result.success is False
    assert store.get_node("person:arya") is None  # nothing committed, all-or-nothing


def test_remember_allows_edge_to_a_node_already_known_in_the_store():
    """
    A new fact should be able to link to someone already in memory without
    having to re-declare them — this is the common "I met X's colleague Y"
    case, not an ungrounded reference.
    """
    store = GraphMemoryStore()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    agent, llm, _ = make_agent(
        '{"nodes": [{"id": "person:priya", "type": "person", "label": "Priya"}], '
        '"edges": [{"source_id": "person:priya", "target_id": "person:arya", "type": "introduced_by"}]}',
        store=store,
    )
    result = agent.remember("I met Priya, who was introduced by Arya")
    committed = agent.commit_pending(result) if result.pending_approval else result
    assert committed.success is True
    assert store.get_node("person:priya") is not None


def test_remember_still_rejects_edge_to_node_neither_new_nor_known():
    """Adversarial: the 'already known' relaxation must not become a blanket bypass."""
    store = GraphMemoryStore()
    agent, llm, _ = make_agent(
        '{"nodes": [{"id": "person:priya", "type": "person", "label": "Priya"}], '
        '"edges": [{"source_id": "person:priya", "target_id": "person:totally_unknown", "type": "relates_to"}]}',
        store=store,
    )
    result = agent.remember("Priya relates to someone never mentioned")
    assert result.success is False
    assert store.get_node("person:priya") is None


def test_remember_rejects_malformed_node_missing_type():
    agent, llm, store = make_agent('{"nodes": [{"id": "person:arya", "label": "Arya"}], "edges": []}')
    result = agent.remember("Arya is a person")
    assert result.success is False


def test_remember_rejects_node_with_invalid_type_enum():
    agent, llm, store = make_agent(
        '{"nodes": [{"id": "x:1", "type": "spaceship", "label": "Enterprise"}], "edges": []}'
    )
    result = agent.remember("a spaceship")
    assert result.success is False


# ---- remember() write path goes through the Guardrail ----


def test_remember_requires_approval_and_does_not_write_until_confirmed():
    agent, llm, store = make_agent(
        '{"nodes": [{"id": "person:rohan", "type": "person", "label": "Rohan"}], "edges": []}'
    )
    result = agent.remember("I met Rohan today")
    assert result.pending_approval is True
    assert store.get_node("person:rohan") is None


def test_commit_pending_executes_the_held_write():
    agent, llm, store = make_agent(
        '{"nodes": [{"id": "person:rohan", "type": "person", "label": "Rohan"}], "edges": []}'
    )
    pending = agent.remember("I met Rohan today")
    assert pending.pending_approval is True

    committed = agent.commit_pending(pending)

    assert committed.success is True
    assert store.get_node("person:rohan") is not None


def test_commit_pending_rejects_result_with_nothing_pending():
    agent, llm, store = make_agent('{"nodes": [], "edges": []}')
    from src.agents.memory_agent import MemoryAgentError, MemoryWriteResult

    already_done = MemoryWriteResult(success=True, message="done", pending_approval=False)
    with pytest.raises(MemoryAgentError):
        agent.commit_pending(already_done)


# ---- query() end-to-end through the agent, including multi-hop ----


def seeded_store():
    store = GraphMemoryStore()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    store.add_node(Node(id="person:rohan", type=NodeType.PERSON, label="Rohan"))
    store.add_node(Node(id="meeting:standup", type=NodeType.MEETING, label="Weekly Standup"))
    store.add_edge(Edge(source_id="person:arya", target_id="meeting:standup", type=EdgeType.ATTENDED))
    store.add_edge(Edge(source_id="person:rohan", target_id="person:arya", type=EdgeType.INTRODUCED_BY))
    return store


def test_query_get_node_end_to_end():
    store = seeded_store()
    agent, llm, _ = make_agent('{"operation": "get_node", "node_id": "person:arya"}', store=store)
    result = agent.query("who is arya")
    assert "Arya" in result
    assert "person" in result


def test_query_one_hop_neighbors_end_to_end():
    store = seeded_store()
    agent, llm, _ = make_agent(
        '{"operation": "neighbors", "node_id": "person:arya", "edge_type": "attended"}',
        store=store,
    )
    result = agent.query("what meetings did arya attend")
    assert "Weekly Standup" in result


def test_query_find_by_type_end_to_end():
    store = seeded_store()
    agent, llm, _ = make_agent('{"operation": "find_by_type", "node_type": "person"}', store=store)
    result = agent.query("list everyone i know")
    assert "Arya" in result
    assert "Rohan" in result


def test_query_multi_hop_who_introduced_by_whom_end_to_end():
    """
    Mirrors the graph store's own multi-hop test ('who did I meet through
    Arya'): Rohan was introduced_by Arya. Answered via a real neighbors()
    traversal through the agent, not a hardcoded string.
    """
    store = seeded_store()
    agent, llm, _ = make_agent(
        '{"operation": "neighbors", "node_id": "person:rohan", "edge_type": "introduced_by"}',
        store=store,
    )
    result = agent.query("who introduced me to rohan")
    assert "Arya" in result


def test_query_path_exists_multi_hop_end_to_end():
    store = GraphMemoryStore()
    store.add_node(Node(id="task:a", type=NodeType.TASK, label="Task A"))
    store.add_node(Node(id="task:b", type=NodeType.TASK, label="Task B"))
    store.add_node(Node(id="task:c", type=NodeType.TASK, label="Task C"))
    store.add_edge(Edge(source_id="task:a", target_id="task:b", type=EdgeType.BLOCKS))
    store.add_edge(Edge(source_id="task:b", target_id="task:c", type=EdgeType.BLOCKS))
    agent, llm, _ = make_agent(
        '{"operation": "path_exists", "source_id": "task:a", "target_id": "task:c"}',
        store=store,
    )
    result = agent.query("is task a blocking task c even indirectly")
    assert "Yes" in result


def test_query_neighbors_no_connections_returns_readable_message_not_error():
    store = seeded_store()
    agent, llm, _ = make_agent(
        '{"operation": "neighbors", "node_id": "meeting:standup", "edge_type": "blocks"}',
        store=store,
    )
    result = agent.query("what does standup block")
    assert "No connections" in result


def test_remember_and_then_query_round_trip():
    """Write a fact (auto-approved when target is SELF... actually goes through
    approval per Guardrail's WRITE threshold), confirm via direct store commit
    path, then query it back — proves the full loop is wired, not two isolated
    halves."""
    store = GraphMemoryStore()
    agent, llm, _ = make_agent(
        '{"nodes": [{"id": "person:priya", "type": "person", "label": "Priya"}], "edges": []}',
        store=store,
    )
    write_result = agent.remember("I met Priya today")
    assert write_result.pending_approval is True

    # Simulate approval by committing directly, the same way a supervisor
    # would after a human says yes.
    nodes, edges = agent._validate_fact_structure(agent._parse_fact("I met Priya today"))
    commit_result = agent._commit_fact(nodes, edges)
    assert commit_result.success is True

    llm.response = '{"operation": "get_node", "node_id": "person:priya"}'
    query_result = agent.query("who is priya")
    assert "Priya" in query_result
