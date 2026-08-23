"""
Memory Agent — natural language in, structured graph operations out.

Design rationale:
- The LLM (haiku tier — this is translation, not reasoning) never touches
  the graph directly. It produces a small structured intermediate
  representation (a JSON object naming an operation and its arguments),
  which is validated against an allow-list of operations and required
  fields *before* anything runs against `GraphMemoryStore`. This is the
  same non-negotiable boundary used in the Scheduler's intent parsing:
  an LLM is trusted to extract meaning from text, never trusted to author
  code or queries that get executed as-is. Concretely, this rules out any
  version of this agent that does `eval()` or builds a Cypher-like query
  string from LLM output — the operation set is closed and small enough
  that closed dispatch is both safer and simpler than an open query
  language would be.
- `query()` is read-only by construction — it only ever calls read methods
  on the store (`get_node`, `neighbors`, `find_by_type`, `path_exists`), so
  it never needs a Guardrail check. `remember()` is the only write path;
  it goes through `GuardrailAgent` exactly like the Scheduler's writes,
  because "write a new fact into permanent memory" is exactly the kind of
  state-changing action the Guardrail exists to mediate.
"""

import json
import re
from dataclasses import dataclass
from typing import Optional

from src.agents.guardrail import (
    ActionRequest,
    ActionSeverity,
    GuardrailAgent,
    TargetSensitivity,
    Verdict,
)
from src.llm.client import LLMClient
from src.memory.graph_store import GraphMemoryStore
from src.memory.models import Edge, EdgeType, Node, NodeType

# Closed set of read operations the LLM's structured output may request.
# Anything outside this set is rejected before touching the store.
ALLOWED_QUERY_OPERATIONS = {"get_node", "neighbors", "find_by_type", "path_exists"}

QUERY_SYSTEM_PROMPT = """You translate natural language questions about a personal
graph memory into ONE structured JSON query. Respond with ONLY a JSON object
(no prose, no markdown fences) with this shape:

{
  "operation": "get_node" | "neighbors" | "find_by_type" | "path_exists",
  "node_id": string or null,
  "node_type": "person" | "meeting" | "task" | "commitment" or null,
  "edge_type": "relates_to" | "follows_up" | "blocks" | "introduced_by" | "attended" | "owes" or null,
  "source_id": string or null,
  "target_id": string or null
}

Node ids look like "person:name" or "meeting:name" (lowercase, spaces as
underscores). Use "get_node" for a single node lookup by id, "neighbors" for
1-hop relationship questions (optionally filtered by edge_type), "find_by_type"
to list all nodes of a type, and "path_exists" for "is X connected to Y"
questions (requires source_id and target_id). Always return every key, using
null for anything not applicable to the chosen operation."""

REMEMBER_SYSTEM_PROMPT = """You extract a fact into graph nodes/edges for a
personal memory system. Respond with ONLY a JSON object (no prose, no markdown
fences) with this shape:

{
  "nodes": [
    {"id": string, "type": "person"|"meeting"|"task"|"commitment", "label": string}
  ],
  "edges": [
    {"source_id": string, "target_id": string,
     "type": "relates_to"|"follows_up"|"blocks"|"introduced_by"|"attended"|"owes"}
  ]
}

Node ids look like "person:name" or "task:short_description" (lowercase,
spaces as underscores). Every edge's source_id/target_id MUST reference a
node also listed in "nodes" (or omit the edge if you can't ground it).
If the fact describes only one entity with no relationship, "edges" can be
an empty list."""


class MemoryAgentError(Exception):
    """Raised when the LLM's structured output is malformed or outside the allow-list."""


@dataclass
class MemoryWriteResult:
    success: bool
    message: str
    pending_approval: bool = False
    nodes_written: list[str] = None
    edges_written: int = 0
    # Set only when pending_approval is True, so a caller (e.g. the
    # Supervisor's approval node) can commit the already-validated fact
    # after a human approves, without re-invoking the LLM.
    pending_nodes: Optional[list[Node]] = None
    pending_edges: Optional[list[Edge]] = None

    def __post_init__(self):
        if self.nodes_written is None:
            self.nodes_written = []


def _extract_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise MemoryAgentError(f"No JSON object found in LLM response: {raw!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise MemoryAgentError(f"Malformed JSON from LLM: {e}") from e


class MemoryAgent:
    def __init__(self, llm_client: LLMClient, memory_store: GraphMemoryStore):
        self.llm_client = llm_client
        self.memory_store = memory_store
        self.guardrail = GuardrailAgent()

    # ---- Read path: query() ----

    def query(self, natural_language_question: str) -> str:
        """
        Translates the question to a validated structured operation, executes
        it deterministically, and formats the result as a plain-English
        string. Never falls through to an unvalidated call — a rejected
        operation returns an explanatory string, it doesn't raise past this
        boundary into caller code that wasn't expecting an exception.
        """
        try:
            structured = self._parse_query(natural_language_question)
        except MemoryAgentError as e:
            return f"Couldn't understand that as a memory query: {e}"

        try:
            return self._execute_query(structured)
        except MemoryAgentError as e:
            return f"Couldn't answer that: {e}"

    def _parse_query(self, question: str) -> dict:
        raw = self.llm_client.complete(question, system=QUERY_SYSTEM_PROMPT)
        data = _extract_json(raw)

        operation = data.get("operation")
        if operation not in ALLOWED_QUERY_OPERATIONS:
            raise MemoryAgentError(
                f"Operation {operation!r} is not in the allow-list {ALLOWED_QUERY_OPERATIONS}."
            )
        return data

    def _execute_query(self, structured: dict) -> str:
        operation = structured["operation"]

        if operation == "get_node":
            node_id = structured.get("node_id")
            if not node_id:
                raise MemoryAgentError("get_node requires node_id.")
            node = self.memory_store.get_node(node_id)
            if node is None:
                return f"No node found with id {node_id!r}."
            return f"{node['label']} ({node['type']}): {node.get('attributes', {})}"

        if operation == "neighbors":
            node_id = structured.get("node_id")
            if not node_id:
                raise MemoryAgentError("neighbors requires node_id.")
            edge_type = self._parse_edge_type(structured.get("edge_type"))
            results = self.memory_store.neighbors(node_id, edge_type=edge_type)
            if not results:
                return f"No connections found for {node_id!r}."
            labels = ", ".join(n["label"] for n in results)
            return f"Connected to: {labels}"

        if operation == "find_by_type":
            node_type = self._parse_node_type(structured.get("node_type"))
            if node_type is None:
                raise MemoryAgentError("find_by_type requires a valid node_type.")
            results = self.memory_store.find_by_type(node_type)
            if not results:
                return f"No {node_type.value} nodes found."
            labels = ", ".join(n["label"] for n in results)
            return f"Found {len(results)} {node_type.value}(s): {labels}"

        if operation == "path_exists":
            source_id = structured.get("source_id")
            target_id = structured.get("target_id")
            if not source_id or not target_id:
                raise MemoryAgentError("path_exists requires source_id and target_id.")
            exists = self.memory_store.path_exists(source_id, target_id)
            return f"{'Yes' if exists else 'No'}, {source_id!r} {'is' if exists else 'is not'} connected to {target_id!r}."

        # Unreachable: _parse_query already validated against ALLOWED_QUERY_OPERATIONS.
        raise MemoryAgentError(f"Unhandled operation: {operation!r}")

    @staticmethod
    def _parse_edge_type(value: Optional[str]) -> Optional[EdgeType]:
        if value is None:
            return None
        try:
            return EdgeType(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_node_type(value: Optional[str]) -> Optional[NodeType]:
        if value is None:
            return None
        try:
            return NodeType(value)
        except ValueError:
            return None

    # ---- Write path: remember() ----

    def remember(self, fact_description: str) -> MemoryWriteResult:
        try:
            structured = self._parse_fact(fact_description)
        except MemoryAgentError as e:
            return MemoryWriteResult(success=False, message=str(e))

        nodes, edges = self._validate_fact_structure(structured)
        if nodes is None:
            return MemoryWriteResult(success=False, message=edges)  # edges holds the error message here

        decision = self.guardrail.evaluate(
            ActionRequest(
                agent_name="memory",
                action_description=f"Remember fact: {fact_description}",
                severity=ActionSeverity.WRITE,
                target_sensitivity=TargetSensitivity.SELF,
            )
        )
        if decision.verdict != Verdict.AUTO_APPROVE:
            return MemoryWriteResult(
                success=True,
                message=f"Write pending approval: {decision.reason}",
                pending_approval=True,
                pending_nodes=nodes,
                pending_edges=edges,
            )

        return self._commit_fact(nodes, edges)

    def commit_pending(self, result: MemoryWriteResult) -> MemoryWriteResult:
        """Executes a previously-held remember() after explicit human approval."""
        if not result.pending_approval or result.pending_nodes is None:
            raise MemoryAgentError("commit_pending called on a result with nothing pending.")
        return self._commit_fact(result.pending_nodes, result.pending_edges or [])

    def _parse_fact(self, fact_description: str) -> dict:
        raw = self.llm_client.complete(fact_description, system=REMEMBER_SYSTEM_PROMPT)
        return _extract_json(raw)

    def _validate_fact_structure(self, structured: dict):
        """
        Returns (nodes, edges) as validated dataclass lists on success, or
        (None, error_message) on failure — validated against the same
        NodeType/EdgeType enums the rest of the system uses, so a
        hallucinated type string is rejected here rather than crashing
        `GraphMemoryStore` deeper in the call stack.

        An edge is only accepted if both endpoints are "grounded": declared
        in this same fact's node list, or already present in the store. The
        latter matters for the very common case of linking a brand-new fact
        to someone already known (e.g. "I met Arya's colleague Priya" should
        be able to reference the existing `person:arya`, not just newly
        introduced ids) — without it, every fact would have to re-declare
        every node it touches, defeating the point of persistent memory.
        A truly ungrounded id (neither new nor already known) is still
        rejected, since writing that edge would either crash
        `GraphMemoryStore.add_edge` or silently reference a node the system
        knows nothing about.
        """
        raw_nodes = structured.get("nodes")
        raw_edges = structured.get("edges", [])
        if not isinstance(raw_nodes, list) or not raw_nodes:
            return None, "Fact must produce at least one node."

        nodes = []
        node_ids = set()
        for raw_node in raw_nodes:
            node_id = raw_node.get("id") if isinstance(raw_node, dict) else None
            node_type = self._parse_node_type(raw_node.get("type") if isinstance(raw_node, dict) else None)
            label = raw_node.get("label") if isinstance(raw_node, dict) else None
            if not node_id or node_type is None or not label:
                return None, f"Malformed node in LLM output: {raw_node!r}"
            nodes.append(Node(id=node_id, type=node_type, label=label))
            node_ids.add(node_id)

        def _is_grounded(node_id: str) -> bool:
            return node_id in node_ids or self.memory_store.get_node(node_id) is not None

        edges = []
        for raw_edge in raw_edges or []:
            source_id = raw_edge.get("source_id") if isinstance(raw_edge, dict) else None
            target_id = raw_edge.get("target_id") if isinstance(raw_edge, dict) else None
            edge_type = self._parse_edge_type(raw_edge.get("type") if isinstance(raw_edge, dict) else None)
            if not source_id or not target_id or edge_type is None:
                return None, f"Malformed edge in LLM output: {raw_edge!r}"
            if not _is_grounded(source_id) or not _is_grounded(target_id):
                return None, (
                    f"Edge references node(s) neither declared in this fact nor "
                    f"already known ({source_id!r} -> {target_id!r}) — refusing to "
                    "write an edge to an ungrounded node."
                )
            edges.append(Edge(source_id=source_id, target_id=target_id, type=edge_type))

        return nodes, edges

    def _commit_fact(self, nodes: list[Node], edges: list[Edge]) -> MemoryWriteResult:
        written_ids = []
        for node in nodes:
            # Idempotent-ish: skip nodes that already exist rather than
            # erroring, since re-mentioning a known person is a normal case,
            # not a conflict.
            if self.memory_store.get_node(node.id) is None:
                self.memory_store.add_node(node)
            written_ids.append(node.id)

        edges_written = 0
        for edge in edges:
            self.memory_store.add_edge(edge)
            edges_written += 1

        return MemoryWriteResult(
            success=True,
            message=f"Remembered {len(written_ids)} node(s), {edges_written} edge(s).",
            nodes_written=written_ids,
            edges_written=edges_written,
        )
