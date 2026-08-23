"""
Graph memory store, backed by networkx.

Design note: networkx here is a genuine engineering choice, not a stand-in
for "the real thing." At personal scale (dozens to low hundreds of people/
meetings/tasks, single-user, no concurrent multi-writer access), an in-
process graph with periodic persistence to disk is simpler to run, debug,
and reason about than standing up a Neo4j server — and it does everything
this project's actual query patterns need. Neo4j becomes the right call at
a scale/concurrency level this project doesn't operate at. That's a
defensible engineering tradeoff to state plainly if asked, not a
compromise to downplay.

Persistence: serialized to a JSON file via networkx's node-link format.
Good enough for a personal assistant that isn't handling concurrent writers.
"""

import json
import os
from typing import Optional

import networkx as nx

from src.memory.models import Node, Edge, NodeType, EdgeType


class GraphMemoryStore:
    def __init__(self, persist_path: Optional[str] = None):
        self.graph = nx.MultiDiGraph()  # directed, allows multiple edge types between same pair
        self.persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            self._load()

    # ---- Write operations ----

    def add_node(self, node: Node) -> None:
        self.graph.add_node(
            node.id,
            type=node.type.value,
            label=node.label,
            attributes=node.attributes,
            created_at=node.created_at.isoformat(),
        )

    def add_edge(self, edge: Edge) -> None:
        if edge.source_id not in self.graph or edge.target_id not in self.graph:
            raise ValueError(
                f"Cannot add edge: both '{edge.source_id}' and '{edge.target_id}' "
                "must already exist as nodes."
            )
        self.graph.add_edge(
            edge.source_id,
            edge.target_id,
            type=edge.type.value,
            attributes=edge.attributes,
            created_at=edge.created_at.isoformat(),
        )

    # ---- Read operations ----

    def get_node(self, node_id: str) -> Optional[dict]:
        if node_id not in self.graph:
            return None
        return {"id": node_id, **self.graph.nodes[node_id]}

    def neighbors(self, node_id: str, edge_type: Optional[EdgeType] = None) -> list[dict]:
        """
        1-hop neighbors of a node, optionally filtered to one edge type.
        This is the core traversal operation — e.g. "who did I meet through
        this person" is neighbors(person_id, EdgeType.INTRODUCED_BY).
        """
        if node_id not in self.graph:
            return []
        results = []
        for _, target_id, data in self.graph.out_edges(node_id, data=True):
            if edge_type is None or data.get("type") == edge_type.value:
                node_data = self.get_node(target_id)
                if node_data:
                    results.append({**node_data, "_edge_type": data.get("type")})
        return results

    def find_by_type(self, node_type: NodeType) -> list[dict]:
        return [
            {"id": n, **d}
            for n, d in self.graph.nodes(data=True)
            if d.get("type") == node_type.value
        ]

    def path_exists(self, source_id: str, target_id: str) -> bool:
        """Used for multi-hop questions like 'is this task blocking that one, even indirectly'."""
        if source_id not in self.graph or target_id not in self.graph:
            return False
        return nx.has_path(self.graph, source_id, target_id)

    # ---- Persistence ----

    def save(self) -> None:
        if not self.persist_path:
            return
        data = nx.node_link_data(self.graph, edges="edges")
        with open(self.persist_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> None:
        with open(self.persist_path, "r") as f:
            data = json.load(f)
        self.graph = nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")
