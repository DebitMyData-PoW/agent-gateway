import pytest

from dmd_gateway.domain.issuance import (
    ControllerProofStatus,
    IssuanceInputs,
    can_issue_identity,
    issuance_blockers,
)
from dmd_gateway.domain.payment_status import PaymentStatus

READY = IssuanceInputs(
    payment_status=PaymentStatus.SETTLED,
    controller_proof=ControllerProofStatus.VERIFIED,
    policy_checks_passed=True,
    intent_valid=True,
    intent_consumed=False,
)


def test_all_conditions_met() -> None:
    assert can_issue_identity(READY)
    assert issuance_blockers(READY) == []


@pytest.mark.parametrize("status", [s for s in PaymentStatus if s is not PaymentStatus.SETTLED])
def test_only_settled_payments_issue(status: PaymentStatus) -> None:
    inputs = IssuanceInputs(**{**_fields(READY), "payment_status": status})
    assert not can_issue_identity(inputs)


@pytest.mark.parametrize(
    "override",
    [
        {"controller_proof": ControllerProofStatus.PENDING},
        {"controller_proof": ControllerProofStatus.REJECTED},
        {"policy_checks_passed": False},
        {"intent_valid": False},
        {"intent_consumed": True},
    ],
)
def test_each_condition_blocks(override: dict) -> None:
    inputs = IssuanceInputs(**{**_fields(READY), **override})
    assert not can_issue_identity(inputs)
    assert len(issuance_blockers(inputs)) == 1


def _fields(inputs: IssuanceInputs) -> dict:
    return {name: getattr(inputs, name) for name in IssuanceInputs.__slots__}
