"""
Node and edge types for the graph memory.

Why a graph at all (worth being able to answer this precisely in an
interview, since it's the single most-discussed design decision in this
whole project): personal/professional context is inherently relational —
"who introduced me to X", "what's blocking task Y", "what did I promise the
last three times I talked to this person" are relationship-traversal
questions, not topical-similarity questions. A flat vector store answers
"what's semantically similar to this text" well; it does NOT answer
"what connects to this node" well. That's the specific gap this fills.

Kept deliberately small for now — this is the MVP node/edge set, not the
final word. New types get added when a real query needs them, not before
(same "let the failure mode prescribe the upgrade" principle used
elsewhere in this project, e.g. deciding not to over-build the RAG for the
museum project).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NodeType(Enum):
    PERSON = "person"
    MEETING = "meeting"
    TASK = "task"
    COMMITMENT = "commitment"


class EdgeType(Enum):
    RELATES_TO = "relates_to"      # generic association, e.g. person <-> project
    FOLLOWS_UP = "follows_up"      # meeting/task follows up on another
    BLOCKS = "blocks"              # task A blocks task B
    INTRODUCED_BY = "introduced_by"  # person A was introduced by person B
    ATTENDED = "attended"          # person attended meeting
    OWES = "owes"                  # commitment owed to a person


@dataclass
class Node:
    id: str                 # unique id, e.g. "person:arun" or "meeting:2026-08-20-standup"
    type: NodeType
    label: str               # human-readable name
    attributes: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)


@dataclass
class Edge:
    source_id: str
    target_id: str
    type: EdgeType
    attributes: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
