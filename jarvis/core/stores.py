"""
Small personal stores kept in the same SQLite file: lists (to-dos, shopping, notes), expenses and routines.

Plain tables, no LLM anywhere near them: adding, finishing and totalling are exact operations, so the
agents that use these never have to trust a model with arithmetic or with what is on a list.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from jarvis.core.storage import Database

MAX_ITEM = 300
MAX_LIST_ITEMS = 500


def list_key(name: Optional[str]) -> str:
    """'Shopping List' / 'groceries' / 'to-do' -> canonical list names."""
    n = re.sub(r"[^a-z0-9 ]", "", (name or "todo").lower()).strip()
    n = re.sub(r"\s*(list|items)$", "", n).strip() or "todo"
    if n in {"todo", "to do", "todos", "task", "tasks", "my", "the"}:
        return "todo"
    if n in {"shopping", "grocery", "groceries", "buy", "market"}:
        return "shopping"
    if n in {"note", "notes"}:
        return "notes"
    return n[:40]


def list_label(key: str) -> str:
    return {"todo": "to-do list", "shopping": "shopping list", "notes": "notes"}.get(key, f"{key} list")


class ListStore:
    def __init__(self, db: Database):
        self.db = db

    def add(self, lst: str, text: str, due: Optional[str] = None) -> Optional[dict]:
        text = re.sub(r"\s+", " ", text).strip(" .")[:MAX_ITEM]
        if not text:
            return None
        key = list_key(lst)
        dup = self.find(key, text, exact=True)
        if dup:                                    # adding milk twice doesn't make two milks
            return {**dup, "duplicate": True}
        if len(self.items(key, include_done=True)) >= MAX_LIST_ITEMS:
            raise ValueError(f"Your {list_label(key)} is full. Clear some finished items first.")
        cur = self.db.execute("INSERT INTO items(list,text,done,due,created) VALUES(?,?,0,?,?)", (key, text, due, time.time()))
        return {"id": cur.lastrowid, "list": key, "text": text, "done": 0, "due": due}

    def items(self, lst: Optional[str] = None, include_done: bool = False) -> list[dict]:
        sql, params = "SELECT * FROM items", []
        conds = []
        if lst is not None:
            conds.append("list=?"); params.append(list_key(lst))
        if not include_done:
            conds.append("done=0")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return self.db.query(sql + " ORDER BY done, COALESCE(due,'9999'), id", tuple(params))

    def lists(self) -> list[dict]:
        return self.db.query("SELECT list, SUM(done=0) AS open, COUNT(*) AS total FROM items GROUP BY list ORDER BY list")

    def find(self, lst: Optional[str], text: str, exact: bool = False) -> Optional[dict]:
        """Open item matching `text`: exact, then contains, then fuzzy. Searches every list when lst is None."""
        q = re.sub(r"\s+", " ", text).strip(" .").lower()
        if not q:
            return None
        rows = self.items(lst) if lst is not None else self.items()
        for r in rows:
            if r["text"].lower() == q:
                return r
        if exact:
            return None
        hits = [r for r in rows if q in r["text"].lower() or r["text"].lower() in q]
        if len(hits) >= 1:
            return min(hits, key=lambda r: abs(len(r["text"]) - len(q)))
        words = {w for w in re.findall(r"\w{3,}", q)}
        scored = [(len(words & set(re.findall(r"\w{3,}", r["text"].lower()))), r) for r in rows]
        scored = [x for x in scored if x[0]]
        if scored:
            return max(scored, key=lambda x: x[0])[1]
        close = difflib.get_close_matches(q, [r["text"].lower() for r in rows], n=1, cutoff=0.75)
        return next((r for r in rows if r["text"].lower() == close[0]), None) if close else None

    def done(self, item_id: int) -> None:
        self.db.execute("UPDATE items SET done=1, closed=? WHERE id=?", (time.time(), item_id))

    def remove(self, item_id: int) -> None:
        self.db.execute("DELETE FROM items WHERE id=?", (item_id,))

    def restore(self, row: dict) -> None:
        self.db.execute("INSERT INTO items(list,text,done,due,created) VALUES(?,?,?,?,?)",
                        (row["list"], row["text"], 0, row.get("due"), row.get("created") or time.time()))

    def clear(self, lst: str, only_done: bool = False) -> int:
        key = list_key(lst)
        cur = self.db.execute("DELETE FROM items WHERE list=?" + (" AND done=1" if only_done else ""), (key,))
        return cur.rowcount

    def due_between(self, start: datetime, end: datetime) -> list[dict]:
        return [r for r in self.items() if r.get("due") and start.isoformat() <= r["due"] < end.isoformat()]

    def overdue(self, now: datetime) -> list[dict]:
        return [r for r in self.items() if r.get("due") and r["due"] < now.isoformat()]


CATEGORIES = {
    "food": r"lunch|dinner|breakfast|brunch|snack|coffee|tea|chai|food|restaurant|canteen|mess|swiggy|zomato|pizza|biryani|juice|meal|eat|ate|cafe|dosa|idli",
    "groceries": r"grocer|vegetable|fruit|milk|supermarket|kirana|bigbasket|blinkit|zepto|instamart|provision",
    "transport": r"uber|ola|auto|cab|taxi|bus|metro|train|flight|fuel|petrol|diesel|rapido|ticket|toll|parking|travel",
    "shopping": r"amazon|flipkart|myntra|clothes|shirt|shoes|shopping|bought|gadget|headphone|book|stationery",
    "bills": r"rent|electricity|wifi|internet|recharge|bill|mobile|phone bill|subscription|netflix|spotify|prime|insurance|emi|fees?",
    "health": r"doctor|medicine|pharmacy|hospital|gym|medical|clinic",
    "fun": r"movie|cinema|game|party|concert|outing|trip|drinks?",
}
_CAT_RX = {c: re.compile(rf"\b({p})", re.I) for c, p in CATEGORIES.items()}


def categorise(note: str) -> str:
    for c, rx in _CAT_RX.items():
        if rx.search(note or ""):
            return c
    return "other"


class ExpenseStore:
    def __init__(self, db: Database, currency: str = "INR"):
        self.db = db
        self.currency = currency

    def add(self, amount: float, note: str, when: datetime, category: Optional[str] = None, currency: Optional[str] = None) -> dict:
        if not (0 < amount < 1e9):
            raise ValueError("That amount doesn't look right.")
        note = re.sub(r"\s+", " ", note or "").strip(" .")[:120] or "expense"
        cat = category if category in CATEGORIES or category == "other" else categorise(note)
        cur = self.db.execute("INSERT INTO expenses(ts,amount,currency,category,note) VALUES(?,?,?,?,?)",
                              (when.timestamp(), round(amount, 2), (currency or self.currency).upper(), cat, note))
        return {"id": cur.lastrowid, "amount": round(amount, 2), "category": cat, "note": note,
                "currency": (currency or self.currency).upper()}

    def between(self, start: datetime, end: datetime, category: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM expenses WHERE ts>=? AND ts<?", [start.timestamp(), end.timestamp()]
        if category:
            sql += " AND category=?"; params.append(category)
        return self.db.query(sql + " ORDER BY ts DESC", tuple(params))

    def last(self) -> Optional[dict]:
        rows = self.db.query("SELECT * FROM expenses ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def remove(self, expense_id: int) -> None:
        self.db.execute("DELETE FROM expenses WHERE id=?", (expense_id,))

    @staticmethod
    def summarise(rows: list[dict]) -> dict:
        total = round(sum(r["amount"] for r in rows), 2)
        by: dict[str, float] = {}
        for r in rows:
            by[r["category"]] = round(by.get(r["category"], 0) + r["amount"], 2)
        return {"total": total, "count": len(rows), "by_category": dict(sorted(by.items(), key=lambda kv: -kv[1]))}

    def budget(self) -> Optional[float]:
        return self.db.kv_get("monthly_budget")

    def set_budget(self, amount: Optional[float]) -> None:
        self.db.kv_set("monthly_budget", amount)


def period(name: str, now: datetime) -> tuple[datetime, datetime, str]:
    """'today' / 'this week' / 'last month' ... -> [start, end) and a label for the reply."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = (name or "this month").lower()
    if "yesterday" in n:
        return day - timedelta(days=1), day, "yesterday"
    if "today" in n:
        return day, day + timedelta(days=1), "today"
    if "last week" in n:
        start = day - timedelta(days=day.weekday() + 7)
        return start, start + timedelta(days=7), "last week"
    if "week" in n:
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7), "this week"
    if "last month" in n:
        first = day.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return prev, first, "last month"
    if "year" in n:
        start = day.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1), "this year"
    first = day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt, "this month"


class RoutineStore:
    MAX_STEPS = 8

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def key(name: str) -> str:
        n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
        n = re.sub(r"\b(my|the|routine|mode)\b", " ", n)
        return re.sub(r"\s+", " ", n).strip()[:40]

    def save(self, name: str, steps: list[str]) -> str:
        key = self.key(name)
        steps = [re.sub(r"\s+", " ", s).strip(" .") for s in steps if s and s.strip()][: self.MAX_STEPS]
        if not key or not steps:
            raise ValueError("A routine needs a name and at least one step.")
        self.db.execute("INSERT INTO routines(name,steps,created) VALUES(?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET steps=excluded.steps", (key, json.dumps(steps), time.time()))
        return key

    def get(self, name: str) -> Optional[dict]:
        key = self.key(name)
        if not key:
            return None
        rows = self.db.query("SELECT * FROM routines WHERE name=?", (key,))
        if not rows:
            names = [r["name"] for r in self.all()]
            close = difflib.get_close_matches(key, names, n=1, cutoff=0.82)
            rows = self.db.query("SELECT * FROM routines WHERE name=?", (close[0],)) if close else []
        return {**rows[0], "steps": json.loads(rows[0]["steps"])} if rows else None

    def exact(self, phrase: str) -> Optional[dict]:
        """The routine whose name is exactly this phrase ("good night"), for trigger-by-saying-it."""
        key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (phrase or "").lower())).strip()
        if not key or len(key) > 40:
            return None
        rows = self.db.query("SELECT * FROM routines WHERE name=?", (key,))
        return {**rows[0], "steps": json.loads(rows[0]["steps"])} if rows else None

    def all(self) -> list[dict]:
        return [{**r, "steps": json.loads(r["steps"])} for r in self.db.query("SELECT * FROM routines ORDER BY name")]

    def delete(self, name: str) -> bool:
        r = self.get(name)
        if not r:
            return False
        self.db.execute("DELETE FROM routines WHERE name=?", (r["name"],))
        return True

    def ran(self, name: str) -> None:
        self.db.execute("UPDATE routines SET runs=runs+1 WHERE name=?", (name,))
