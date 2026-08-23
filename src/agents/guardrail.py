"""
Guardrail Agent — mediates every state-changing action before execution.

Design rationale (this is the part to be able to explain in an interview):
- This is deliberately NOT an LLM call. Risk classification here is a bounded
  categorization task (which category of action, on which class of target),
  not open-ended reasoning — a fast local classifier is both cheaper AND
  faster than an LLM API round-trip, which matters because this sits on the
  critical path before every single state-changing action.
- Real precedent: SafeClaw-R (2026) found 36.4% of a real personal-agent
  framework's built-in skills posed high/critical risk without a mediation
  layer like this one. The core design principle borrowed from that work:
  actions must be mediated PRIOR to execution, not audited after the fact.

Risk model: every action is scored on two independent axes —
  1. Action severity: what kind of action is this (read < draft < send/write < delete/financial)
  2. Target sensitivity: what is it acting on (self-only < known-contact < external/unknown)
A HIGH verdict on either axis alone is enough to require human approval —
severity and sensitivity are not averaged, because a highly sensitive
low-severity action (e.g., reading someone else's private notes) is just as
worth stopping as a high-severity action on a safe target.
"""

from dataclasses import dataclass
from enum import Enum


class ActionSeverity(Enum):
    READ = 1          # look something up, no side effects
    DRAFT = 2          # produce a draft, nothing sent/committed yet
    WRITE = 3          # calendar write, send a message, commit to something
    IRREVERSIBLE = 4    # delete, financial transaction, external commitment with a third party


class TargetSensitivity(Enum):
    SELF = 1            # affects only the user's own data/schedule
    KNOWN_CONTACT = 2   # affects a contact already in memory
    EXTERNAL_UNKNOWN = 3  # affects someone/something not in memory, or leaves the system


class Verdict(Enum):
    AUTO_APPROVE = "auto_approve"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass
class ActionRequest:
    """What an upstream agent is asking the Guardrail to clear."""
    agent_name: str
    action_description: str
    severity: ActionSeverity
    target_sensitivity: TargetSensitivity
    is_reversible: bool = True


@dataclass
class GuardrailDecision:
    verdict: Verdict
    reason: str
    action: ActionRequest


class GuardrailAgent:
    """
    Local, dependency-free risk classifier. No LLM call, no network access —
    this is intentional (see module docstring).
    """

    # Any severity >= this threshold requires approval regardless of target.
    SEVERITY_APPROVAL_THRESHOLD = ActionSeverity.WRITE

    # Any target sensitivity >= this threshold requires approval regardless of severity.
    SENSITIVITY_APPROVAL_THRESHOLD = TargetSensitivity.EXTERNAL_UNKNOWN

    def evaluate(self, request: ActionRequest) -> GuardrailDecision:
        # Irreversible actions are never auto-approved, full stop, regardless
        # of how "safe" the target looks. This is a hard rule, not a scored one.
        if request.severity == ActionSeverity.IRREVERSIBLE or not request.is_reversible:
            return GuardrailDecision(
                verdict=Verdict.REQUIRE_APPROVAL,
                reason="Irreversible action — always requires explicit human approval.",
                action=request,
            )

        severity_flags = request.severity.value >= self.SEVERITY_APPROVAL_THRESHOLD.value
        sensitivity_flags = (
            request.target_sensitivity.value >= self.SENSITIVITY_APPROVAL_THRESHOLD.value
        )

        if severity_flags or sensitivity_flags:
            reasons = []
            if severity_flags:
                reasons.append(f"severity={request.severity.name} meets approval threshold")
            if sensitivity_flags:
                reasons.append(f"target={request.target_sensitivity.name} meets approval threshold")
            return GuardrailDecision(
                verdict=Verdict.REQUIRE_APPROVAL,
                reason="; ".join(reasons),
                action=request,
            )

        return GuardrailDecision(
            verdict=Verdict.AUTO_APPROVE,
            reason="Low severity, low sensitivity — within auto-approve bounds.",
            action=request,
        )
