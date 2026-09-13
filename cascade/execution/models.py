from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime

from cascade.domain.models import Assessment, Record


class Approval(Record):
    id: str
    incident_id: str
    plan_id: str
    plan_digest: str
    world_version: int
    additional_cost: Decimal
    status: Literal["APPROVED", "REJECTED", "CONSUMED"]
    created_at: AwareDatetime
    actor: Literal["local_user"] = "local_user"


class ActionOutcome(Record):
    action_id: str
    commitment_id: str
    resolution: str
    status: Literal["VERIFIED", "FAILED"]
    explanation: str
    occurred_at: AwareDatetime
    world_version: int
    side_effect: bool


class Execution(Record):
    id: str
    plan_id: str
    approval_id: str
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"]
    next_step: int
    total_steps: int
    expected_world_version: int
    outcomes: tuple[ActionOutcome, ...]
    assessment: Assessment
    message: str
    mode: Literal["mock"] = "mock"
