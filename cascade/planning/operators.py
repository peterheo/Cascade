from cascade.domain.models import Dependency, World
from cascade.planning.models import RecoveryOption


def apply_option(world: World, option: RecoveryOption) -> World:
    """Materialize a hypothetical world, preserving unrelated rules and travel bounds."""
    current = next(c for c in world.commitments if c.id == option.commitment_id)
    if option.replacement and (
        option.replacement.intent_id != current.intent_id or option.replacement.kind != current.kind
    ):
        raise ValueError("replacement must retain intent and kind")
    owned = {
        d.id
        for d in world.deadlines
        if d.commitment_id == current.id and d.source.source in ("demo_fixture", "mock_provider")
    }
    if not set(option.replaced_deadline_ids) <= owned:
        raise ValueError("option cannot remove an unowned deadline")
    if any(d.commitment_id != current.id for d in option.replacement_deadlines):
        raise ValueError("replacement deadline targets another commitment")
    deadlines = tuple(d for d in world.deadlines if d.id not in option.replaced_deadline_ids)
    deadlines += option.replacement_deadlines
    edges = world.dependencies
    if option.replacement:
        commitments = tuple(
            option.replacement if c.id == current.id else c for c in world.commitments
        )
    else:
        # REQUIRES is not bypassable: abandoning its prerequisite cannot satisfy it.
        if any(d.from_id == current.id and d.relation == "REQUIRES" and d.hard for d in edges):
            raise ValueError("cannot drop an active hard prerequisite")
        if any(d.commitment_id == current.id and d.hard and d.id not in owned for d in deadlines):
            raise ValueError("cannot abandon a commitment with a hard user deadline")
        incoming = [d for d in edges if d.to_id == current.id]
        outgoing = [d for d in edges if d.from_id == current.id]
        bypass = tuple(
            Dependency(
                id=f"bypass:{left.id}:{right.id}",
                from_id=left.from_id,
                to_id=right.to_id,
                relation="TRAVEL_TO",
                lag_minutes=left.lag_minutes + right.lag_minutes,
                hard=left.hard and right.hard,
                confidence=min(left.confidence, right.confidence),
                explanation=f"Retain travel via skipped {current.title}; omit activity duration.",
            )
            for left in incoming
            for right in outgoing
        )
        edges = tuple(d for d in edges if current.id not in (d.from_id, d.to_id)) + bypass
        commitments = tuple(c for c in world.commitments if c.id != current.id)
        deadlines = tuple(d for d in deadlines if d.commitment_id != current.id)
    return World.model_validate(
        {
            **world.model_dump(),
            "commitments": commitments,
            "dependencies": edges,
            "deadlines": deadlines,
        }
    )
