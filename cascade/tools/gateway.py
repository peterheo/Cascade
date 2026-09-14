from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import Record
from cascade.tools.permissions import (
    Operation,
    PermissionPolicy,
    operation_mode,
    risk_tier,
)

CallDecision = Literal[
    "AUTO",
    "APPROVED",
    "REQUIRES_APPROVAL",
    "DENIED_BY_POLICY",
    "DENIED_BY_SANDBOX",
    "NO_ADAPTER",
]


class PostCondition(Record):
    """The observable state a write must produce before the action counts as done."""

    commitment_present: bool
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    min_refund: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.commitment_present != (self.start_at is not None and self.end_at is not None):
            raise ValueError("a retained commitment requires both expected times")
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError("invalid expected interval")
        if not self.commitment_present and self.min_refund < 0:
            raise ValueError("invalid refund expectation")
        return self


class ToolAction(Record):
    """A single external operation. Mode and tier are derived, not caller-supplied."""

    id: str
    plan_id: str
    commitment_id: str
    provider: str
    operation: Operation
    mode: Literal["READ", "WRITE"]
    risk_tier: int = Field(ge=0, le=5)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    currency: Literal["EUR"] = "EUR"
    option_id: str | None = None
    idempotency_key: str = Field(min_length=1)
    postcondition: PostCondition
    description: str

    @model_validator(mode="after")
    def derived_classification(self) -> Self:
        if self.mode != operation_mode(self.operation):
            raise ValueError("mode is derived from the operation and cannot be overridden")
        if self.risk_tier != risk_tier(self.operation, self.amount):
            raise ValueError("risk tier is derived from the operation and cannot be lowered")
        if self.mode == "READ" and self.amount:
            raise ValueError("a read operation cannot carry a committed amount")
        return self


def build_action(
    *,
    plan_id: str,
    commitment_id: str,
    provider: str,
    operation: Operation,
    idempotency_key: str,
    postcondition: PostCondition,
    description: str,
    amount: Decimal = Decimal("0"),
    option_id: str | None = None,
    action_id: str | None = None,
) -> ToolAction:
    return ToolAction(
        id=action_id or f"act_{uuid4().hex}",
        plan_id=plan_id,
        commitment_id=commitment_id,
        provider=provider,
        operation=operation,
        mode=operation_mode(operation),
        risk_tier=risk_tier(operation, amount),
        amount=amount,
        option_id=option_id,
        idempotency_key=idempotency_key,
        postcondition=postcondition,
        description=description,
    )


class ToolResult(Record):
    success: bool
    provider: str
    operation: Operation
    external_reference: str | None = None
    side_effect: bool = False
    verified: bool = False
    raw_result_ref: str
    detail: str
    observed_start_at: AwareDatetime | None = None
    observed_end_at: AwareDatetime | None = None
    refund_amount: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.side_effect and operation_mode(self.operation) == "READ":
            raise ValueError("a read operation cannot report a side effect")
        if self.verified and not self.success:
            raise ValueError("a failed operation cannot be verified")
        return self


class SandboxDecision(Record):
    allowed: bool
    policy: str
    reason: str


class ExecutionSandbox(Protocol):
    """Execution boundary. Denies first; Cascade's approvals remain the product layer."""

    def authorize(self, action: ToolAction) -> SandboxDecision: ...


class ExecutionProvider(Protocol):
    """Mutating adapter contract: check before writing, verify after writing."""

    kind: str

    def check(self, action: ToolAction) -> ToolResult: ...

    def apply(self, action: ToolAction) -> ToolResult: ...

    def verify(self, action: ToolAction) -> ToolResult: ...


class ExecutionContext(Record):
    plan_id: str
    world_version: int
    actor: str
    policy: PermissionPolicy = Field(default_factory=PermissionPolicy)
    approved_action_ids: tuple[str, ...] = ()


class ToolCall(Record):
    id: str
    action: ToolAction
    decision: CallDecision
    reason: str
    result: ToolResult | None = None
    at: AwareDatetime

    @model_validator(mode="after")
    def blocked_calls_have_no_result(self) -> Self:
        if self.decision not in ("AUTO", "APPROVED") and self.result is not None:
            raise ValueError("a call that was not authorized cannot carry a provider result")
        return self


def _blocked(action: ToolAction, decision: CallDecision, reason: str) -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex}",
        action=action,
        decision=decision,
        reason=reason,
        at=datetime.now(UTC),
    )


def _matches_postcondition(expected: PostCondition, observed: ToolResult) -> bool:
    if expected.commitment_present:
        return (
            observed.observed_start_at == expected.start_at
            and observed.observed_end_at == expected.end_at
        )
    return (
        observed.observed_start_at is None
        and observed.observed_end_at is None
        and observed.refund_amount >= expected.min_refund
    )


def reconcile_raised_write(
    action: ToolAction,
    apply_error: Exception,
    *,
    observed: ToolResult | None = None,
    verify_error: Exception | None = None,
) -> ToolResult:
    """Classify a write that raised after reading the provider's record back."""
    apply_detail = f"Provider apply raised {type(apply_error).__name__}."
    if verify_error is not None:
        return ToolResult(
            success=False,
            provider=action.provider,
            operation=action.operation,
            side_effect=True,
            raw_result_ref=f"error:{action.id}:verify",
            detail=(
                f"{apply_detail} Read-back also failed with {type(verify_error).__name__}; "
                "the external state is unknown and must be reconciled with the provider."
            ),
        )
    if observed is None:
        raise ValueError("a read-back result or error is required")
    if not observed.success:
        return observed.model_copy(
            update={
                "success": False,
                "side_effect": False,
                "verified": False,
                "detail": f"{apply_detail} Read-back: {observed.detail} The write did not land.",
            }
        )
    if _matches_postcondition(action.postcondition, observed):
        return observed.model_copy(
            update={
                "success": True,
                "side_effect": True,
                "verified": True,
                "detail": (
                    f"{apply_detail} Read-back confirmed the postcondition: {observed.detail}"
                ),
            }
        )
    return observed.model_copy(
        update={
            "success": False,
            "side_effect": True,
            "verified": False,
            "detail": (
                f"{apply_detail} Read-back found a record, but the postcondition did not match: "
                f"{observed.detail} The external state is uncertain and must be reconciled "
                "with the provider."
            ),
        }
    )


class ToolGateway:
    """The only path to an external effect. Planner and executor never call vendors."""

    def __init__(
        self,
        providers: dict[str, ExecutionProvider],
        sandbox: ExecutionSandbox | None = None,
    ):
        self.providers = providers
        self.sandbox = sandbox

    def authorize(self, action: ToolAction, context: ExecutionContext) -> ToolCall | None:
        """Return the blocking call, or None when the action may run. No side effects."""
        decision, reason = context.policy.decide(action.operation, action.risk_tier, action.amount)
        if decision == "DENIED":
            return _blocked(action, "DENIED_BY_POLICY", reason)
        # Never ask a user to approve an action the boundary or the adapters cannot run.
        if action.provider not in self.providers:
            return _blocked(action, "NO_ADAPTER", f"No adapter registered for {action.provider}.")
        if self.sandbox is not None:
            verdict = self.sandbox.authorize(action)
            if not verdict.allowed:
                return _blocked(action, "DENIED_BY_SANDBOX", f"{verdict.policy}: {verdict.reason}")
        if decision == "REQUIRES_APPROVAL" and action.id not in context.approved_action_ids:
            return _blocked(action, "REQUIRES_APPROVAL", reason)
        return None

    def _authorized_call(
        self, action: ToolAction, context: ExecutionContext, result: ToolResult
    ) -> ToolCall:
        approved = action.id in context.approved_action_ids
        decision, reason = context.policy.decide(action.operation, action.risk_tier, action.amount)
        return ToolCall(
            id=f"call_{uuid4().hex}",
            action=action,
            decision="APPROVED" if approved and decision != "AUTO" else "AUTO",
            reason=reason,
            result=result,
            at=datetime.now(UTC),
        )

    def query(self, action: ToolAction, context: ExecutionContext) -> ToolCall:
        if action.mode != "READ":
            raise ValueError("query accepts read operations only")
        blocked = self.authorize(action, context)
        if blocked is not None:
            return blocked
        provider = self.providers[action.provider]
        try:
            result = provider.check(action)
        except Exception as exc:
            # An adapter failure is unknown state, never evidence that nothing exists.
            result = ToolResult(
                success=False,
                provider=action.provider,
                operation=action.operation,
                raw_result_ref=f"error:{action.id}",
                detail=f"Adapter raised {type(exc).__name__}; availability is unknown.",
            )
        return self._authorized_call(action, context, result)

    def execute(self, action: ToolAction, context: ExecutionContext) -> ToolCall:
        """Write, then read back. Nothing is complete until its postcondition holds."""
        if action.mode != "WRITE":
            raise ValueError("execute accepts write operations only")
        blocked = self.authorize(action, context)
        if blocked is not None:
            return blocked
        provider = self.providers[action.provider]
        try:
            applied = provider.apply(action)
        except Exception as exc:
            try:
                observed = provider.verify(action)
            except Exception as verify_exc:
                result = reconcile_raised_write(action, exc, verify_error=verify_exc)
            else:
                result = reconcile_raised_write(action, exc, observed=observed)
            return self._authorized_call(action, context, result)
        if not applied.success:
            return self._authorized_call(action, context, applied)
        try:
            observed = provider.verify(action)
        except Exception as exc:
            observed = ToolResult(
                success=False,
                provider=action.provider,
                operation=action.operation,
                raw_result_ref=f"error:{action.id}:verify",
                detail=f"Verification raised {type(exc).__name__}.",
            )
        verified = observed.success and self._matches(action.postcondition, observed)
        result = applied.model_copy(
            update={
                "success": verified,
                "verified": verified,
                "observed_start_at": observed.observed_start_at,
                "observed_end_at": observed.observed_end_at,
                "refund_amount": observed.refund_amount,
                "detail": applied.detail
                if verified
                else f"{applied.detail} Postcondition unverified: {observed.detail}",
            }
        )
        return self._authorized_call(action, context, result)

    @staticmethod
    def _matches(expected: PostCondition, observed: ToolResult) -> bool:
        return _matches_postcondition(expected, observed)
