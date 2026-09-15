from collections import Counter
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime

from cascade.domain.models import Record, World
from cascade.execution.models import ExecutionResult
from cascade.memory.preferences import Preference, preference
from cascade.planning.models import PlanningResult

# A suggestion needs repeated agreement before it is worth showing at all.
MIN_EVIDENCE = 3
MIN_AGREEMENT = 0.7


class IntentOutcome(Record):
    intent_id: str
    kept: bool


class ResolutionRecord(Record):
    """What actually happened to one incident, kept so ranking can improve later."""

    id: str
    incident_id: str
    outcome: Literal["EXECUTED", "DISMISSED"]
    trigger_commitment_id: str
    affected_commitment_ids: tuple[str, ...]
    severity: str | None
    skill: str | None
    selected_plan_id: str | None
    rejected_plan_ids: tuple[str, ...] = ()
    chosen_intents: tuple[IntentOutcome, ...] = ()
    # Each rejected candidate's own intent profile, so a choice can be compared to
    # what it was chosen over rather than to nothing.
    rejected_intents: tuple[tuple[IntentOutcome, ...], ...] = ()
    executed_actions: tuple[str, ...] = ()
    additional_cost: str = "0"
    recorded_at: AwareDatetime


def profile(quality: dict[str, float]) -> tuple[IntentOutcome, ...]:
    return tuple(
        IntentOutcome(intent_id=intent, kept=value >= 1)
        for intent, value in sorted(quality.items())
    )


def record_execution(
    planning: PlanningResult, execution: ExecutionResult, severity: str | None
) -> ResolutionRecord:
    chosen = next((p for p in planning.candidates if p.id == execution.plan_id), None)
    rejected = [p for p in planning.candidates if p.id != execution.plan_id]
    return ResolutionRecord(
        id=f"res_{uuid4().hex}",
        incident_id=execution.incident_id,
        outcome="EXECUTED",
        trigger_commitment_id="",
        affected_commitment_ids=tuple(step.commitment_id for step in execution.steps),
        severity=severity,
        skill=planning.skill.name if planning.skill else None,
        selected_plan_id=execution.plan_id,
        rejected_plan_ids=tuple(p.id for p in rejected),
        chosen_intents=profile(chosen.intent_quality) if chosen else (),
        rejected_intents=tuple(profile(p.intent_quality) for p in rejected),
        executed_actions=tuple(
            step.call.result.external_reference
            for step in execution.steps
            if step.call and step.call.result and step.call.result.external_reference
        ),
        additional_cost=str(chosen.additional_cost) if chosen else "0",
        recorded_at=datetime.now(UTC),
    )


def record_simulation_execution(
    planning: PlanningResult, execution, severity: str | None, incident_id: str = ""
) -> ResolutionRecord:
    """Record a completed React simulation using its stepwise execution model."""
    chosen = next((p for p in planning.candidates if p.id == execution.plan_id), None)
    return ResolutionRecord(
        id=f"res_{uuid4().hex}",
        incident_id=incident_id,
        outcome="EXECUTED",
        trigger_commitment_id=chosen.actions[0].commitment_id if chosen and chosen.actions else "",
        affected_commitment_ids=(
            tuple(action.commitment_id for action in chosen.actions) if chosen else ()
        ),
        severity=severity,
        skill=planning.skill.name if planning.skill else None,
        selected_plan_id=execution.plan_id,
        rejected_plan_ids=tuple(p.id for p in planning.candidates if p.id != execution.plan_id),
        chosen_intents=profile(chosen.intent_quality) if chosen else (),
        rejected_intents=tuple(
            profile(p.intent_quality) for p in planning.candidates if p.id != execution.plan_id
        ),
        executed_actions=tuple(
            outcome.action_id for outcome in execution.outcomes if outcome.status == "VERIFIED"
        ),
        additional_cost=str(chosen.additional_cost) if chosen else "0",
        recorded_at=datetime.now(UTC),
    )


def record_dismissal(incident_id: str, severity: str | None) -> ResolutionRecord:
    return ResolutionRecord(
        id=f"res_{uuid4().hex}",
        incident_id=incident_id,
        outcome="DISMISSED",
        trigger_commitment_id="",
        affected_commitment_ids=(),
        severity=severity,
        skill=None,
        selected_plan_id=None,
        recorded_at=datetime.now(UTC),
    )


def _revealed(record: ResolutionRecord) -> set[tuple[str, str]]:
    """Pairs (kept, given up) where a rejected option would have reversed the choice.

    A preference is only revealed when the alternative was actually available: keeping
    an intent nothing threatened says nothing about what the user values.
    """
    kept = {o.intent_id for o in record.chosen_intents if o.kept}
    lost = {o.intent_id for o in record.chosen_intents if not o.kept}
    pairs = set()
    for other in record.rejected_intents:
        other_kept = {o.intent_id for o in other if o.kept}
        for winner in kept - other_kept:
            for loser in lost & other_kept:
                pairs.add((winner, loser))
    return pairs


def suggest(records: tuple[ResolutionRecord, ...], world: World) -> tuple[Preference, ...]:
    """Derive suggestions from repeated choices. These never become policy on their own.

    Confidence is recomputed from the whole record every time rather than decayed on a
    timer, so a reversed choice lowers it immediately instead of ageing out slowly.
    """
    support: Counter[tuple[str, str]] = Counter()
    evidence: dict[tuple[str, str], list[str]] = {}
    for record in records:
        if record.outcome != "EXECUTED":
            continue
        for pair in _revealed(record):
            support[pair] += 1
            evidence.setdefault(pair, []).append(record.id)
    descriptions = {i.id: i.description for i in world.intents}
    suggestions = []
    for (winner, loser), count in sorted(support.items()):
        against = support[(loser, winner)]
        total = count + against
        confidence = count / total
        if total < MIN_EVIDENCE or confidence < MIN_AGREEMENT:
            continue
        suggestions.append(
            preference(
                statement=(
                    f"When these conflict, you have protected "
                    f"“{descriptions.get(winner, winner)}” over "
                    f"“{descriptions.get(loser, loser)}”."
                ),
                directive="require_intent",
                value=winner,
                source="learned",
                confidence=round(confidence, 2),
                evidence=tuple(evidence[(winner, loser)]),
            )
        )
    return tuple(suggestions)
