"""
Deterministic keyword router (the "fast path").

Runs before any LLM call. If one agent clearly wins, the LLM router is skipped entirely:
that saves free-tier quota (~40-60% of everyday requests are unambiguous) and removes a
whole class of misroutes. If the result is ambiguous, the LLM router decides.
"""

from __future__ import annotations

import re

RULES: dict[str, list[tuple[str, float]]] = {
    "device": [
        (r"\b(alarm|wake me|timer|remind me|reminder)\b", 3), (r"\b(open|launch|start) (the )?\w+( app)?\b", 1.5),
        (r"^(please )?(call|dial|ring|phone)\b|\bgive \w+ a (call|ring)\b", 3), (r"^(please )?(text|sms)\b|\bsend an? (text|sms)\b", 3), (r"\b(wifi|wi-fi|bluetooth|flashlight|torch|do not disturb|dnd|silent mode)\b", 3),
        (r"\b(battery|charge level)\b", 3), (r"\bnotifications?\b", 2), (r"\b(where am i|my location|current location)\b", 3),
        (r"\b(phone number|contact details|contact)\b", 1.5), (r"\bturn (on|off)\b", 1),
    ],
    "scheduler": [
        (r"\b(calendar|schedule|meeting|meetings|event|appointment|standup|book|reschedule|postpone|push|move)\b", 2),
        (r"\b(free|available|availability|busy)\b", 1.5), (r"\b(cancel)\b", 1.5),
        (r"\bwhat('s| is| do i have)? (on|planned|happening)\b", 1.5), (r"\bset up a (call|meeting)\b", 2.5),
        (r"\bblock\b.*\b(time|hours?|slot|calendar)\b|\b(focus|gym|study|deep work) (time|block|session)\b", 3),
    ],
    "communication": [
        (r"\b(e-?mail|mail|inbox|gmail|reply|draft)\b", 3), (r"\bslack\b|#[\w-]+", 3),
        (r"\b(send|write|message|dm|post)\b", 1), (r"\b(unread|emails)\b", 2),
    ],
    "memory": [
        (r"^remember\b|\bremember that\b|\bnote that\b|\bdon't forget that\b", 4),
        (r"\b(who is|who's|who introduced|what do (i|you) know about|tell me about)\b", 2.5),
        (r"\b(forget (about|everything))\b", 3), (r"\b(owe|promised|commitment)\b", 2), (r"\bmy (tasks|projects|preferences)\b", 2),
    ],
    "github": [(r"\b(github|repo|repository|pull request|\bpr\b|prs|issue|issues|ci|build|workflow|commit)\b", 3)],
    "places": [
        (r"\b(how (long|far)|travel time|distance|route|directions|commute)\b", 3), (r"\b(where should (we|i) meet|meet(ing)? point|halfway)\b", 3.5),
        (r"\b(when should i leave|leave for)\b", 3.5), (r"\b(near me|nearby|cafe|restaurant)\b", 2), (r"\bwhere is\b", 1.5),
    ],
    "briefing": [
        (r"\b(brief me|briefing|catch me up|what did i miss|my day|daily summary|morning summary|today's summary)\b", 4),
        (r"\bwhat'?s (up|new) today\b", 3), (r"\bwhat'?s up\b", 1),
        (r"\b(overnight|over night|while i (was )?(slept|asleep|sleeping|away)|morning report|night report|"
         r"since i (went to bed|slept|left)|what happened (last night|overnight))\b", 5),
        (r"\b(anyone|anybody|someone|somebody|people|who)\b.{0,30}\b(mad|angry|upset|annoyed|pissed|unhappy|frustrated|cross)\b", 5),
    ],
    "planner": [
        (r"\b(plan my|help me plan|prepare for|prioriti[sz]e|organi[sz]e my|reorgani[sz]e)\b", 3.5),
        (r"\bi need to (prepare|get ready|finish)\b", 2.5),
    ],
    "chat": [
        (r"^(hi|hello|hey|yo|good (morning|evening|afternoon|night)|thanks|thank you|ok|okay|cool|great)\b[\s!.,]*$", 5),
        (r"\b(who are you|what can you do|your name|how are you|tell me a joke)\b", 4),
    ],
}

_COMPILED = {a: [(re.compile(p, re.I), w) for p, w in rules] for a, rules in RULES.items()}

# Words that link two actions. Used to decide whether to call the splitter at all.
STRONG_LINK = re.compile(r";|\band then\b|\band (?:also )?(?:mail|email|text|message|tell|send|remind|set|book|schedule|"
                         r"turn|open|call|post|cancel|move|add|create|check|find|play|start)\b", re.I)
WEAK_LINK = re.compile(r"\b(then|after that|also)\b", re.I)


def score(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for agent, rules in _COMPILED.items():
        s = sum(w for p, w in rules if p.search(text))
        if s:
            out[agent] = s
    # Disambiguations learned from mistakes:
    t = text.lower()
    if re.search(r"\b(schedule|book|set up|setup|arrange|organi[sz]e) (a |an )?(call|meeting|sync|session|chat)\b", t):
        out["scheduler"] = out.get("scheduler", 0) + 3   # "schedule a call" is calendar, not the dialer
    if re.search(r"\bremind me\b", t):
        out["device"] = out.get("device", 0) + 3        # reminders are device, not scheduler/memory
    if re.search(r"\b(mail|email|slack|text|sms)\b", t) and out.get("scheduler"):
        out["scheduler"] -= 1.5                          # "mail Arun about the meeting" is communication
    if "when is my" in t or "when's my" in t:
        out["scheduler"] = out.get("scheduler", 0) + 3
    return {k: v for k, v in out.items() if v > 0}


def fast_route(text: str) -> tuple[str | None, float]:
    """Returns (agent, confidence) if a single agent clearly wins, else (None, 0)."""
    s = score(text)
    if not s:
        return None, 0.0
    ranked = sorted(s.items(), key=lambda kv: -kv[1])
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= 3 and top - second >= 1.5:
        return best, min(0.99, 0.6 + 0.08 * (top - second))
    return None, 0.0


def raw_score(text: str) -> dict[str, float]:
    """Keyword scores without disambiguation (used to spot requests touching 2+ agents)."""
    return {a: s for a, rules in _COMPILED.items() if (s := sum(w for p, w in rules if p.search(text)))}


def looks_multi_intent(text: str) -> bool:
    if STRONG_LINK.search(text):
        return True
    return bool(WEAK_LINK.search(text)) and len([a for a, v in raw_score(text).items() if v >= 2]) >= 2
