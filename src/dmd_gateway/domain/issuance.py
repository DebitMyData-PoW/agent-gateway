"""The single rule that decides whether a DebitMyData identity / DID may be issued.

A DID is issued only when ALL of these hold:
  * the payment's normalized status is ``settled``;
  * the controller proof is ``verified``;
  * rate-limit, fraud, policy and compliance checks passed;
  * the registration intent was valid when paid and has not already been consumed.

Callers must use ``issuance_blockers`` / ``can_issue_identity`` instead of re-implementing
the rule, so REST, MCP and background workers cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from dmd_gateway.domain.payment_status import PaymentStatus


class ControllerProofStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IssuanceInputs:
    payment_status: PaymentStatus
    controller_proof: ControllerProofStatus
    policy_checks_passed: bool
    intent_valid: bool
    intent_consumed: bool


def issuance_blockers(inputs: IssuanceInputs) -> list[str]:
    blockers: list[str] = []
    if inputs.payment_status is not PaymentStatus.SETTLED:
        blockers.append(f"payment is {inputs.payment_status}, not settled")
    if inputs.controller_proof is not ControllerProofStatus.VERIFIED:
        blockers.append(f"controller proof is {inputs.controller_proof}, not verified")
    if not inputs.policy_checks_passed:
        blockers.append("policy, risk or compliance checks have not passed")
    if not inputs.intent_valid:
        blockers.append("registration intent is not valid")
    if inputs.intent_consumed:
        blockers.append("registration intent was already used")
    return blockers


def can_issue_identity(inputs: IssuanceInputs) -> bool:
    return not issuance_blockers(inputs)
