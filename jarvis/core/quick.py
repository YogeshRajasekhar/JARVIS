"""
Answers that must never need a language model: the time, the date, arithmetic and unit conversions.

These run before routing. They are exact, instant, work offline, and cost no free-tier quota. A model
asked "what is 15% of 2400" can be confidently wrong; this cannot.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------- time and date
CITY_TZ = {
    "india": "Asia/Kolkata", "chennai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "hyderabad": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "germany": "Europe/Berlin", "berlin": "Europe/Berlin", "darmstadt": "Europe/Berlin", "frankfurt": "Europe/Berlin", "munich": "Europe/Berlin",
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London", "paris": "Europe/Paris", "france": "Europe/Paris",
    "zurich": "Europe/Zurich", "switzerland": "Europe/Zurich", "amsterdam": "Europe/Amsterdam", "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York", "washington": "America/New_York",
    "chicago": "America/Chicago", "california": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles", "toronto": "America/Toronto",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "singapore": "Asia/Singapore", "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "beijing": "Asia/Shanghai", "china": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong", "seoul": "Asia/Seoul", "korea": "Asia/Seoul", "moscow": "Europe/Moscow",
}
_TIME_RX = re.compile(r"^(?:what(?:'s| is)? (?:the )?(?:current )?time(?: is it)?(?: now| right now)?|what time is it(?: now| right now)?|"
                      r"(?:tell me |do you have )?the time|time now|current time)(?: (?:in|at) (?P<city>[a-z .]+?))?[?.! ]*$", re.I)
_DATE_RX = re.compile(r"^(?:what(?:'s| is)? (?:the |today'?s )?(?:date|day)(?: (?:is it|today|is it today))?|what day is (?:it|today)|"
                      r"which day is (?:it|today)|today'?s date|what(?:'s| is) today)(?: today)?[?.! ]*$", re.I)
_DAY_OF_RX = re.compile(r"^what (?:day|date) (?:is|was|will) (?:it )?(?:be )?(?:on )?(?P<when>.+?)[?.! ]*$", re.I)


def _fmt_clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _fmt_date(dt: datetime) -> str:
    return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')}"


def time_date(text: str, now: datetime) -> Optional[str]:
    t = text.strip()
    m = _TIME_RX.match(t)
    if m:
        city = (m.group("city") or "").strip().lower()
        if city:
            tz = CITY_TZ.get(city)
            if not tz:
                return None                      # unknown city: let an agent (web) answer
            there = now.astimezone(ZoneInfo(tz))
            day = "" if there.date() == now.date() else (" tomorrow" if there.date() > now.date() else " yesterday")
            return f"It's {_fmt_clock(there)}{day} in {city.title()}."
        return f"It's {_fmt_clock(now)}."
    if _DATE_RX.match(t):
        return f"Today is {_fmt_date(now)}."
    m = _DAY_OF_RX.match(t)
    if m:
        from jarvis.core.timeparse import resolve
        when = m.group("when").strip()
        if re.fullmatch(r"(it|today|it today)", when, re.I):
            return f"Today is {_fmt_date(now)}."
        r = resolve(when, now)
        if r:
            verb = "was" if r.start.date() < now.date() else "is"
            return f"{when[0].upper() + when[1:]} {verb} {_fmt_date(r.start)}."
    return None


# ---------------------------------------------------------------------------- arithmetic
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log10, "ln": math.log, "sin": lambda d: math.sin(math.radians(d)),
          "cos": lambda d: math.cos(math.radians(d)), "tan": lambda d: math.tan(math.radians(d))}
_CONST = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 200 or abs(a) > 1e6):
            raise ValueError("too large")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and len(node.args) == 1 and not node.keywords:
        return _FUNCS[node.func.id](_eval(node.args[0]))
    if isinstance(node, ast.Name) and node.id in _CONST:
        return _CONST[node.id]
    raise ValueError("not arithmetic")


def _num(x: float) -> str:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        raise ValueError("not a number")
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) < 1e6 else f"{x:,.2f}"


_WORDS = [
    (r"\bmultiplied by\b|\btimes\b|×|\bx\b(?=\s*\d)", "*"), (r"\bdivided by\b|\bover\b|÷", "/"), (r"\bplus\b|\band\b(?=\s*\d)", "+"),
    (r"\bminus\b|\bless\b", "-"), (r"\bto the power of\b|\braised to\b|\^", "**"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
    (r"\bmod(?:ulo)?\b", "%"),
]
_MATH_LEAD = re.compile(r"^(?:what(?:'s| is)|calculate|compute|how much is|solve|evaluate|work out)\s+", re.I)


def calculate(text: str) -> Optional[str]:
    t = text.strip().rstrip("?.!= ").lower()
    had_lead = bool(_MATH_LEAD.match(t))
    t = _MATH_LEAD.sub("", t)
    # "15% of 2400", "15 percent of 2400"
    m = re.fullmatch(r"(-?[\d,.]+)\s*(?:%|percent)\s*of\s*(-?[\d,.]+)", t)
    if m:
        a, b = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        return f"{_num(a)}% of {_num(b)} is {_num(a * b / 100)}."
    m = re.fullmatch(r"(-?[\d,.]+)\s*(?:is what (?:%|percent(?:age)?) of|as a (?:%|percent(?:age)?) of)\s*(-?[\d,.]+)", t)
    if m:
        a, b = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        return f"{_num(a)} is {_num(a / b * 100)}% of {_num(b)}." if b else None
    m = re.fullmatch(r"(?:the )?(square|cube) root of (-?[\d,.]+)", t)
    if m:
        v = float(m.group(2).replace(",", ""))
        if v < 0:
            return "That has no real root."
        return f"The {m.group(1)} root of {_num(v)} is {_num(v ** (0.5 if m.group(1) == 'square' else 1 / 3))}."
    expr = t
    for rx, sym in _WORDS:
        expr = re.sub(rx, f" {sym} ", expr)
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)
    expr = re.sub(r"(\d(?:\.\d+)?)\s*%", r"(\1/100)", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    if not re.fullmatch(r"[\d\s.+\-*/%()a-z]+", expr) or not re.search(r"\d", expr):
        return None
    if not re.search(r"[+\-*/%]|\b(sqrt|log|ln|sin|cos|tan|abs)\b", expr):
        return None                              # a bare number isn't a sum
    if not had_lead and re.search(r"[a-z]", re.sub(r"\b(sqrt|log|ln|sin|cos|tan|abs|round|pi|e)\b", "", expr)):
        return None
    if not had_lead and re.fullmatch(r"\d{1,4}[-/]\d{1,2}([-/]\d{1,4})?", t.replace(" ", "") if " " not in t else ""):
        return None                              # "12/11" or "2026-11-14" is probably a date
    try:
        value = _eval(ast.parse(expr, mode="eval"))
        return f"{_pretty(expr)} = {_num(value)}"
    except ZeroDivisionError:
        return "You can't divide by zero."
    except (ValueError, SyntaxError, OverflowError, TypeError):
        return None


def _pretty(expr: str) -> str:
    return re.sub(r"\s+", " ", expr.replace("**", "^").replace("*", "×").replace("/", "÷")).strip()


# ---------------------------------------------------------------------------- unit conversion
_LINEAR = {  # everything in terms of a base unit per dimension
    "length": {"mm": .001, "cm": .01, "m": 1, "km": 1000, "inch": .0254, "ft": .3048, "yard": .9144, "mile": 1609.344},
    "mass": {"mg": 1e-6, "g": .001, "kg": 1, "tonne": 1000, "oz": .0283495, "lb": .453592},
    "volume": {"ml": .001, "l": 1, "cup": .236588, "gallon": 3.78541, "tsp": .00492892, "tbsp": .0147868},
    "speed": {"kmph": 1, "mph": 1.609344, "mps": 3.6, "knot": 1.852},
    "data": {"kb": 1, "mb": 1024, "gb": 1024 ** 2, "tb": 1024 ** 3},
    "time": {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800},
    "area": {"sqm": 1, "sqft": .092903, "acre": 4046.86, "hectare": 10000},
}
_ALIAS = {
    "millimeter": "mm", "millimetre": "mm", "centimeter": "cm", "centimetre": "cm", "meter": "m", "metre": "m", "kilometer": "km",
    "kilometre": "km", "kms": "km", "in": "inch", "inches": "inch", "foot": "ft", "feet": "ft", "yards": "yard", "miles": "mile", "mi": "mile",
    "milligram": "mg", "gram": "g", "gm": "g", "kilogram": "kg", "kilo": "kg", "kgs": "kg", "ton": "tonne", "ounce": "oz", "pound": "lb", "lbs": "lb",
    "milliliter": "ml", "millilitre": "ml", "liter": "l", "litre": "l", "cups": "cup", "gallons": "gallon", "teaspoon": "tsp", "tablespoon": "tbsp",
    "km/h": "kmph", "kph": "kmph", "m/s": "mps", "knots": "knot", "kilobyte": "kb", "megabyte": "mb", "gigabyte": "gb", "terabyte": "tb",
    "sec": "second", "secs": "second", "min": "minute", "mins": "minute", "hr": "hour", "hrs": "hour",
    "square meter": "sqm", "square metre": "sqm", "square foot": "sqft", "square feet": "sqft", "sq ft": "sqft", "acres": "acre", "hectares": "hectare",
    "celsius": "c", "centigrade": "c", "fahrenheit": "f", "kelvin": "k", "°c": "c", "°f": "f",
}
_NAMES = {"mm": "mm", "cm": "cm", "m": "m", "km": "km", "inch": "inches", "ft": "feet", "yard": "yards", "mile": "miles", "mg": "mg", "g": "g",
          "kg": "kg", "tonne": "tonnes", "oz": "ounces", "lb": "pounds", "ml": "ml", "l": "litres", "cup": "cups", "gallon": "gallons",
          "tsp": "teaspoons", "tbsp": "tablespoons", "kmph": "km/h", "mph": "mph", "mps": "m/s", "knot": "knots", "kb": "KB", "mb": "MB",
          "gb": "GB", "tb": "TB", "second": "seconds", "minute": "minutes", "hour": "hours", "day": "days", "week": "weeks",
          "sqm": "square metres", "sqft": "square feet", "acre": "acres", "hectare": "hectares", "c": "°C", "f": "°F", "k": "K"}
_CONV_RX = re.compile(r"^(?:convert |what(?:'s| is) |how (?:many|much) (?:is )?)?(?P<n>-?[\d,]*\.?\d+)\s*(?P<a>[a-z°/ ]+?)\s+(?:to|in|into|as|equals?)\s+(?P<b>[a-z°/ ]+?)[?.! ]*$", re.I)
_HOWMANY_RX = re.compile(r"^how many (?P<b>[a-z°/ ]+?) (?:are |is )?(?:there )?in (?:an? )?(?P<n>-?[\d,]*\.?\d+)?\s*(?P<a>[a-z°/ ]+?)[?.! ]*$", re.I)


def _unit(u: str) -> Optional[str]:
    u = re.sub(r"\s+", " ", u.strip().lower()).removeprefix("degrees ").removeprefix("degree ")
    for cand in (u, u.rstrip("s")):
        cand = _ALIAS.get(cand, cand)
        if cand in {"c", "f", "k"} or any(cand in d for d in _LINEAR.values()):
            return cand
    return None


def _one(name: str) -> str:
    return {"inches": "inch", "feet": "foot"}.get(name, name[:-1] if name.endswith("s") and len(name) > 3 else name)


def convert_units(text: str) -> Optional[str]:
    m = _CONV_RX.match(text.strip()) or _HOWMANY_RX.match(text.strip())
    if not m:
        return None
    a, b = _unit(m.group("a")), _unit(m.group("b"))
    if not a or not b or a == b:
        return None
    n = float((m.group("n") or "1").replace(",", ""))
    temp = {"c", "f", "k"}
    if a in temp or b in temp:
        if not (a in temp and b in temp):
            return None
        kelvin = {"c": n + 273.15, "f": (n - 32) * 5 / 9 + 273.15, "k": n}[a]
        out = {"c": kelvin - 273.15, "f": (kelvin - 273.15) * 9 / 5 + 32, "k": kelvin}[b]
        return f"{_num(n)}{_NAMES[a]} is {_num(round(out, 1))}{_NAMES[b]}."
    for dim in _LINEAR.values():
        if a in dim and b in dim:
            out = n * dim[a] / dim[b]
            shown = _num(round(out, 3 if abs(out) < 100 else 1))
            return f"{_num(n)} {_one(_NAMES[a]) if n == 1 else _NAMES[a]} is {shown} {_NAMES[b]}."
    return None


def countdown(target: datetime, now: datetime) -> str:
    """'in 2 hours 15 minutes' style distance to a future time."""
    secs = int((target - now).total_seconds())
    if secs < 60:
        return "right now"
    mins = secs // 60
    if mins < 60:
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    hours, rem = divmod(mins, 60)
    if hours < 24:
        return f"in {hours} hour{'s' if hours != 1 else ''}" + (f" {rem} minute{'s' if rem != 1 else ''}" if rem else "")
    days = (target.date() - now.date()).days
    return f"in {days} day{'s' if days != 1 else ''}"


def answer(text: str, now: datetime) -> Optional[str]:
    return time_date(text, now) or convert_units(text) or calculate(text)


__all__ = ["answer", "calculate", "convert_units", "time_date", "countdown", "timedelta"]
