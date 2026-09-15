"""Small, task-specific contexts for live reasoning calls.

The builders deliberately construct new dictionaries from allowlisted fields.  They
never pass domain model dumps through, because those dumps contain provenance and
other data that a particular reasoning task does not need.
"""

from __future__ import annotations

from collections.abc import Iterable

from cascade.domain.models import Incident, World
from cascade.planning.models import SELECTABLE_RESOLUTIONS, PlanningResult, SearchPolicy

ALLOWED_RESOLUTIONS = ("PRESERVED", *SELECTABLE_RESOLUTIONS)


class ReasoningContext(dict):
    """JSON context with non-serialized manifest metadata attached."""

    fields: tuple[str, ...]
    entity_ids: tuple[str, ...]
    minimized: bool

    def __init__(
        self,
        payload: dict,
        *,
        fields: Iterable[str],
        entity_ids: Iterable[str] = (),
        minimized: bool = True,
    ):
        super().__init__(payload)
        self.fields = tuple(fields)
        self.entity_ids = tuple(dict.fromkeys(entity_ids))
        self.minimized = minimized


def _commitment(commitment) -> dict:
    return {
        "id": commitment.id,
        "kind": commitment.kind,
        "title": commitment.title,
        "start_at": commitment.start_at.isoformat(),
        "end_at": commitment.end_at.isoformat(),
    }


def extract_context(event_text: str, world: World) -> ReasoningContext:
    commitments = [_commitment(item) for item in world.commitments]
    return ReasoningContext(
        {
            "event_text": event_text,
            "world": {"version": world.version, "commitments": commitments},
        },
        fields=(
            "event_text",
            "world.version",
            "commitments[].id",
            "commitments[].kind",
            "commitments[].title",
            "commitments[].start_at",
            "commitments[].end_at",
        ),
        entity_ids=(item["id"] for item in commitments),
    )


def strategy_context(
    incident: Incident,
    world: World,
    policy: SearchPolicy,
    affected_ids: Iterable[str],
) -> ReasoningContext:
    affected = tuple(affected_ids)
    affected_set = set(affected)
    commitments = [_commitment(item) for item in world.commitments if item.id in affected_set]
    intent_ids = tuple(
        dict.fromkeys(item.intent_id for item in world.commitments if item.id in affected_set)
    )
    intents = [
        {"id": item.id, "description": item.description, "importance": item.importance}
        for item in world.intents
        if item.id in intent_ids
    ]
    violations = [item.model_dump(mode="json") for item in incident.violations]
    return ReasoningContext(
        {
            "affected_ids": list(affected),
            "commitments": commitments,
            "intents": intents,
            "violations": violations,
            "policy": policy.model_dump(mode="json"),
            "allowed_resolutions": list(ALLOWED_RESOLUTIONS),
        },
        fields=(
            "affected_ids",
            "commitments[].id",
            "commitments[].kind",
            "commitments[].title",
            "commitments[].start_at",
            "commitments[].end_at",
            "intents[].id",
            "intents[].description",
            "intents[].importance",
            "violations[]",
            "policy",
            "allowed_resolutions",
        ),
        entity_ids=(*affected, *intent_ids),
    )


def compare_context(planning: PlanningResult, policy: SearchPolicy) -> ReasoningContext:
    candidates = []
    entity_ids = []
    for candidate in planning.candidates:
        entity_ids.append(candidate.id)
        actions = []
        for action in candidate.actions:
            actions.append(
                {
                    "commitment_id": action.commitment_id,
                    "resolution": action.resolution,
                    "option_id": action.id,
                }
            )
            entity_ids.append(action.commitment_id)
        candidates.append(
            {
                "id": candidate.id,
                "actions": actions,
                "additional_cost": str(candidate.additional_cost),
                "loss": str(candidate.loss),
                "refund": str(candidate.refund),
                "intent_quality": candidate.intent_quality,
            }
        )
    return ReasoningContext(
        {
            "candidates": candidates,
            "policy": policy.model_dump(mode="json"),
            "notes": list(planning.notes),
        },
        fields=(
            "candidates[].id",
            "candidates[].actions[].commitment_id",
            "candidates[].actions[].resolution",
            "candidates[].actions[].option_id",
            "candidates[].additional_cost",
            "candidates[].loss",
            "candidates[].refund",
            "candidates[].intent_quality",
            "policy",
            "notes",
        ),
        entity_ids=entity_ids,
    )


# Descriptive aliases keep the builders discoverable to callers that prefer a
# ``build_*`` naming convention.
build_extract_context = extract_context
build_strategy_context = strategy_context
build_compare_context = compare_context
