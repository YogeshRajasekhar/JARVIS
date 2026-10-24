"""
Memory agent: Graph RAG over people, projects, tasks, commitments, places and preferences.

Write path: LLM extracts entities/relations by NAME -> code resolves names to existing nodes
(so "Arun" links to the existing person:arun_kumar) -> memory.write tool (audited).
Read path: LLM picks one of 5 lookup operations -> code traverses the graph -> templated answer.
Sensitive values are refused before anything is written.
"""

from __future__ import annotations

import re

from jarvis.agents.base import AgentReply, BaseAgent, TurnContext
from jarvis.core.redaction import contains_sensitive
from jarvis.llm.schemas import MemoryFactOut, MemoryQueryOut
from jarvis.prompts import MEMORY_FACT, MEMORY_QUERY

WRITE_RX = re.compile(r"^(please\s+)?(remember|note|don't forget|make a note|save)\b|\bremember that\b", re.I)
FORGET_RX = re.compile(r"^(please\s+)?forget (?:about |everything about )?(.+?)[.!]?$", re.I)
REL_TEXT = {"introduced_by": "introduced by", "works_on": "works on", "owes": "owes", "blocks": "blocks",
            "meets_at": "meets at", "prefers": "prefers", "works_with": "works with", "attended": "attended",
            "follows_up": "follows up on", "relates_to": "related to"}


class MemoryAgent(BaseAgent):
    name = "memory"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        if FORGET_RX.match(text.strip()):
            return self._forget(FORGET_RX.match(text.strip()).group(2), ctx)
        if WRITE_RX.search(text):
            return self._remember(text, ctx)
        return self._query(text, ctx)

    # ------------------------------------------------------------------ write
    def _remember(self, text: str, ctx: TurnContext) -> AgentReply:
        if contains_sensitive(text) or re.search(r"\b(password|otp|cvv|upi pin|card number)\b", text, re.I):
            return AgentReply(self.name, "I don't store passwords, codes, card or ID numbers. That stays with you.")
        fact: MemoryFactOut = self.extract(MemoryFactOut, MEMORY_FACT, text, ctx, task="memory_fact")
        if any(e.name == "refused" for e in fact.entities):
            return AgentReply(self.name, "I don't store passwords, codes, card or ID numbers. That stays with you.")
        args = {"entities": [e.model_dump() for e in fact.entities],
                "relations": [{"from": r.source, "relation": r.relation, "to": r.target} for r in fact.relations],
                "attributes": [a.model_dump() for a in fact.attributes]}
        # Relation endpoints must be declared entities (or "me"/already known): no dangling nodes.
        declared = {e["name"].lower() for e in args["entities"]} | {"me"}
        for r in args["relations"]:
            for end in (r["from"], r["to"]):
                if end.lower() not in declared and not self.svc.graph.resolve(end):
                    args["entities"].append({"name": end, "type": "person"})
                    declared.add(end.lower())
        out = self.propose("memory.write", args, ctx)
        people = [e["name"] for e in args["entities"] if e["type"] == "person"]
        summary = self._summarise(args)
        return AgentReply(self.name, self.say_outcome(out, f"Noted: {summary}."), [out],
                          focus={"last_person": people[0]} if people else {})

    @staticmethod
    def _summarise(args: dict) -> str:
        bits = [f"{r['from']} {REL_TEXT.get(r['relation'], r['relation'])} {r['to']}" for r in args["relations"]]
        bits += [f"{a['entity']}'s {a['key']} is {a['value']}" for a in args["attributes"]]
        if not bits:
            bits = [e["name"] for e in args["entities"]]
        return "; ".join(bits)[:200]

    def _forget(self, name: str, ctx: TurnContext) -> AgentReply:
        nid = self.svc.graph.resolve(name)
        if not nid or nid == "self:me":
            return AgentReply(self.name, f"I don't have anything stored about {name}.")
        label = self.svc.graph.node(nid)["label"]
        out = self.propose("memory.forget", {"node_id": nid, "label": label}, ctx)
        return AgentReply(self.name, self.say_outcome(out), [out])

    # ------------------------------------------------------------------ read
    def _query(self, text: str, ctx: TurnContext) -> AgentReply:
        q: MemoryQueryOut = self.extract(MemoryQueryOut, MEMORY_QUERY, text, ctx, task="memory_query")
        g = self.svc.graph
        if q.operation in ("about", "related"):
            nid = g.resolve(q.entity)
            if not nid:
                return AgentReply(self.name, f"I don't know anything about {q.entity} yet. Tell me and I'll remember.")
            label = g.node(nid)["label"]
            if q.operation == "about":
                return AgentReply(self.name, g.describe_node(nid) + ".", focus={"last_person": label})
            edges = g.edges_of(nid, q.relation)
            if not edges:
                return AgentReply(self.name, f"I don't have that relation stored for {label}.")
            if q.relation == "introduced_by":
                intro = [e["other"]["label"] for e in edges if e["direction"] == "out"]
                if intro:
                    return AgentReply(self.name, f"{intro[0]} introduced you to {label}.", focus={"last_person": label})
            lines = [f"{label} {REL_TEXT.get(e['relation'], e['relation'])} {e['other']['label']}" if e["direction"] == "out"
                     else f"{e['other']['label']} {REL_TEXT.get(e['relation'], e['relation'])} {label}" for e in edges[:6]]
            return AgentReply(self.name, ". ".join(lines) + ".", focus={"last_person": label})
        if q.operation == "list":
            items = g.by_type(q.node_type or "person")
            items = [i for i in items if i["id"] != "self:me"]
            if not items:
                return AgentReply(self.name, f"No {q.node_type}s stored yet.")
            names = ", ".join(i["label"] for i in items[:10])
            return AgentReply(self.name, f"{len(items)} {q.node_type}{'s' if len(items) != 1 else ''}: {names}.")
        if q.operation == "connected":
            a, b = g.resolve(q.entity), g.resolve(q.target)
            if not a or not b:
                return AgentReply(self.name, "I don't know one of those yet.")
            path = g.connected(a, b)
            if not path:
                return AgentReply(self.name, f"I don't see any link between {q.entity} and {q.target}.")
            return AgentReply(self.name, "Yes: " + " → ".join(g.node(n)["label"] for n in path) + ".")
        hits = g.search(q.text or text)
        past = self.svc.db.search_messages(q.text or text, limit=3)
        if hits:
            return AgentReply(self.name, " ".join(g.describe_node(h["id"], 4) + "." for h in hits[:3]))
        if past:
            return AgentReply(self.name, f"We talked about that before: \"{past[0]['content'][:140]}\"")
        return AgentReply(self.name, "I don't have anything stored about that.")
