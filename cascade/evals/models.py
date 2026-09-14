from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from cascade.domain.models import IncidentSeverity, Record
from cascade.planning.models import SearchPolicy
from cascade.tools.permissions import PermissionPolicy

PlanningStatus = Literal[
    "COMPLETE",
    "PARTIAL",
    "BLOCKED",
    "NO_FEASIBLE_PLAN",
    "BUDGET_EXHAUSTED",
]
ExecutionMode = Literal["NONE", "WITHOUT_APPROVAL", "APPROVED", "APPROVED_TWICE"]


class Faults(Record):
    """Explicit, reproducible failure injection. No randomness anywhere in evals."""

    # Planner-side: the read adapter cannot answer at all.
    query_outage: tuple[str, ...] = ()
    # Planner-side: the adapter answers, and the answer is that nothing is available.
    empty_inventory: tuple[str, ...] = ()
    # Execution-side: inventory disappears between the quote and the write.
    withdraw: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Execution-side: the provider refuses the write.
    reject_write: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class Expectation(Record):
    ingest_rejected: bool = False
    incident: bool = True
    hard_violations: int | None = None
    soft_violations: int | None = None
    affected: tuple[str, ...] | None = None
    violation_ids: tuple[str, ...] | None = None
    planning_status: PlanningStatus | None = None
    skill: str | None = None
    severity: IncidentSeverity | None = None
    severity_withheld: bool = False
    candidates: int | None = None
    min_candidates: int | None = None
    exhaustion_reported: bool = False
    execution_status: str | None = None
    side_effects: int | None = None
    sandbox_denials: int | None = None
    incident_status: Literal["OPEN", "RESOLVED", "DISMISSED"] | None = None
    final_hard_violations: int | None = None
    replan_feasible: bool | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.severity is not None and self.severity_withheld:
            raise ValueError("severity and severity_withheld cannot both be set")
        return self


class Scenario(Record):
    id: str
    title: str
    description: str
    tags: tuple[str, ...] = ()
    # The demo itinerary is the shared baseline; scenarios vary the disruption.
    arrival: str = "19:05"
    drop_dependencies: tuple[str, ...] = ()
    confidence: float = Field(default=1, ge=0, le=1)
    expected_version: int = 0
    plan: bool = True
    policy: SearchPolicy | None = None
    skill: str | None = None
    permissions: PermissionPolicy | None = None
    sandbox_providers: tuple[str, ...] | None = None
    sandbox_max_amount: Decimal | None = None
    faults: Faults = Field(default_factory=Faults)
    execute: ExecutionMode = "NONE"
    expect: Expectation

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.expect.ingest_rejected and (self.plan or self.execute != "NONE"):
            raise ValueError("a rejected event cannot be planned or executed")
        if self.execute != "NONE" and not self.plan:
            raise ValueError("execution requires a planning stage")
        return self


class Check(Record):
    name: str
    passed: bool
    expected: str
    actual: str


class ScenarioOutcome(Record):
    scenario_id: str
    title: str
    tags: tuple[str, ...]
    passed: bool
    checks: tuple[Check, ...]
    error: str | None = None
    detected_violation_ids: tuple[str, ...] = ()
    expected_violation_ids: tuple[str, ...] = ()
    surfaced_candidates: int = 0
    feasible_candidates: int = 0
    blocked_mutations: int = 0
    attempted_mutations: int = 0


class Metrics(Record):
    scenarios: int
    passed: int
    conflict_precision: float | None
    conflict_recall: float | None
    recovery_feasibility: float | None
    unauthorized_mutations_blocked: float | None
    graceful_degradation: float | None


class EvalReport(Record):
    outcomes: tuple[ScenarioOutcome, ...]
    metrics: Metrics

    @property
    def ok(self) -> bool:
        return all(o.passed for o in self.outcomes)
