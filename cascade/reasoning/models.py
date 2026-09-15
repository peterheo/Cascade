from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import EventResult, Mutation, Record
from cascade.planning.models import PlanningResult, Resolution

ReasoningTask = Literal["extract", "strategy", "compare"]


class ContextManifest(Record):
    """Auditable metadata describing the exact minimized context sent to a model."""

    task: ReasoningTask
    model: str
    fields: tuple[str, ...]
    entity_ids: tuple[str, ...]
    bytes_sent: int = Field(ge=0)
    input_hash: str
    minimized: bool


class ExtractedChange(Record):
    outcome: Literal["UPDATE", "NO_CHANGE", "NEEDS_CLARIFICATION", "UNSUPPORTED"]
    commitment_id: str | None
    new_start_at: AwareDatetime | None
    new_end_at: AwareDatetime | None
    confidence: float = Field(ge=0, le=1)
    evidence_quote: str = Field(max_length=2000)
    explanation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_change(self) -> Self:
        if self.outcome == "UPDATE":
            if not self.commitment_id or self.new_start_at is None or self.new_end_at is None:
                raise ValueError("UPDATE requires identity and aware start/end times")
            if self.new_end_at < self.new_start_at:
                raise ValueError("invalid extracted interval")
        elif any(
            v is not None
            for v in (
                self.commitment_id,
                self.new_start_at,
                self.new_end_at,
            )
        ):
            raise ValueError("non-update outcomes cannot carry a mutation")
        return self


class OperatorSuggestion(Record):
    commitment_id: str
    priorities: tuple[Resolution, ...] = Field(min_length=1, max_length=5)
    rationale: str = Field(min_length=1, max_length=1000)


class ProposedStrategy(Record):
    suggestions: tuple[OperatorSuggestion, ...] = Field(max_length=12)
    assumptions: tuple[str, ...] = Field(max_length=12)
    decisions_required: tuple[str, ...] = Field(max_length=12)


class PlanExplanation(Record):
    plan_id: str
    explanation: str = Field(min_length=1, max_length=2000)
    tradeoff: str = Field(min_length=1, max_length=2000)


class PlanComparison(Record):
    plans: tuple[PlanExplanation, ...] = Field(max_length=64)
    recommended_plan_id: str | None
    rationale: str = Field(min_length=1, max_length=2000)


class ModelCall(Record):
    id: str
    task: ReasoningTask
    provider: str
    model: str
    attempts: int
    elapsed_ms: int
    input_hash: str
    prompt_hash: str
    output_hash: str
    # Token counts are optional because not every compatible response supplies them.
    input_tokens: int | None
    output_tokens: int | None
    manifest: ContextManifest


class PrivacySettings(Record):
    """Process-local controls for live inference and event-text retention."""

    live_inference: bool = True
    persist_event_text: bool = False


class NaturalEventRequest(Record):
    event_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=12000)
    apply: bool = False


class ExtractionResult(Record):
    id: str
    status: Literal["PROPOSED", "NEEDS_CONFIRMATION", "APPLIED", "NO_CHANGE", "UNSUPPORTED"]
    based_on_version: int
    extraction: ExtractedChange
    mutation: Mutation | None
    event_result: EventResult | None
    model_call: ModelCall


class AssistedPlanningResult(Record):
    planning: PlanningResult
    strategy: ProposedStrategy | None
    comparison: PlanComparison | None
    model_calls: tuple[ModelCall, ...]
    warnings: tuple[str, ...]
