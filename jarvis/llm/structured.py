"""
Structured output for small/free models: ask for JSON, validate with pydantic, repair once.

Why this exists: free models (Llama 8B, Gemini Flash-Lite) are good at extraction but
regularly emit almost-JSON (fences, trailing commas, a sentence before the object, an
enum value with different casing). Each failure mode is handled here, in one place,
instead of in every agent:

1. Extract the first JSON object/array from the text (ignores fences and chatter).
2. Light normalisation (trailing commas, smart quotes, Python literals).
3. Pydantic validation (types, enums, ranges) with lenient pre-normalisation of enums.
4. If still invalid: one repair round that shows the model its own output and the exact
   validation error. Small models fix their own mistakes very reliably when told what's wrong.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    pass


_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)


def extract_json_text(raw: str) -> str:
    text = _FENCE.sub("", raw or "").strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise StructuredOutputError(f"No JSON found in: {raw[:200]!r}")
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Unbalanced: return the tail and let the repair step deal with it.
    end = text.rfind(closer)
    return text[start : end + 1] if end > start else text[start:]


def loads_lenient(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fixed = (
        text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    )
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)  # trailing commas
    fixed = re.sub(r"\bNone\b", "null", fixed)
    fixed = re.sub(r"\bTrue\b", "true", fixed)
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    return json.loads(fixed)


def parse_model(raw: str, model: Type[T]) -> T:
    data = loads_lenient(extract_json_text(raw))
    return model.model_validate(data)


def complete_json(llm, model: Type[T], system: str, prompt: str, *, task: Optional[str] = None,
                  max_tokens: int = 600, history: Optional[list] = None, repairs: int = 1) -> T:
    raw = llm.complete(prompt, system=system, json=True, temperature=0.0, max_tokens=max_tokens,
                       task=task, history=history)
    last_err: Exception
    try:
        return parse_model(raw, model)
    except (StructuredOutputError, json.JSONDecodeError, ValidationError) as e:
        last_err = e

    for _ in range(repairs):
        repair_prompt = (
            f"{prompt}\n\nYour previous answer was:\n{raw[:1500]}\n\n"
            f"It was INVALID because: {str(last_err)[:600]}\n"
            "Return ONLY the corrected JSON object. No explanation, no markdown."
        )
        raw = llm.complete(repair_prompt, system=system, json=True, temperature=0.0,
                           max_tokens=max_tokens, task=task)
        try:
            return parse_model(raw, model)
        except (StructuredOutputError, json.JSONDecodeError, ValidationError) as e:
            last_err = e
    raise StructuredOutputError(f"Model output invalid after {repairs} repair(s): {last_err}")
