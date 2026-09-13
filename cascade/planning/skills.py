from decimal import Decimal
from importlib.resources import files
from typing import Self

from pydantic import Field, model_validator

from cascade.domain.models import Incident, Record, World
from cascade.planning.models import Resolution, SearchPolicy, SkillRef

SKILL_DIRECTORY = "data/skills"


class SkillTrigger(Record):
    """Everything here is read off the incident deterministically."""

    commitment_kinds: tuple[str, ...] = Field(min_length=1)
    min_delay_minutes: float = Field(default=0, ge=0)
    min_hard_violations: int = Field(default=1, ge=0)


class SkillLimits(Record):
    """Only the limits a skill actually states; the rest keep the caller's policy."""

    max_candidates: int | None = Field(default=None, ge=1, le=1024)
    max_options_per_commitment: int | None = Field(default=None, ge=1, le=100)
    max_tool_calls: int | None = Field(default=None, ge=0, le=100)
    max_depth: int | None = Field(default=None, ge=1, le=100)
    max_additional_cost: Decimal | None = Field(default=None, ge=0)


class Skill(Record):
    """A versioned recovery template. It constrains the planner; it is not a prompt."""

    name: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    description: str
    trigger: SkillTrigger
    # Which downstream commitment kinds this template knows how to repair.
    inspect_kinds: tuple[str, ...] = Field(min_length=1)
    operators: tuple[Resolution, ...] = Field(min_length=1)
    order: tuple[Resolution, ...] = ()
    limits: SkillLimits = Field(default_factory=SkillLimits)
    # An opt-in template is never chosen for the user; restricting operators can turn a
    # recoverable day into an unrecoverable one, and that has to be a deliberate choice.
    auto_select: bool = True
    notes: str = ""

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if "PRESERVED" in self.operators or "PRESERVED" in self.order:
            raise ValueError("preservation is always available and cannot be listed")
        if len(set(self.operators)) != len(self.operators):
            raise ValueError("duplicate operator in this skill")
        if not set(self.order) <= set(self.operators):
            raise ValueError("a skill cannot order an operator it does not allow")
        if len(set(self.order)) != len(self.order):
            raise ValueError("duplicate operator order entry")
        return self

    @property
    def ref(self) -> SkillRef:
        return SkillRef(name=self.name, version=self.version)

    def matches(self, world: World, incident: Incident) -> bool:
        commitment = next(
            (c for c in world.commitments if c.id == incident.trigger_commitment_id), None
        )
        if commitment is None or commitment.kind not in self.trigger.commitment_kinds:
            return False
        hard = [v for v in incident.violations if v.severity == "hard"]
        if len(hard) < self.trigger.min_hard_violations:
            return False
        worst = max((v.delay_minutes for v in hard), default=0)
        return worst >= self.trigger.min_delay_minutes

    def policy(self, base: SearchPolicy) -> SearchPolicy:
        stated = {k: v for k, v in self.limits.model_dump().items() if v is not None}
        return base.model_copy(update={**stated, "allowed_resolutions": tuple(self.operators)})

    def priorities(self, world: World, incident: Incident) -> dict[str, tuple[Resolution, ...]]:
        if not self.order:
            return {}
        kinds = {c.id: c.kind for c in world.commitments}
        return {
            cid: self.order
            for cid in incident.affected_commitment_ids
            if kinds.get(cid) in self.inspect_kinds
        }


def load_skills() -> tuple[Skill, ...]:
    resources = files("cascade").joinpath(SKILL_DIRECTORY)
    return tuple(
        sorted(
            (
                Skill.model_validate_json(entry.read_text())
                for entry in resources.iterdir()
                if entry.name.endswith(".json")
            ),
            key=lambda s: (s.name, s.version),
        )
    )


def select(
    world: World, incident: Incident, skills: tuple[Skill, ...] | None = None
) -> Skill | None:
    """Deterministic: the most specific trigger wins, then the newest version."""
    available = skills if skills is not None else load_skills()
    candidates = [s for s in available if s.auto_select and s.matches(world, incident)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda s: (
            s.trigger.min_delay_minutes,
            s.trigger.min_hard_violations,
            s.version,
            s.name,
        ),
    )
