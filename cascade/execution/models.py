from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import Record, Violation, World
from cascade.planning.models import Resolution
from cascade.security.approvals import ApprovalRequest
from cascade.tools.gateway import ToolCall

StepStatus = Literal[
    "SKIPPED",
    "EXECUTED",
    "PRECHECK_FAILED",
    "FAILED",
    "REQUIRES_APPROVAL",
    "DENIED",
    "NOT_ATTEMPTED",
]

ExecutionStatus = Literal[
    "COMPLETED",
    "PARTIAL",
    "BLOCKED",
    "DENIED",
    "AWAITING_APPROVAL",
    "STALE",
]


class ExecutionStep(Record):
    id: str
    commitment_id: str
    resolution: Resolution
    status: StepStatus
    precheck: ToolCall | None = None
    call: ToolCall | None = None
    world_version_after: int | None = None
    violations_after: tuple[Violation, ...] = ()
    note: str

    @model_validator(mode="after")
    def executed_steps_carry_evidence(self) -> Self:
        if self.status == "EXECUTED" and (self.call is None or self.world_version_after is None):
            raise ValueError("an executed step must record its verified call and new version")
        return self


class ExecutionResult(Record):
    id: str
    plan_id: str
    incident_id: str
    based_on_version: int = Field(ge=0)
    status: ExecutionStatus
    steps: tuple[ExecutionStep, ...]
    approval_request: ApprovalRequest | None = None
    world: World
    world_version_after: int = Field(ge=0)
    remaining_violations: tuple[Violation, ...] = ()
    side_effects: int = Field(default=0, ge=0)
    replan_required: bool = False
    verified: bool = False
    started_at: AwareDatetime
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def completion_is_earned(self) -> Self:
        if self.status == "COMPLETED" and (
            self.replan_required
            or not self.verified
            or any(v.severity == "hard" for v in self.remaining_violations)
        ):
            raise ValueError("a completed execution is verified and leaves no hard violation")
        if self.status in ("AWAITING_APPROVAL", "DENIED", "STALE") and self.side_effects:
            raise ValueError("an unauthorized execution cannot have produced a side effect")
        if self.status == "AWAITING_APPROVAL" and self.approval_request is None:
            raise ValueError("an execution awaiting approval must carry its request")
        return self
