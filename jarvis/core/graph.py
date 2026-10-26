"""
Knowledge graph (Graph RAG memory), networkx in memory + SQLite persistence.

Upgrades over v1's GraphMemoryStore:
- More node types (place, project, preference) and relations (works_on, meets_at, prefers,
  works_with) because a personal assistant's questions are about these.
- Name resolution: the LLM says "Arun", code finds person:arun (exact id, label match,
  alias, then fuzzy match). Small models never have to produce ids.
- Bidirectional traversal for "related" questions (v1 only followed outgoing edges, which
  made "who did Priya introduce" unanswerable).
- Every write goes straight to SQLite, so a crash never loses memory.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from typing import Optional

import networkx as nx

from jarvis.core.storage import Database

NODE_TYPES = {"person", "meeting", "task", "commitment", "place", "project", "preference", "self"}
RELATIONS = {"relates_to", "follows_up", "blocks", "introduced_by", "attended", "owes",
             "works_on", "meets_at", "prefers", "works_with"}
INVERSE_TEXT = {
    "introduced_by": "introduced", "owes": "is owed by", "blocks": "is blocked by",
    "works_on": "has contributor", "meets_at": "is a meeting spot for", "attended": "had attendee",
    "prefers": "is preferred by", "follows_up": "is followed up by",
}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "unnamed"


class KnowledgeGraph:
    def __init__(self, db: Database, owner_name: str = "me"):
        self.db = db
        self.g = nx.MultiDiGraph()
        self._load()
        if "self:me" not in self.g:
            self.upsert_node("self:me", "self", owner_name, {})

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        for r in self.db.query("SELECT * FROM graph_nodes"):
            self.g.add_node(r["id"], type=r["type"], label=r["label"], attributes=json.loads(r["attributes"]))
        for r in self.db.query("SELECT * FROM graph_edges"):
            if r["src"] in self.g and r["dst"] in self.g:
                self.g.add_edge(r["src"], r["dst"], key=r["type"], type=r["type"],
                                attributes=json.loads(r["attributes"]))

    # ------------------------------------------------------------------ writes
    def upsert_node(self, node_id: str, ntype: str, label: str, attributes: Optional[dict] = None) -> str:
        if ntype not in NODE_TYPES:
            raise ValueError(f"unknown node type {ntype}")
        attrs = dict(self.g.nodes[node_id].get("attributes", {})) if node_id in self.g else {}
        attrs.update(attributes or {})
        self.g.add_node(node_id, type=ntype, label=label, attributes=attrs)
        self.db.execute(
            "INSERT INTO graph_nodes(id,type,label,attributes,created) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET label=excluded.label, attributes=excluded.attributes",
            (node_id, ntype, label, json.dumps(attrs), time.time()))
        return node_id

    def ensure_entity(self, name: str, ntype: str) -> str:
        if name.strip().lower() in {"me", "i", "myself", "user"}:
            return "self:me"
        found = self.resolve(name, ntype)
        if found:
            return found
        return self.upsert_node(f"{ntype}:{slug(name)}", ntype, name.strip(), {})

    def add_edge(self, src: str, dst: str, relation: str, attributes: Optional[dict] = None) -> None:
        if relation not in RELATIONS:
            raise ValueError(f"unknown relation {relation}")
        if src not in self.g or dst not in self.g:
            raise ValueError("both endpoints must exist")
        self.g.add_edge(src, dst, key=relation, type=relation, attributes=attributes or {})
        self.db.execute(
            "INSERT OR REPLACE INTO graph_edges(src,dst,type,attributes,created) VALUES(?,?,?,?,?)",
            (src, dst, relation, json.dumps(attributes or {}), time.time()))

    def set_attribute(self, node_id: str, key: str, value: str) -> None:
        n = self.g.nodes[node_id]
        self.upsert_node(node_id, n["type"], n["label"], {key: value})

    def delete_node(self, node_id: str) -> bool:
        if node_id not in self.g or node_id == "self:me":
            return False
        self.g.remove_node(node_id)
        self.db.execute("DELETE FROM graph_nodes WHERE id=?", (node_id,))
        self.db.execute("DELETE FROM graph_edges WHERE src=? OR dst=?", (node_id, node_id))
        return True

    # ------------------------------------------------------------------ reads
    def node(self, node_id: str) -> Optional[dict]:
        if node_id not in self.g:
            return None
        return {"id": node_id, **self.g.nodes[node_id]}

    def resolve(self, name: Optional[str], ntype: Optional[str] = None) -> Optional[str]:
        """Name -> node id. Exact id, exact label, alias, prefix, then fuzzy (>=0.8)."""
        if not name:
            return None
        q = name.strip().lower()
        if q in {"me", "i", "myself"}:
            return "self:me"
        if q in self.g:
            return q
        cands = [(nid, d) for nid, d in self.g.nodes(data=True) if ntype is None or d["type"] == ntype]
        for nid, d in cands:
            if d["label"].lower() == q or nid.split(":", 1)[-1] == slug(q):
                return nid
        for nid, d in cands:
            aliases = [a.lower() for a in d.get("attributes", {}).get("aliases", [])]
            if q in aliases:
                return nid
        for nid, d in cands:  # "Arun" matches "Arun Kumar"
            label = d["label"].lower()
            if label.startswith(q + " ") or q.startswith(label + " "):
                return nid
        labels = {d["label"].lower(): nid for nid, d in cands}
        close = difflib.get_close_matches(q, list(labels), n=1, cutoff=0.8)
        return labels[close[0]] if close else None

    def edges_of(self, node_id: str, relation: Optional[str] = None) -> list[dict]:
        """Both directions, phrased from node_id's point of view."""
        out = []
        for _, dst, d in self.g.out_edges(node_id, data=True):
            if relation is None or d["type"] == relation:
                out.append({"direction": "out", "relation": d["type"], "other": self.node(dst),
                            "attributes": d.get("attributes", {})})
        for src, _, d in self.g.in_edges(node_id, data=True):
            if relation is None or d["type"] == relation:
                out.append({"direction": "in", "relation": d["type"], "other": self.node(src),
                            "attributes": d.get("attributes", {})})
        return out

    def by_type(self, ntype: str) -> list[dict]:
        return [{"id": n, **d} for n, d in self.g.nodes(data=True) if d["type"] == ntype]

    def connected(self, a: str, b: str) -> Optional[list[str]]:
        try:
            return nx.shortest_path(self.g.to_undirected(as_view=True), a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def search(self, text: str, limit: int = 8) -> list[dict]:
        terms = [t for t in re.findall(r"\w+", text.lower()) if len(t) > 2]
        scored = []
        for n, d in self.g.nodes(data=True):
            hay = (d["label"] + " " + json.dumps(d.get("attributes", {}))).lower()
            score = sum(hay.count(t) for t in terms)
            if score:
                scored.append((score, {"id": n, **d}))
        scored.sort(key=lambda x: -x[0])
        return [x[1] for x in scored[:limit]]

    def describe_node(self, node_id: str, max_edges: int = 12) -> str:
        n = self.node(node_id)
        if not n:
            return ""
        parts = [f"{n['label']} ({n['type']})"]
        attrs = {k: v for k, v in n.get("attributes", {}).items() if k != "aliases"}
        if attrs:
            parts.append("; ".join(f"{k}: {v}" for k, v in attrs.items()))
        for e in self.edges_of(node_id)[:max_edges]:
            other = e["other"]["label"] if e["other"] else "?"
            if e["direction"] == "out":
                parts.append(f"{e['relation'].replace('_', ' ')} {other}")
            else:
                parts.append(f"{INVERSE_TEXT.get(e['relation'], e['relation'].replace('_', ' ') + ' (from)')} {other}")
        return ". ".join(parts)

    _STOP = set("""a an the i me my mine you your yours we our is are was were be been am do does did have has had to of in
        on at for with about from by and or but not what who whom whose where when why how which that this these those it its
        he she they them his her their there here please tell know any anything something can could would should will shall
        jarvis hey okay ok just also really very much many more most some all""".split())

    def retrieve(self, text: str, k: int = 6) -> list[str]:
        """
        Graph RAG retrieval: score every entity by name/alias matches, words in its attributes and relations,
        then add direct neighbours of strong hits. Returns short fact lines, best first.
        Works on phrasing like "where does my sister study" (matches an attribute "relationship: sister").
        """
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in self._STOP and len(w) > 1]
        if not words:
            return []
        stems = {w.rstrip("s") for w in words}
        scores: dict[str, float] = {}
        for nid, d in self.g.nodes(data=True):
            if nid == "self:me":
                continue
            label = d["label"].lower()
            attrs = d.get("attributes", {})
            alias = " ".join(a.lower() for a in attrs.get("aliases", []))
            hay_attr = " ".join(f"{k} {v}" for k, v in attrs.items() if k != "aliases").lower()
            rel_words = " ".join(e[2].get("type", "").replace("_", " ") for e in self.g.in_edges(nid, data=True))
            sc = 0.0
            for w in stems:
                if re.search(rf"\b{re.escape(w)}", label) or re.search(rf"\b{re.escape(w)}", alias):
                    sc += 3
                elif re.search(rf"\b{re.escape(w)}", hay_attr):
                    sc += 2
                elif re.search(rf"\b{re.escape(w)}", rel_words):
                    sc += 0.5
            if sc:
                scores[nid] = sc
        for w in words:                               # fuzzy names ("Ananyaa", "Prof Ravi")
            fuzzy = self.resolve(w) if len(w) > 3 else None
            if fuzzy and fuzzy != "self:me" and fuzzy not in scores:
                scores[fuzzy] = 2.5
        top = sorted(scores, key=lambda n: -scores[n])[:k]
        for nid in list(top[:3]):                     # include neighbours of the best hits
            for e in self.edges_of(nid)[:4]:
                o = e["other"]["id"] if e["other"] else None
                if o and o not in top and o != "self:me" and len(top) < k + 3:
                    top.append(o)
        return [self.describe_node(n, max_edges=6) for n in top]

    def context_for(self, text: str, limit: int = 4) -> str:
        """Short fact lines relevant to `text`, for agent prompts."""
        return "\n".join(self.retrieve(text, k=limit))

    def stats(self) -> dict:
        return {"nodes": self.g.number_of_nodes(), "edges": self.g.number_of_edges()}
