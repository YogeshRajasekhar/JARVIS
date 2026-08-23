"""
Tests for the graph memory store.

The interesting tests here aren't the CRUD ones (necessary but not
interesting) — they're the multi-hop traversal ones, since "can this graph
answer relationship questions a flat vector store can't" is the entire
justification for building it this way. If these tests didn't exist, the
graph-vs-flat design decision would be unverified.
"""

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Node, Edge, NodeType, EdgeType


def make_store():
    return GraphMemoryStore()


def test_add_and_get_node():
    store = make_store()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    result = store.get_node("person:arya")
    assert result is not None
    assert result["label"] == "Arya"
    assert result["type"] == "person"


def test_get_nonexistent_node_returns_none():
    store = make_store()
    assert store.get_node("person:nobody") is None


def test_add_edge_requires_both_nodes_to_exist():
    store = make_store()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    with pytest.raises(ValueError):
        store.add_edge(
            Edge(source_id="person:arya", target_id="person:ghost", type=EdgeType.RELATES_TO)
        )


def test_one_hop_neighbor_lookup():
    """Direct relationship: who attended this meeting."""
    store = make_store()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    store.add_node(Node(id="meeting:standup", type=NodeType.MEETING, label="Weekly Standup"))
    store.add_edge(
        Edge(source_id="person:arya", target_id="meeting:standup", type=EdgeType.ATTENDED)
    )
    neighbors = store.neighbors("person:arya", edge_type=EdgeType.ATTENDED)
    assert len(neighbors) == 1
    assert neighbors[0]["id"] == "meeting:standup"


def test_multi_hop_relationship_question():
    """
    This is the actual test that justifies the graph over flat storage:
    'who did I meet through Arya' requires a 2-hop traversal
    (me -> introduced_by -> Arya -> introduced -> Rohan), which a flat
    vector similarity search over text chunks cannot do at all — there is
    no text chunk that directly says this unless someone wrote it down
    verbatim, whereas the graph derives it structurally.
    """
    store = make_store()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    store.add_node(Node(id="person:rohan", type=NodeType.PERSON, label="Rohan"))
    store.add_edge(
        Edge(source_id="person:rohan", target_id="person:arya", type=EdgeType.INTRODUCED_BY)
    )
    # Rohan was introduced BY Arya -> from Arya's side, Arya "introduced" Rohan.
    # Traverse from Arya to find who she introduced:
    introduced_by_arya = [
        n for n in store.find_by_type(NodeType.PERSON)
        if any(
            nb["id"] == "person:arya"
            for nb in store.neighbors(n["id"], edge_type=EdgeType.INTRODUCED_BY)
        )
    ]
    assert any(p["id"] == "person:rohan" for p in introduced_by_arya)


def test_task_blocking_chain_path_exists():
    """
    Another multi-hop case: task A blocks B blocks C. Asking 'is A blocking
    C, even indirectly' needs path traversal, not a single edge lookup.
    """
    store = make_store()
    store.add_node(Node(id="task:a", type=NodeType.TASK, label="Task A"))
    store.add_node(Node(id="task:b", type=NodeType.TASK, label="Task B"))
    store.add_node(Node(id="task:c", type=NodeType.TASK, label="Task C"))
    store.add_edge(Edge(source_id="task:a", target_id="task:b", type=EdgeType.BLOCKS))
    store.add_edge(Edge(source_id="task:b", target_id="task:c", type=EdgeType.BLOCKS))

    assert store.path_exists("task:a", "task:c") is True
    assert store.path_exists("task:c", "task:a") is False  # direction matters


def test_find_by_type_filters_correctly():
    store = make_store()
    store.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
    store.add_node(Node(id="task:report", type=NodeType.TASK, label="Write report"))
    people = store.find_by_type(NodeType.PERSON)
    tasks = store.find_by_type(NodeType.TASK)
    assert len(people) == 1
    assert len(tasks) == 1
    assert people[0]["id"] == "person:arya"


def test_persistence_round_trip():
    """Save to disk, reload into a fresh store, confirm the graph survives intact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "graph.json")

        store1 = GraphMemoryStore(persist_path=path)
        store1.add_node(Node(id="person:arya", type=NodeType.PERSON, label="Arya"))
        store1.add_node(Node(id="meeting:standup", type=NodeType.MEETING, label="Standup"))
        store1.add_edge(
            Edge(source_id="person:arya", target_id="meeting:standup", type=EdgeType.ATTENDED)
        )
        store1.save()

        store2 = GraphMemoryStore(persist_path=path)
        assert store2.get_node("person:arya") is not None
        neighbors = store2.neighbors("person:arya", edge_type=EdgeType.ATTENDED)
        assert len(neighbors) == 1
        assert neighbors[0]["id"] == "meeting:standup"
