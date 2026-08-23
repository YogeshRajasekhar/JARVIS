"""
Tests for the Guardrail agent.

These aren't just "does it run" smoke tests — the adversarial cases at the
bottom are specifically trying to get the guardrail to wrongly auto-approve
something it shouldn't. That's the actual bar this module needs to clear.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agents.guardrail import (
    GuardrailAgent,
    ActionRequest,
    ActionSeverity,
    TargetSensitivity,
    Verdict,
)

guardrail = GuardrailAgent()


def test_read_only_self_action_auto_approves():
    """Looking up your own calendar should never need a human in the loop."""
    req = ActionRequest(
        agent_name="scheduler",
        action_description="Check availability on Thursday",
        severity=ActionSeverity.READ,
        target_sensitivity=TargetSensitivity.SELF,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.AUTO_APPROVE


def test_draft_message_to_known_contact_auto_approves():
    """Drafting (not sending) to a known contact is low-risk."""
    req = ActionRequest(
        agent_name="communication",
        action_description="Draft a reply to a known colleague",
        severity=ActionSeverity.DRAFT,
        target_sensitivity=TargetSensitivity.KNOWN_CONTACT,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.AUTO_APPROVE


def test_sending_message_requires_approval():
    """This is the core design intent: draft freely, but sending needs a checkpoint."""
    req = ActionRequest(
        agent_name="communication",
        action_description="Send an email to a colleague",
        severity=ActionSeverity.WRITE,
        target_sensitivity=TargetSensitivity.KNOWN_CONTACT,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL
    assert "severity" in decision.reason


def test_calendar_write_on_self_requires_approval():
    """Even though the target is 'safe' (self), WRITE severity alone is enough to flag."""
    req = ActionRequest(
        agent_name="scheduler",
        action_description="Reschedule a meeting",
        severity=ActionSeverity.WRITE,
        target_sensitivity=TargetSensitivity.SELF,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL


def test_read_action_on_external_unknown_target_requires_approval():
    """
    This is the case that proves severity and sensitivity are checked
    independently, not averaged: a READ action (low severity) on an
    EXTERNAL_UNKNOWN target (high sensitivity) must still require approval.
    A naive averaging scheme would wrongly auto-approve this.
    """
    req = ActionRequest(
        agent_name="memory",
        action_description="Look up information about a person not in memory",
        severity=ActionSeverity.READ,
        target_sensitivity=TargetSensitivity.EXTERNAL_UNKNOWN,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL
    assert "target" in decision.reason


def test_irreversible_action_always_requires_approval_even_if_low_risk_otherwise():
    """
    Adversarial case: try to construct an IRREVERSIBLE action that looks
    'safe' on every other axis (self target). It should still be blocked
    from auto-approval — the hard rule must win regardless of other scores.
    """
    req = ActionRequest(
        agent_name="scheduler",
        action_description="Permanently delete a recurring meeting series",
        severity=ActionSeverity.IRREVERSIBLE,
        target_sensitivity=TargetSensitivity.SELF,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL
    assert "Irreversible" in decision.reason


def test_is_reversible_false_overrides_severity_field():
    """
    Adversarial case: an upstream agent could mislabel severity as DRAFT
    while marking is_reversible=False (e.g., a bug, or a malicious/careless
    caller). The guardrail should catch this via the is_reversible flag
    independent of what severity was claimed.
    """
    req = ActionRequest(
        agent_name="communication",
        action_description="Mislabeled action claiming to be a draft",
        severity=ActionSeverity.DRAFT,
        target_sensitivity=TargetSensitivity.SELF,
        is_reversible=False,
    )
    decision = guardrail.evaluate(req)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL


def test_no_verdict_is_ever_block_by_default():
    """
    Sanity check on current scope: this MVP guardrail only distinguishes
    auto-approve vs. require-approval, it never silently BLOCKs outright.
    This is worth asserting explicitly so it's a deliberate design choice
    that shows up in the tests, not an accident nobody noticed.
    """
    always_ok = ActionRequest(
        agent_name="scheduler",
        action_description="Check availability",
        severity=ActionSeverity.READ,
        target_sensitivity=TargetSensitivity.SELF,
    )
    always_blockish = ActionRequest(
        agent_name="scheduler",
        action_description="Delete everything",
        severity=ActionSeverity.IRREVERSIBLE,
        target_sensitivity=TargetSensitivity.EXTERNAL_UNKNOWN,
    )
    for req in (always_ok, always_blockish):
        assert guardrail.evaluate(req).verdict != Verdict.BLOCK
