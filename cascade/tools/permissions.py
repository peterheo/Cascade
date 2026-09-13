from decimal import Decimal
from typing import Literal

from pydantic import Field

from cascade.domain.models import Record

Operation = Literal[
    "check_availability",
    "reschedule",
    "book",
    "cancel",
    "refund",
    "notify",
]

READ_OPERATIONS: tuple[Operation, ...] = ("check_availability",)
WRITE_OPERATIONS: tuple[Operation, ...] = ("reschedule", "book", "cancel", "refund", "notify")
ALL_OPERATIONS: tuple[Operation, ...] = READ_OPERATIONS + WRITE_OPERATIONS

Decision = Literal["AUTO", "REQUIRES_APPROVAL", "DENIED"]


def operation_mode(operation: Operation) -> Literal["READ", "WRITE"]:
    """Reads never carry a side effect; writes always do. The split is structural."""
    return "READ" if operation in READ_OPERATIONS else "WRITE"


def risk_tier(operation: Operation, amount: Decimal) -> int:
    """Assign the design's risk tier deterministically. A model never picks a tier."""
    if operation in READ_OPERATIONS:
        return 0
    if operation == "notify":
        return 2
    # Any committed spend is tier 4 regardless of which operation carries it.
    return 4 if amount > 0 else 3


class PermissionPolicy(Record):
    """Product-level authorization. Confidence never substitutes for permission."""

    # Tiers at or below this execute without asking; the design's default stops at 2.
    auto_max_tier: int = Field(default=2, ge=0, le=5)
    # Tiers above this cannot be approved in-product at all (tier 5 is out of scope).
    approval_max_tier: int = Field(default=4, ge=0, le=5)
    max_auto_spend: Decimal = Field(default=Decimal("0"), ge=0)
    allowed_operations: tuple[Operation, ...] = ALL_OPERATIONS

    def decide(self, operation: Operation, tier: int, amount: Decimal) -> tuple[Decision, str]:
        if operation not in self.allowed_operations:
            return "DENIED", f"Operation {operation} is not permitted by this policy."
        if tier > self.approval_max_tier:
            return "DENIED", f"Risk tier {tier} is above the approvable maximum."
        if amount > self.max_auto_spend and tier > self.auto_max_tier:
            return "REQUIRES_APPROVAL", f"Spending {amount} EUR requires explicit approval."
        if tier > self.auto_max_tier:
            return "REQUIRES_APPROVAL", f"Risk tier {tier} requires user approval."
        if amount > self.max_auto_spend:
            return "REQUIRES_APPROVAL", f"Spending {amount} EUR exceeds the automatic limit."
        return "AUTO", f"Risk tier {tier} is authorized without approval."
