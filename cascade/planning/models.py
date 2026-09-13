from decimal import Decimal
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import Assessment, Commitment, Deadline, Record, World

Resolution = Literal["PRESERVED", "RESCHEDULED", "SUBSTITUTED", "COMPENSATED", "ABANDONED"]


class RecoveryOption(Record):
    id: str
    commitment_id: str
    resolution: Resolution
    replacement: Commitment | None = None
    # Only explicitly owned provider rules may be replaced. User rules survive.
    replaced_deadline_ids: tuple[str, ...] = ()
    replacement_deadlines: tuple[Deadline, ...] = ()
    additional_cost: Decimal = Field(default=Decimal("0"), ge=0)
    loss: Decimal = Field(default=Decimal("0"), ge=0)
    refund: Decimal = Field(default=Decimal("0"), ge=0)
    currency: Literal["EUR"] = "EUR"
    intent_quality: float = Field(default=1, ge=0, le=1)
    explanation: str
    evidence: str

    @model_validator(mode="after")
    def valid_resolution(self) -> Self:
        dropped = self.resolution in ("COMPENSATED", "ABANDONED")
        if dropped != (self.replacement is None):
            raise ValueError(
                "loss resolutions require removal; other resolutions require replacement"
            )
        if dropped and self.intent_quality != 0:
            raise ValueError("a lost intent must have zero quality")
        if self.replacement and self.replacement.id != self.commitment_id:
            raise ValueError("replacement must retain the commitment identity")
        if self.resolution == "COMPENSATED" and self.refund <= 0:
            raise ValueError("compensation requires a provider-backed refund")
        if self.resolution == "ABANDONED" and self.refund:
            raise ValueError("a refund must be described as compensation")
        return self


class ProviderResult(Record):
    provider: str
    commitment_id: str
    status: Literal["AVAILABLE", "UNAVAILABLE", "ERROR"]
    options: tuple[RecoveryOption, ...] = ()
    exhausted: dict[str, str] = Field(default_factory=dict)
    evidence: str
    checked_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == "AVAILABLE") != bool(self.options):
            raise ValueError("only AVAILABLE results may contain options")
        if self.status == "ERROR" and self.exhausted:
            raise ValueError("provider failures cannot establish exhaustion")
        if any(o.commitment_id != self.commitment_id for o in self.options):
            raise ValueError("option belongs to another commitment")
        if len({o.id for o in self.options}) != len(self.options):
            raise ValueError("duplicate provider option IDs")
        if self.expires_at <= self.checked_at:
            raise ValueError("invalid quote validity window")
        return self


class SearchPolicy(Record):
    max_candidates: int = Field(default=64, ge=1, le=1024)
    max_options_per_commitment: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=25, ge=0, le=100)
    max_expansions: int = Field(default=1000, ge=1, le=10000)
    max_depth: int = Field(default=12, ge=1, le=100)
    max_seconds: float = Field(default=5, gt=0, le=30)
    max_additional_cost: Decimal = Field(default=Decimal("500"), ge=0)
    # These are explicit demo defaults, exposed to callers rather than inferred.
    required_intent_ids: tuple[str, ...] = ("intent_transfer", "intent_hotel")


class CandidatePlan(Record):
    id: str
    based_on_version: int
    world: World
    actions: tuple[RecoveryOption, ...]
    intent_quality: dict[str, float]
    additional_cost: Decimal
    loss: Decimal
    refund: Decimal
    currency: Literal["EUR"] = "EUR"
    soft_delay_minutes: float
    assessment: Assessment
    explanation: str
    status: Literal["AWAITING_APPROVAL"] = "AWAITING_APPROVAL"


class PlanningResult(Record):
    id: str
    incident_id: str
    based_on_version: int
    status: Literal["COMPLETE", "PARTIAL", "BLOCKED", "NO_FEASIBLE_PLAN", "BUDGET_EXHAUSTED"]
    candidates: tuple[CandidatePlan, ...]
    provider_results: tuple[ProviderResult, ...]
    rejections: dict[str, int]
    tool_calls: int
    expansions: int
    policy: SearchPolicy
    notes: tuple[str, ...]
