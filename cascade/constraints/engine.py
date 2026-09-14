from datetime import timedelta

import networkx as nx

from cascade.domain.models import Assessment, Violation, World
from cascade.graph.traversal import build_graph


def evaluate(world: World) -> Assessment:
    """Project earliest possible times; never mutate or claim to rebook commitments.

    Hard precedence edges propagate delays through the DAG. Soft edges are checked
    but do not force shifts. Fixed reservations retain their scheduled time, so
    downstream projections indicate threats, not provider availability.
    """
    graph = build_graph(world)
    commitments = {c.id: c for c in world.commitments}
    starts = {c.id: c.start_at for c in world.commitments}
    violations = []
    incoming = {cid: [] for cid in commitments}
    for edge in world.dependencies:
        incoming[edge.to_id].append(edge)
    for cid in nx.topological_sort(graph):
        target = commitments[cid]
        for edge in incoming[cid]:
            parent = commitments[edge.from_id]
            earliest = (
                starts[parent.id]
                + (parent.end_at - parent.start_at)
                + timedelta(minutes=edge.lag_minutes)
            )
            if earliest > target.start_at:
                violations.append(
                    Violation(
                        constraint_id=edge.id,
                        affected_commitment_ids=(parent.id, cid),
                        severity="hard" if edge.hard else "soft",
                        actual_at=earliest,
                        required_at=target.start_at,
                        delay_minutes=(earliest - target.start_at).total_seconds() / 60,
                        explanation=edge.explanation,
                    )
                )
            if edge.hard:
                starts[cid] = max(starts[cid], earliest)
    for deadline in world.deadlines:
        actual = starts[deadline.commitment_id]
        if actual > deadline.latest_start:
            violations.append(
                Violation(
                    constraint_id=deadline.id,
                    affected_commitment_ids=(deadline.commitment_id,),
                    severity="hard" if deadline.hard else "soft",
                    actual_at=actual,
                    required_at=deadline.latest_start,
                    delay_minutes=(actual - deadline.latest_start).total_seconds() / 60,
                    explanation=deadline.explanation,
                )
            )
    # Hard-edge projection separates ordered pairs; unlinked or soft-ordered pairs can overlap.
    for index, first in enumerate(world.commitments):
        for second in world.commitments[index + 1 :]:
            first_start = starts[first.id]
            second_start = starts[second.id]
            first_end = first_start + (first.end_at - first.start_at)
            second_end = second_start + (second.end_at - second.start_at)
            if not (first_start < second_end and second_start < first_end):
                continue
            earlier, later = sorted(
                (first, second), key=lambda commitment: (starts[commitment.id], commitment.id)
            )
            earlier_start = starts[earlier.id]
            later_start = starts[later.id]
            earlier_end = earlier_start + (earlier.end_at - earlier.start_at)
            lo, hi = sorted((first.id, second.id))
            violations.append(
                Violation(
                    constraint_id=f"overlap:{lo}:{hi}",
                    affected_commitment_ids=(earlier.id, later.id),
                    severity="hard",
                    actual_at=later_start,
                    required_at=earlier_end,
                    delay_minutes=(earlier_end - later_start).total_seconds() / 60,
                    explanation=(
                        f"{earlier.title} and {later.title} overlap; both cannot be attended."
                    ),
                )
            )
    return Assessment(projected_starts=starts, violations=tuple(violations))
