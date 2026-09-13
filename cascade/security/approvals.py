from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import Record
from cascade.tools.gateway import ToolAction
from cascade.tools.permissions import Operation


class ApprovalItem(Record):
    action_id: str
    commitment_id: str
    provider: str
    operation: Operation
    risk_tier: int = Field(ge=0, le=5)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    description: str
    reason: str


class ApprovalRequest(Record):
    """One consent decision, bound to a plan and to the world version it was built on."""

    id: str
    plan_id: str
    incident_id: str
    based_on_version: int = Field(ge=0)
    items: tuple[ApprovalItem, ...] = Field(min_length=1)
    total_amount: Decimal = Field(ge=0)
    currency: Literal["EUR"] = "EUR"
    status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    approved_action_ids: tuple[str, ...] = ()
    actor: str | None = None
    note: str = ""
    created_at: AwareDatetime
    decided_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def decided_consistently(self) -> Self:
        if (self.status == "PENDING") != (self.decided_at is None):
            raise ValueError("a decided request records when and by whom it was decided")
        if self.status == "PENDING" and (self.approved_action_ids or self.actor):
            raise ValueError("a pending request carries no grant")
        if self.status == "REJECTED" and self.approved_action_ids:
            raise ValueError("a rejected request grants nothing")
        known = {item.action_id for item in self.items}
        if not set(self.approved_action_ids) <= known:
            raise ValueError("grant references an action outside this request")
        if self.total_amount != sum((i.amount for i in self.items), Decimal("0")):
            raise ValueError("total must equal the sum of its items")
        return self


def approval_request(
    plan_id: str,
    incident_id: str,
    based_on_version: int,
    pending: tuple[tuple[ToolAction, str], ...],
) -> ApprovalRequest:
    items = tuple(
        ApprovalItem(
            action_id=action.id,
            commitment_id=action.commitment_id,
            provider=action.provider,
            operation=action.operation,
            risk_tier=action.risk_tier,
            amount=action.amount,
            description=action.description,
            reason=reason,
        )
        for action, reason in pending
    )
    return ApprovalRequest(
        id=f"apr_{uuid4().hex}",
        plan_id=plan_id,
        incident_id=incident_id,
        based_on_version=based_on_version,
        items=items,
        total_amount=sum((i.amount for i in items), Decimal("0")),
        created_at=datetime.now(UTC),
    )


class ApprovalError(ValueError):
    """An approval cannot be granted as requested."""


def grant(
    request: ApprovalRequest,
    actor: str,
    approved_action_ids: tuple[str, ...],
    acknowledged_amount: Decimal,
) -> ApprovalRequest:
    """Consent must be informed: the caller echoes the exact actions and the total."""
    if request.status != "PENDING":
        raise ApprovalError("this approval request was already decided")
    known = {item.action_id for item in request.items}
    if set(approved_action_ids) != known:
        raise ApprovalError("approval must cover every requested action exactly")
    if acknowledged_amount != request.total_amount:
        raise ApprovalError("acknowledged amount does not match the requested total")
    return request.model_copy(
        update={
            "status": "APPROVED",
            "approved_action_ids": tuple(sorted(approved_action_ids)),
            "actor": actor,
            "decided_at": datetime.now(UTC),
        }
    )


def reject(request: ApprovalRequest, actor: str, note: str) -> ApprovalRequest:
    if request.status != "PENDING":
        raise ApprovalError("this approval request was already decided")
    return request.model_copy(
        update={
            "status": "REJECTED",
            "actor": actor,
            "note": note,
            "decided_at": datetime.now(UTC),
        }
    )
