"""
Sensitive-data redaction and untrusted-content handling.

Applied (1) to anything before it is sent to an external LLM provider, (2) to anything
before it is stored, and (3) to device data such as notifications before an agent sees it.
Deterministic regexes, no model, so it can't be talked out of its job.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # OTP / verification codes: "OTP is 482913", "code: 4821", "482913 is your verification code"
    ("OTP", re.compile(r"(?i)\b(?:otp|one[- ]time (?:password|code)|verification code|security code|passcode|code)\b"
                       r"[^\d\n]{0,25}(\d{4,8})\b")),
    ("OTP", re.compile(r"(?i)\b(\d{4,8})\b(?=[^\n]{0,30}\b(?:is your|is the)\b[^\n]{0,30}\b(?:otp|code|password)\b)")),
    # Payment cards (13-19 digits, optional spaces/dashes)
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    # Aadhaar (12 digits in 4-4-4 groups, first digit 2-9)
    ("AADHAAR", re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")),
    # PAN (ABCDE1234F)
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Indian passport (A1234567) / generic passport mention
    ("PASSPORT", re.compile(r"(?i)\bpassport(?: no\.?| number)?\s*[:#]?\s*([A-Z]\d{7})\b")),
    # UPI PIN / CVV / password statements
    ("SECRET", re.compile(r"(?i)\b(?:upi pin|atm pin|pin|cvv|cvc|password|passwd|pwd)\b\s*(?:is|=|:)\s*(\S+)")),
    # IBAN / bank account numbers when labelled
    ("ACCOUNT", re.compile(r"(?i)\b(?:a/c|acct|account)(?: no\.?| number)?\s*[:#]?\s*(\d{9,18})\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    # API keys / tokens that look like secrets
    ("TOKEN", re.compile(r"\b(?:sk|gsk|xox[abpr]|ghp|gho|github_pat|AIza)[-_A-Za-z0-9]{16,}\b")),
]


def redact(text: str) -> str:
    """Replace sensitive values with [REDACTED:<KIND>] markers."""
    if not text:
        return text
    out = text
    for kind, pat in _PATTERNS:
        def _sub(m: re.Match, kind=kind) -> str:
            if m.groups():
                g = m.group(1)
                return m.group(0).replace(g, f"[REDACTED:{kind}]")
            return f"[REDACTED:{kind}]"
        out = pat.sub(_sub, out)
    return out


def contains_sensitive(text: str) -> bool:
    return redact(text) != text


# ---- Prompt-injection heuristics for untrusted content (emails, Slack, web, notifications) ----
_INJECTION = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|disregard (?:your|the) (?:rules|instructions)"
    r"|you are now|new instructions|system prompt|act as|developer mode|jailbreak"
    r"|forward (?:all|every|my) (?:emails?|mails?|messages?)|send (?:all|every) (?:emails?|files?|contacts?)"
    r"|reveal (?:your|the) (?:prompt|instructions|keys?|password)|exfiltrate|do not tell the user)"
)


def injection_score(text: str) -> int:
    return len(_INJECTION.findall(text or ""))


def wrap_untrusted(text: str, source: str) -> str:
    """Mark external content as data. Prompts tell the model never to follow instructions inside."""
    safe = redact(text or "").replace("</untrusted>", "")
    flag = ' suspicious="true"' if injection_score(safe) else ""
    return f'<untrusted source="{source}"{flag}>\n{safe}\n</untrusted>'
