"""
Lists, money and routines: three agents for everyday bookkeeping.

Each one understands the common phrasings with plain patterns first (instant, exact, works with no
language model at all) and only asks the model to extract a command when the phrasing is unusual.
The stores do the remembering and the arithmetic; the model never does either.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from pydantic import Field

from jarvis.agents.base import AgentError, AgentReply, BaseAgent, TurnContext
from jarvis.core.redaction import states_secret
from jarvis.core.stores import CATEGORIES, ExpenseStore, ListStore, RoutineStore, list_key, list_label, period
from jarvis.core.timeparse import fmt_time, resolve
from jarvis.llm.schemas import _Lenient
from jarvis.prompts import LISTS, MONEY, ROUTINE

LIST_NAME = r"(?P<list>shopping|grocery|groceries|to-?do|todo|task|tasks|packing|reading|watch|movie|wish|bucket|[a-z]+(?: [a-z]+)?)"
# "add X to my shopping list" / "to my tasks": a well-known list may drop the word "list"; any other name must say it
# ("add Arun to the meeting" is not a list command).
ON_LIST = r"(?:my |the |our )(?:(?P<list>shopping|grocery|groceries|to-?do|todo|tasks?|notes)(?: list)?|(?P<list2>[a-z]+(?: [a-z]+)?) list)"


def _lst(m) -> Optional[str]:
    d = m.groupdict()
    return d.get("list") or d.get("list2")
DUE_RX = re.compile(r"\s+(?:by|due|before|on|for)\s+(?P<when>(?:tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"next \w+|this \w+|the \d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)? \w+|\w+ \d{1,2}(?:st|nd|rd|th)?|end of (?:the )?(?:day|week|month))"
                    r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\s+(?:morning|afternoon|evening|night))?)\s*$", re.I)


# ============================================================================ lists / to-dos / notes
class ListsOut(_Lenient):
    action: Literal["add", "show", "done", "remove", "clear", "unknown"]
    items: List[str] = Field(default_factory=lambda: [])      # declared before `list`, which shadows the builtin below
    due: Optional[str] = None
    list: Optional[str] = None


def split_items(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\band\b|&|\+)\s*", text)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def parse_lists(text: str) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.strip()).rstrip("?.! ")
    low = t.lower()
    # ---- notes
    m = re.match(r"^(?:please )?(?:take|make|write|jot|note)(?: down)?(?: a)?(?: quick)?(?: note)?(?: down)?(?: that| saying| to self)?[:,]? (?P<x>.+)$", t, re.I)
    if m and re.match(r"^(?:please )?(?:take a note|make a note|write a note|write down|jot down|note down|note to self|take a quick note)", low):
        return {"action": "add", "list": "notes", "items": [m.group("x")], "due": None}
    if re.match(r"^(?:show|read|what are|what's in|list|open)(?: me)? (?:my |the )?notes$", low) or low in {"my notes", "notes"}:
        return {"action": "show", "list": "notes", "items": [], "due": None}
    # ---- show
    if re.match(r"^(?:what(?:'s| is| are)|show|list|read|tell me|give me)(?: me)?(?: all)?(?: on| in)? (?:my |the )?(?:open |pending )?(?:tasks?|to-?dos?|to-?do list|task list)$", low) \
            or re.match(r"^(?:what do i (?:have|need) to do|what(?:'s| is) (?:left|pending) to do|do i have any (?:tasks?|to-?dos?)|my (?:tasks|to-?dos?|to-?do list))$", low):
        return {"action": "show", "list": "todo", "items": [], "due": None}
    if re.match(r"^(?:what do i (?:need|have) to buy|what should i buy)$", low):
        return {"action": "show", "list": "shopping", "items": [], "due": None}
    m = re.match(rf"^(?:what(?:'s| is| are)|show|list|read|tell me|give me)(?: me)?(?: on| in)? (?:my |the ){LIST_NAME} list$", low) \
        or re.match(rf"^(?:my |the ){LIST_NAME} list$", low)
    if m:
        return {"action": "show", "list": m.group("list"), "items": [], "due": None}
    if re.match(r"^(?:what|which) lists do i have|^(?:show|list)(?: me)? (?:all )?my lists$", low):
        return {"action": "show", "list": "*", "items": [], "due": None}
    # ---- clear
    m = re.match(rf"^(?:clear|empty|wipe|reset|delete everything (?:on|from)) (?:my |the ){LIST_NAME} list$", low)
    if m:
        return {"action": "clear", "list": m.group("list"), "items": [], "due": None}
    if re.match(r"^(?:clear|delete|remove) (?:my |the |all )?(?:completed|finished|done) (?:tasks|items|to-?dos)$", low):
        return {"action": "clear", "list": "todo", "items": ["__done__"], "due": None}
    if re.match(r"^(?:clear|empty|delete all) (?:my |the |all )?(?:tasks|to-?dos|notes)$", low):
        return {"action": "clear", "list": "notes" if "note" in low else "todo", "items": [], "due": None}
    # ---- remove
    m = re.match(rf"^(?:remove|delete|take|drop|scratch) (?P<x>.+?) (?:from|off) {ON_LIST}$", t, re.I)
    if m:
        return {"action": "remove", "list": _lst(m), "items": split_items(m.group("x")), "due": None}
    m = re.match(r"^(?:remove|delete|drop) (?:the )?(?:task|to-?do|item)(?: to| called| about)? (?P<x>.+)$", t, re.I)
    if m:
        return {"action": "remove", "list": None, "items": [m.group("x")], "due": None}
    # ---- done
    m = re.match(r"^(?:mark|check off|tick off|tick|cross off|cross out|strike off|strike) (?P<x>.+?)(?: (?:as|off))?(?: (?:done|complete|completed|finished|bought))?"
                 rf"(?: (?:on|from|in|off) {ON_LIST})?$", t, re.I)
    if m:
        return {"action": "done", "list": _lst(m), "items": split_items(m.group("x")), "due": None}
    m = re.match(r"^i(?:'ve| have)?(?: just| already)? (?:bought|picked up)(?: the| my| some)? (?P<x>.+)$", t, re.I)
    if m:
        return {"action": "done", "list": "shopping", "items": split_items(m.group("x")), "due": None}
    m = re.match(r"^i(?:'ve| have)?(?: just| already)? (?:finished|completed|done with)(?: the| my)? (?P<x>.+)$", t, re.I) \
        or re.match(r"^(?P<x>.+?) is (?:done|finished|complete|completed)$", t, re.I)
    if m:      # weak: "I finished the report" is usually a task, but could be small talk; the router weighs it lower
        return {"action": "done", "list": None, "items": [m.group("x")], "due": None, "weak": True}
    # ---- add
    m = re.match(rf"^(?:please )?(?:add|put|include|append|stick) (?P<x>.+?) (?:to|on|in|onto|into) {ON_LIST}$", t, re.I)
    if m:
        key = list_key(_lst(m))
        x = m.group("x")
        due = None
        if key == "todo":
            x, due = _split_due(x)
        return {"action": "add", "list": _lst(m), "items": split_items(x) if key != "todo" else [x], "due": due}
    m = re.match(r"^(?:i (?:need|have|want) to buy|we need to buy|we(?:'re| are) out of|i(?:'m| am) out of|we(?:'ve| have) run out of|i(?:'ve| have) run out of) (?P<x>.+)$", t, re.I)
    if m:
        return {"action": "add", "list": "shopping", "items": split_items(re.sub(r"^(?:some|a|an|more) ", "", m.group("x"), flags=re.I)), "due": None}
    m = re.match(r"^(?:please )?(?:add|create|make|new|set)(?: me)?(?: a| an| new)*(?: new)? (?:task|to-?do|todo)(?: item)?(?: to| for| that says| called|:)? (?P<x>.+)$", t, re.I) \
        or re.match(r"^(?:task|to-?do|todo)\s*[:\-]\s*(?P<x>.+)$", t, re.I) \
        or re.match(r"^(?:add|put) (?P<x>.+?) (?:to|on) my (?:tasks|to-?dos)$", t, re.I)
    if m:
        x, due = _split_due(m.group("x"))
        return {"action": "add", "list": "todo", "items": [x], "due": due}
    return None


def _split_due(x: str) -> tuple[str, Optional[str]]:
    m = DUE_RX.search(x)
    if m and len(x[: m.start()].strip()) >= 2:
        return x[: m.start()].strip(), m.group("when")
    return x, None


class ListsAgent(BaseAgent):
    name = "lists"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd = parse_lists(text)
        if cmd is None:
            try:
                out: ListsOut = self.extract(ListsOut, LISTS, text, ctx, task="lists")
                cmd = out.model_dump()
            except AgentError:
                cmd = {"action": "unknown"}
        store: ListStore = self.svc.lists
        action = cmd.get("action")
        lst = cmd.get("list")
        items = [i for i in (cmd.get("items") or []) if i and i.strip()]
        if action == "add":
            return self._add(store, lst, items, cmd.get("due"), ctx)
        if action == "show":
            return self._show(store, lst, ctx)
        if action in ("done", "remove"):
            return self._close(store, lst, items, action, ctx)
        if action == "clear":
            return self._clear(store, lst, items, ctx)
        return AgentReply(self.name, "I keep your to-do list, shopping list, notes and any other list. "
                                     "Try \"add milk to my shopping list\" or \"what are my tasks\".")

    # ------------------------------------------------------------------ add
    def _add(self, store, lst, items, due_phrase, ctx) -> AgentReply:
        if not items:
            return AgentReply(self.name, f"What should I add to your {list_label(list_key(lst))}?", clarify="content")
        key = list_key(lst)
        if any(states_secret(i) for i in items):
            return AgentReply(self.name, "I don't store passwords, codes, card or ID numbers, even in notes.")
        due_iso, due_text = None, ""
        if due_phrase:
            r = resolve(due_phrase, ctx.now)
            if r:
                d = r.start if r.has_time else r.start.replace(hour=23, minute=59)
                due_iso, due_text = d.isoformat(), f", due {fmt_time(d, ctx.now) if r.has_time else _day(d, ctx.now)}"
        added, dup = [], []
        try:
            for it in items[:20]:
                row = store.add(key, it, due_iso)
                if row is None:
                    continue
                (dup if row.get("duplicate") else added).append(row["text"])
        except ValueError as e:
            return AgentReply(self.name, str(e))
        self.svc.db.audit(self.name, "lists.add", {"list": key, "n": len(added)}, "done")
        if not added and dup:
            return AgentReply(self.name, f"{_join(dup)} {'is' if len(dup) == 1 else 'are'} already on your {list_label(key)}.")
        if key == "notes":
            return AgentReply(self.name, "Noted.", focus={"last_list": key})
        n = len(store.items(key))
        extra = f" {_join(dup)} {'was' if len(dup) == 1 else 'were'} already there." if dup else ""
        return AgentReply(self.name, f"Added {_join(added)} to your {list_label(key)}{due_text}. {n} item{'s' if n != 1 else ''} on it now.{extra}",
                          focus={"last_list": key}, cards=[self._card(store, key)])

    # ------------------------------------------------------------------ show
    def _show(self, store, lst, ctx) -> AgentReply:
        if lst == "*":
            ls = [x for x in store.lists() if x["open"]]
            if not ls:
                return AgentReply(self.name, "You don't have any lists yet. Say \"add milk to my shopping list\" to start one.")
            return AgentReply(self.name, "Your lists: " + ", ".join(f"{list_label(x['list'])} ({x['open']})" for x in ls) + ".")
        key = list_key(lst or ctx.focus.get("last_list"))
        rows = store.items(key)
        if not rows:
            return AgentReply(self.name, f"Your {list_label(key)} is empty." if key != "notes" else "You have no notes.",
                              focus={"last_list": key})
        shown = rows[:8]
        if key == "todo":
            parts = [r["text"] + (f" (due {_day(datetime.fromisoformat(r['due']), ctx.now)})" if r.get("due") else "") for r in shown]
            overdue = store.overdue(ctx.now)
            lead = f"You have {len(rows)} task{'s' if len(rows) != 1 else ''}"
            if overdue:
                lead += f", {len(overdue)} overdue"
        else:
            parts = [r["text"] for r in shown]
            lead = f"{len(rows)} {'note' if key == 'notes' else 'item'}{'s' if len(rows) != 1 else ''}" + \
                   ("" if key == "notes" else f" on your {list_label(key)}")
        more = f", and {len(rows) - len(shown)} more" if len(rows) > len(shown) else ""
        return AgentReply(self.name, f"{lead}: {'; '.join(parts)}{more}.", focus={"last_list": key}, cards=[self._card(store, key)])

    # ------------------------------------------------------------------ done / remove
    def _close(self, store, lst, items, action, ctx) -> AgentReply:
        if not items:
            return AgentReply(self.name, "Which item?", clarify="content")
        key = list_key(lst) if lst else None
        hit, miss, where = [], [], key
        for it in items[:20]:
            it = re.sub(r"^(?:the|my) ", "", it.strip(), flags=re.I)
            row = store.find(key, it) or (store.find(ctx.focus.get("last_list"), it) if not key and ctx.focus.get("last_list") else None) \
                or (store.find(None, it) if not key else None)
            if not row:
                miss.append(it)
                continue
            (store.done if action == "done" else store.remove)(row["id"])
            hit.append(row["text"])
            where = row["list"]
        self.svc.db.audit(self.name, f"lists.{action}", {"n": len(hit)}, "done")
        if not hit:
            return AgentReply(self.name, f"I couldn't find {_join(miss)} on {'your ' + list_label(key) if key else 'any list'}.")
        left = len(store.items(where))
        verb = "Ticked off" if action == "done" else "Removed"
        tail = f" I couldn't find {_join(miss)}." if miss else ""
        rest = "Nothing left on it." if left == 0 else f"{left} left."
        return AgentReply(self.name, f"{verb} {_join(hit)}. {rest}{tail}", focus={"last_list": where}, cards=[self._card(store, where)])

    def _clear(self, store, lst, items, ctx) -> AgentReply:
        key = list_key(lst)
        only_done = items == ["__done__"]
        n = len(store.items(key, include_done=True)) if not only_done else len([r for r in store.items(key, include_done=True) if r["done"]])
        if n == 0:
            return AgentReply(self.name, f"Your {list_label(key)} is already empty." if not only_done else "No finished tasks to clear.")
        out = self.propose("lists.clear", {"list": key, "only_done": only_done, "count": n}, ctx)
        return AgentReply(self.name, self.say_outcome(out), [out])

    @staticmethod
    def _card(store, key) -> dict:
        rows = store.items(key, include_done=True)[:30]
        return {"type": "list", "list": key, "title": list_label(key).capitalize(),
                "items": [{"text": r["text"], "done": bool(r["done"]), "due": r.get("due")} for r in rows]}


def _join(xs: list[str]) -> str:
    xs = [x for x in xs if x]
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def _day(d: datetime, now: datetime) -> str:
    delta = (d.date() - now.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta < 7:
        return d.strftime("%A")
    return f"{d.day} {d.strftime('%b')}"


# ============================================================================ money
class MoneyOut(_Lenient):
    action: Literal["add", "report", "budget", "undo", "unknown"]
    amount: Optional[float] = Field(None, ge=0)
    note: Optional[str] = None
    period: Optional[str] = None
    category: Optional[str] = None
    when: Optional[str] = None


AMOUNT = r"(?:rs\.?|₹|inr|\$|usd|eur|€)?\s*(?P<amt>\d[\d,]*(?:\.\d{1,2})?)\s*(?P<k>k|lakh|lakhs)?\s*(?:rs\.?|rupees?|bucks|inr|/-)?"
PERIOD_RX = re.compile(r"\b(today|yesterday|this week|last week|this month|last month|this year)\b", re.I)
SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _amount(m) -> float:
    d = m.groupdict()
    v = float((d.get("amt") or d.get("amt2")).replace(",", ""))
    k = (d.get("k") or "").lower()
    return v * (1000 if k == "k" else 100000 if k.startswith("lakh") else 1)


def parse_money(text: str) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.strip()).rstrip("?.! ")
    low = t.lower()
    if re.match(r"^(?:undo|delete|remove|cancel)(?: that| the| my)? (?:last |previous )?(?:expense|spend|entry|transaction)$", low):
        return {"action": "undo"}
    m = re.match(rf"^(?:set|make|change|update)(?: a| my| the)?(?: monthly)? budget(?: for (?:this|the) month)?(?: of| to| as| at)? {AMOUNT}(?: (?:for|per|a) (?:this |the )?month)?$", low)
    if m:
        return {"action": "budget", "amount": _amount(m)}
    if re.match(r"^(?:what(?:'s| is) my (?:monthly )?budget|how much (?:budget|money) (?:is|do i have) left|budget (?:left|status|remaining)|am i (?:within|over) (?:my )?budget)", low):
        return {"action": "budget", "amount": None}
    if re.search(r"\b(how much (?:did|have) i (?:spen[dt]|paid)|how much (?:am i|was i) spending|what did i spend|(?:show|list|what are)(?: me)? my (?:expenses|spending)|"
                 r"my (?:expenses|spending)|total (?:expenses|spending|spent)|expense (?:report|summary)|spending (?:report|summary)|where (?:did|does) my money go)\b", low):
        p = PERIOD_RX.search(low)
        cat = next((c for c in list(CATEGORIES) + ["other"] if re.search(rf"\bon {c}\b|\b{c} (?:expenses|spending)\b", low)), None)
        if not cat:
            on = re.search(r"\bon ([a-z ]+?)(?: (?:today|yesterday|this|last)\b|$)", low)
            if on:
                from jarvis.core.stores import categorise
                c = categorise(on.group(1))
                cat = c if c != "other" else None
        return {"action": "report", "period": p.group(1) if p else None, "category": cat}
    m = re.match(rf"^(?:i |we |just )?(?:just )?(?:spent|paid|payed|blew|shelled out|dropped)(?: about| around| nearly)? {AMOUNT}(?: (?:on|for|at|to|in) (?P<note>.+?))?$", t, re.I) \
        or re.match(rf"^(?:add|log|record|note|track)(?: an| a)?(?: expense)?(?: of)? {AMOUNT}(?: (?:on|for|at|to|as) (?P<note>.+?))?$", t, re.I) \
        or re.match(rf"^(?:i )?(?:bought|got|ordered|had|paid for) (?P<note>.+?) (?:for|at|costing|worth) {AMOUNT}$", t, re.I) \
        or re.match(rf"^(?:the |my )?(?P<note>[a-z][a-z ]{{1,30}}?) (?:cost|costs|came to|set me back)(?: me)? {AMOUNT}$", t, re.I) \
        or re.match(r"^(?:the |my )?(?P<note>[a-z][a-z ]{1,30}?) was (?:(?:rs\.?|₹|inr)\s*(?P<amt>\d[\d,]*(?:\.\d{1,2})?)(?P<k>k)?|"
                    r"(?P<amt2>\d[\d,]*(?:\.\d{1,2})?)\s*(?:rs\.?|rupees?|bucks))$", t, re.I)
    if m:
        note = (m.groupdict().get("note") or "").strip()
        when = None
        p = re.search(r"\b(today|yesterday|last night|this morning)\b", note, re.I)
        if p:
            when, note = p.group(1), (note[: p.start()] + note[p.end():]).strip(" ,")
        return {"action": "add", "amount": _amount(m), "note": note, "when": when}
    return None


class MoneyAgent(BaseAgent):
    name = "money"

    def _fmt(self, v: float) -> str:
        sym = SYMBOL.get(self.svc.expenses.currency, self.svc.expenses.currency + " ")
        return f"{sym}{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{sym}{v:,.2f}"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd = parse_money(text)
        if cmd is None:
            try:
                cmd = self.extract(MoneyOut, MONEY, text, ctx, task="money").model_dump()
            except AgentError:
                cmd = {"action": "unknown"}
        store: ExpenseStore = self.svc.expenses
        a = cmd.get("action")
        if a == "add":
            return self._add(store, cmd, ctx)
        if a == "report":
            return self._report(store, cmd, ctx)
        if a == "budget":
            return self._budget(store, cmd, ctx)
        if a == "undo":
            last = store.last()
            if not last:
                return AgentReply(self.name, "There's no expense to remove.")
            store.remove(last["id"])
            self.svc.db.audit(self.name, "money.undo", {}, "done")
            return AgentReply(self.name, f"Removed {self._fmt(last['amount'])} for {last['note']}.")
        return AgentReply(self.name, "I can track spending. Try \"I spent 250 on lunch\" or \"how much did I spend this week\".")

    def _add(self, store, cmd, ctx) -> AgentReply:
        amt = cmd.get("amount")
        if not amt:
            return AgentReply(self.name, "How much was it?", clarify="amount")
        when = ctx.now
        w = (cmd.get("when") or "").lower()
        if "yesterday" in w or "last night" in w:
            when = ctx.now - timedelta(days=1)
        try:
            row = store.add(float(amt), cmd.get("note") or "", when, cmd.get("category"))
        except ValueError as e:
            return AgentReply(self.name, str(e))
        self.svc.db.audit(self.name, "money.add", {"category": row["category"]}, "done")
        start, end, _ = period("this month", ctx.now)
        month = store.summarise(store.between(start, end))
        text = f"Logged {self._fmt(row['amount'])} for {row['note']} under {row['category']}. {self._fmt(month['total'])} spent this month."
        b = store.budget()
        if b:
            left = b - month["total"]
            text += f" {self._fmt(left)} of your budget left." if left >= 0 else f" That's {self._fmt(-left)} over your {self._fmt(b)} budget."
        return AgentReply(self.name, text, cards=[self._card(store, ctx.now, "this month")])

    def _report(self, store, cmd, ctx) -> AgentReply:
        start, end, label = period(cmd.get("period") or "this month", ctx.now)
        cat = cmd.get("category") if cmd.get("category") in list(CATEGORIES) + ["other"] else None
        rows = store.between(start, end, cat)
        if not rows:
            return AgentReply(self.name, f"No {'%s ' % cat if cat else ''}expenses logged {label}.")
        s = store.summarise(rows)
        if cat:
            text = f"You spent {self._fmt(s['total'])} on {cat} {label}, across {s['count']} expense{'s' if s['count'] != 1 else ''}."
        else:
            top = list(s["by_category"].items())[:3]
            text = f"You spent {self._fmt(s['total'])} {label}. " + "Most on " + ", ".join(f"{c} ({self._fmt(v)})" for c, v in top) + "."
            b = store.budget()
            if b and label == "this month":
                left = b - s["total"]
                text += f" {self._fmt(left)} of your {self._fmt(b)} budget left." if left >= 0 else f" You're {self._fmt(-left)} over budget."
        return AgentReply(self.name, text, cards=[self._card(store, ctx.now, label, cat)])

    def _budget(self, store, cmd, ctx) -> AgentReply:
        if cmd.get("amount"):
            store.set_budget(float(cmd["amount"]))
            self.svc.db.audit(self.name, "money.budget", {}, "done")
            return AgentReply(self.name, f"Monthly budget set to {self._fmt(float(cmd['amount']))}.")
        b = store.budget()
        if not b:
            return AgentReply(self.name, "You haven't set a budget. Say \"set my monthly budget to 15000\".")
        start, end, _ = period("this month", ctx.now)
        spent = store.summarise(store.between(start, end))["total"]
        left = b - spent
        return AgentReply(self.name, f"Budget {self._fmt(b)}, spent {self._fmt(spent)} this month, " +
                          (f"{self._fmt(left)} left." if left >= 0 else f"{self._fmt(-left)} over."))

    def _card(self, store, now, label, cat=None) -> dict:
        start, end, label = period(label, now)
        rows = store.between(start, end, cat)
        s = store.summarise(rows)
        return {"type": "money", "label": label, "total": self._fmt(s["total"]), "budget": self._fmt(store.budget()) if store.budget() else None,
                "ratio": min(1.0, s["total"] / store.budget()) if store.budget() else None,
                "categories": [{"name": c, "amount": self._fmt(v), "share": round(v / s["total"], 3) if s["total"] else 0}
                               for c, v in s["by_category"].items()],
                "recent": [{"note": r["note"], "amount": self._fmt(r["amount"]), "category": r["category"]} for r in rows[:5]]}


# ============================================================================ routines
class RoutineOut(_Lenient):
    action: Literal["create", "run", "list", "delete", "show", "unknown"]
    name: Optional[str] = None
    steps: list[str] = Field(default_factory=list)


STEP_VERBS = (r"mail|email|text|message|tell|send|remind|set|book|schedule|turn|open|call|post|cancel|move|add|create|check|find|play|start|"
              r"read|brief|show|give|what|mute|unmute|pause|switch|enable|disable|wake|log|note|navigate|whatsapp|close|stop|lower|raise|put")


def split_steps(text: str) -> list[str]:
    parts = re.split(rf"\s*(?:;|,\s*(?:and\s+)?(?:then\s+)?|\s+and then\s+|\s+then\s+|\s+after that\s+|\s+and\s+(?=(?:{STEP_VERBS})\b))\s*", text.strip())
    return [p.strip(" .") for p in parts if len(p.strip(" .")) > 2]


def parse_routine(text: str) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.strip()).rstrip("?.! ")
    low = t.lower()
    if re.match(r"^(?:what|which) routines do i have|^(?:list|show)(?: me)?(?: all)? (?:my |the )?routines$|^my routines$", low):
        return {"action": "list"}
    m = re.match(r"^(?:delete|remove|forget|drop)(?: my| the)? (?P<n>.+?) routine$", low)
    if m:
        return {"action": "delete", "name": m.group("n")}
    m = re.match(r"^(?:what does|what(?:'s| is) in|show(?: me)?)(?: my| the)? (?P<n>.+?) routine(?: do)?$", low)
    if m:
        return {"action": "show", "name": m.group("n")}
    m = re.match(r"^(?:create|make|add|set up|save|define|new)(?: me)?(?: a| my| new)*(?: new)? routine(?: called| named| for)? (?P<n>[^:,]+?)\s*(?:[:,]| that | to | which | where you )\s*(?P<s>.+)$", t, re.I) \
        or re.match(r"^(?:when(?:ever)? i say|if i say) [\"']?(?P<n>[^,:\"']+?)[\"']?\s*(?:[,:]|\bthen\b)\s*(?P<s>.+)$", t, re.I) \
        or re.match(r"^(?:my |the )?(?P<n>[a-z ]+?) routine (?:is|should be|should|means)\s*[:,]?\s*(?P<s>.+)$", t, re.I)
    if m:
        return {"action": "create", "name": m.group("n").strip(), "steps": split_steps(m.group("s"))}
    m = re.match(r"^(?:run|start|do|trigger|begin|activate|launch|execute)(?: my| the)? (?P<n>.+?) (?:routine|mode)$", low)
    if m:
        return {"action": "run", "name": m.group("n")}
    if re.match(r"^(?:create|make|add|new|set up)(?: a| my)?(?: new)? routine$", low):
        return {"action": "create", "name": None, "steps": []}
    return None


class RoutineAgent(BaseAgent):
    """User-defined shortcuts: one phrase runs several requests, each through its normal agent, permissions and approvals."""
    name = "routines"

    def handle(self, text: str, ctx: TurnContext) -> AgentReply:
        cmd = parse_routine(text)
        if cmd is None:
            try:
                cmd = self.extract(RoutineOut, ROUTINE, text, ctx, task="routine").model_dump()
            except AgentError:
                cmd = {"action": "unknown"}
        store: RoutineStore = self.svc.routines
        a = cmd.get("action")
        if a == "list":
            rs = store.all()
            if not rs:
                return AgentReply(self.name, "No routines yet. Try: \"when I say good night, turn on do not disturb and set an alarm for 6:30\".")
            return AgentReply(self.name, f"You have {len(rs)} routine{'s' if len(rs) != 1 else ''}: " + "; ".join(
                f"{r['name']} ({len(r['steps'])} step{'s' if len(r['steps']) != 1 else ''})" for r in rs) + ".",
                cards=[{"type": "routines", "items": [{"name": r["name"], "steps": r["steps"]} for r in rs]}])
        if a == "create":
            name, steps = cmd.get("name"), [s for s in (cmd.get("steps") or []) if s.strip()]
            if not name or not steps:
                return AgentReply(self.name, "Tell me the name and the steps, like: \"when I say good night, turn on do not disturb "
                                             "and set an alarm for 6:30\".")
            if RoutineStore.key(name) in RESERVED:
                return AgentReply(self.name, f"\"{name}\" already means something to me. Pick another name.")
            steps = steps[: RoutineStore.MAX_STEPS]
            if any(RoutineStore.key(s) == RoutineStore.key(name) for s in steps):
                return AgentReply(self.name, "A routine can't run itself.")
            try:
                key = store.save(name, steps)
            except ValueError as e:
                return AgentReply(self.name, str(e))
            self.svc.db.audit(self.name, "routines.save", {"name": key, "steps": len(steps)}, "done")
            return AgentReply(self.name, f"Saved. Say \"{key}\" and I'll: " + "; ".join(f"{i + 1}) {s}" for i, s in enumerate(steps)) + ".",
                              cards=[{"type": "routines", "items": [{"name": key, "steps": steps}]}])
        if a in ("delete", "show", "run"):
            r = store.get(cmd.get("name") or "")
            if not r:
                return AgentReply(self.name, f"You don't have a routine called {cmd.get('name') or 'that'}." +
                                  (" You have: " + ", ".join(x["name"] for x in store.all()) + "." if store.all() else ""))
            if a == "delete":
                store.delete(r["name"])
                return AgentReply(self.name, f"Deleted the {r['name']} routine.")
            if a == "show":
                return AgentReply(self.name, f"{r['name'].capitalize()}: " + "; ".join(f"{i + 1}) {s}" for i, s in enumerate(r["steps"])) + ".")
            store.ran(r["name"])
            return AgentReply(self.name, f"Running {r['name']}.", focus={"plan_steps": r["steps"]})
        return AgentReply(self.name, "Routines run several things from one phrase. Try: \"when I say leaving home, turn off wifi "
                                     "and tell me how long to reach the lab\".")


# Names a routine may not take: they are commands JARVIS must always understand the same way.
RESERVED = {"yes", "no", "ok", "okay", "cancel", "stop", "lockdown", "lock down", "unlock", "approve", "approve all", "help",
            "never mind", "thanks", "thank you", "hello", "hi", "hey"}
