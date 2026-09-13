from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(Record):
    source: str
    external_id: str
    confidence: float = Field(default=1, ge=0, le=1)


class Intent(Record):
    id: str
    description: str
    importance: float = Field(ge=0, le=1)
    source: Literal["explicit_user", "derived", "learned"] = "explicit_user"


class Commitment(Record):
    id: str
    kind: Literal["flight", "transfer", "hotel", "restaurant", "ticket", "meeting"]
    title: str
    intent_id: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    source: SourceRef

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.end_at < self.start_at:
            raise ValueError("end_at must be at or after start_at")
        return self


class Dependency(Record):
    id: str
    from_id: str
    to_id: str
    relation: Literal["BEFORE", "TRAVEL_TO", "REQUIRES"] = "BEFORE"
    lag_minutes: int = Field(default=0, ge=0)
    hard: bool = True
    confidence: float = Field(default=1, ge=0, le=1)
    source: Literal["explicit", "deterministic", "model_inferred"] = "deterministic"
    explanation: str


class Deadline(Record):
    id: str
    commitment_id: str
    latest_start: AwareDatetime
    hard: bool = True
    explanation: str
    source: SourceRef = Field(
        default_factory=lambda: SourceRef(source="explicit_user", external_id="deadline")
    )


class World(Record):
    commitments: tuple[Commitment, ...]
    intents: tuple[Intent, ...]
    dependencies: tuple[Dependency, ...]
    deadlines: tuple[Deadline, ...] = ()
    version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_references(self) -> Self:
        for name in ("commitments", "intents", "dependencies", "deadlines"):
            values = getattr(self, name)
            if len({v.id for v in values}) != len(values):
                raise ValueError(f"duplicate IDs in {name}")
        ids = {c.id for c in self.commitments}
        intents = {i.id for i in self.intents}
        if any(c.intent_id not in intents for c in self.commitments):
            raise ValueError("unknown intent reference")
        if any(d.from_id not in ids or d.to_id not in ids for d in self.dependencies):
            raise ValueError("unknown dependency endpoint")
        if any(d.commitment_id not in ids for d in self.deadlines):
            raise ValueError("unknown deadline commitment")
        constraint_ids = [d.id for d in (*self.dependencies, *self.deadlines)]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("constraint IDs must be globally unique")
        return self


class Mutation(Record):
    event_id: str = Field(min_length=1)
    commitment_id: str
    expected_version: int = Field(ge=0)
    new_start_at: AwareDatetime
    new_end_at: AwareDatetime
    source: SourceRef


class Violation(Record):
    constraint_id: str
    affected_commitment_ids: tuple[str, ...]
    severity: Literal["hard", "soft"]
    actual_at: datetime
    required_at: datetime
    delay_minutes: float
    explanation: str


class Assessment(Record):
    projected_starts: dict[str, datetime]
    violations: tuple[Violation, ...]


class Incident(Record):
    id: str
    trigger_event_id: str
    trigger_commitment_id: str
    affected_commitment_ids: tuple[str, ...]
    threatened_intent_ids: tuple[str, ...]
    violations: tuple[Violation, ...]
    status: Literal["OPEN"] = "OPEN"


class EventResult(Record):
    event_id: str
    world_version: int
    incident: Incident | None
    assessment: Assessment
