from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from cascade.constraints.engine import evaluate
from cascade.domain.models import Incident, World
from cascade.graph.traversal import descendants
from cascade.planning.models import (
    CandidatePlan,
    PlanningResult,
    ProviderResult,
    RecoveryOption,
    SearchPolicy,
)
from cascade.planning.operators import apply_option
from cascade.tools.adapters.fixtures import RecoveryProvider


@dataclass
class Branch:
    world: World
    actions: tuple[RecoveryOption, ...] = ()


def financials(actions: tuple[RecoveryOption, ...]) -> tuple[Decimal, Decimal, Decimal]:
    return tuple(
        sum((getattr(a, key) for a in actions), Decimal(0))
        for key in ("additional_cost", "loss", "refund")
    )


def dominates(left: CandidatePlan, right: CandidatePlan) -> bool:
    # Compare intent dimensions separately: a scalar score must not hide tradeoffs.
    a = (
        *left.intent_quality.values(),
        -left.additional_cost,
        -left.loss,
        left.refund,
        -left.soft_delay_minutes,
    )
    b = (
        *right.intent_quality.values(),
        -right.additional_cost,
        -right.loss,
        right.refund,
        -right.soft_delay_minutes,
    )
    return all(x >= y for x, y in zip(a, b, strict=True)) and any(
        x > y for x, y in zip(a, b, strict=True)
    )


class RecoveryPlanner:
    def __init__(self, providers: dict[str, RecoveryProvider]):
        self.providers = providers

    def plan(
        self,
        world: World,
        incident: Incident,
        policy: SearchPolicy | None = None,
        operator_priorities: dict[str, tuple[str, ...]] | None = None,
    ) -> PlanningResult:
        policy = policy or SearchPolicy()
        known_intents = {i.id for i in world.intents}
        if not set(policy.required_intent_ids) <= known_intents:
            raise ValueError("required intent does not exist in this world")
        started = monotonic()
        order = descendants(world, incident.trigger_commitment_id)
        originals = {c.id: c for c in world.commitments}
        frontier = [Branch(world)]
        evidence: list[ProviderResult] = []
        rejections = Counter()
        notes = []
        processed: set[str] = set()
        expansions = calls = 0
        incomplete = unavailable = False

        for depth, cid in enumerate(order):
            if depth >= policy.max_depth or monotonic() - started >= policy.max_seconds:
                incomplete = True
                notes.append("Depth or wall-clock budget reached; search is incomplete.")
                frontier = []
                break
            original = originals[cid]
            provider = self.providers.get(original.kind)
            result = None
            if provider is None:
                unavailable = True
                notes.append(f"No provider adapter for {cid}; recovery availability is unknown.")
            elif calls >= policy.max_tool_calls:
                incomplete = True
                notes.append(f"Tool budget reached before querying {cid}.")
            else:
                calls += 1
                try:
                    result = provider.query(original, world)
                    if result.commitment_id != cid:
                        raise ValueError("provider result belongs to another commitment")
                except Exception as exc:
                    # A query failure must never be converted into proof of unavailability.
                    unavailable = True
                    result = None
                    notes.append(f"Provider query failed for {cid}: {type(exc).__name__}.")
                if result is not None:
                    evidence.append(result)
                    if result.status == "ERROR" or result.expires_at <= datetime.now(UTC):
                        unavailable = True
                        notes.append(
                            f"Provider result for {cid} failed or expired; options ignored."
                        )
                        result = None
            options = result.options if result else ()
            priorities = (operator_priorities or {}).get(cid, ())
            # Semantic hints can change search order, never invent or authorize options.
            if priorities:
                options = tuple(
                    sorted(
                        options,
                        key=lambda option: (
                            priorities.index(option.resolution)
                            if option.resolution in priorities
                            else len(priorities)
                        ),
                    )
                )
            if len(options) > policy.max_options_per_commitment:
                incomplete = True
                notes.append(f"Option budget truncated inventory for {cid}.")
            options = options[: policy.max_options_per_commitment]
            next_frontier = []
            processed.add(cid)
            stop = False
            for branch in frontier:
                # Keep the current commitment only if it is actually feasible in this branch.
                preserve = RecoveryOption(
                    id=f"preserve:{cid}",
                    commitment_id=cid,
                    resolution="PRESERVED",
                    replacement=next(c for c in branch.world.commitments if c.id == cid),
                    explanation=f"Keep {original.title} at its booked time.",
                    evidence=f"authoritative-state:v{world.version}:{cid}",
                )
                for option in (preserve, *options):
                    if expansions >= policy.max_expansions or (
                        monotonic() - started >= policy.max_seconds
                    ):
                        incomplete = stop = True
                        break
                    expansions += 1
                    if (
                        original.intent_id in policy.required_intent_ids
                        and option.intent_quality < 1
                    ):
                        rejections["required_intent"] += 1
                        continue
                    actions = (*branch.actions, option)
                    if financials(actions)[0] > policy.max_additional_cost:
                        rejections["spending_limit"] += 1
                        continue
                    try:
                        candidate = apply_option(branch.world, option)
                        assessment = evaluate(candidate)
                    except ValueError:
                        rejections["invalid_operator"] += 1
                        continue
                    # Unprocessed downstream conflicts can be repaired in later rounds.
                    # A conflict at an already processed target cannot be deferred.
                    if any(
                        v.severity == "hard" and v.affected_commitment_ids[-1] in processed
                        for v in assessment.violations
                    ):
                        rejections["hard_constraint"] += 1
                        continue
                    next_frontier.append(Branch(candidate, actions))
                if stop:
                    break
            if len(next_frontier) > policy.max_candidates:
                incomplete = True
                notes.append("Candidate frontier truncated; optimality is not established.")
                # Stable and deterministic: inexpensive branches are retained first.
                next_frontier.sort(key=lambda b: financials(b.actions)[0])
                next_frontier = next_frontier[: policy.max_candidates]
            frontier = next_frontier
            if stop:
                notes.append("Expansion or wall-clock budget reached; search is incomplete.")
                # Only return candidates with an explicit resolution for every target.
                if depth != len(order) - 1:
                    frontier = []
                break
            if not frontier:
                break

        candidates = []
        for branch in frontier:
            assessment = evaluate(branch.world)
            if any(v.severity == "hard" for v in assessment.violations):
                rejections["global_hard_constraint"] += 1
                continue
            quality = {
                i.id: float(any(c.intent_id == i.id for c in branch.world.commitments))
                for i in sorted(world.intents, key=lambda i: i.id)
            }
            for action in branch.actions:
                quality[originals[action.commitment_id].intent_id] = action.intent_quality
            if any(quality[i] < 1 for i in policy.required_intent_ids):
                rejections["required_intent"] += 1
                continue
            used = {a.id for a in branch.actions if a.resolution != "PRESERVED"}
            if any(
                r.expires_at <= datetime.now(UTC) and any(o.id in used for o in r.options)
                for r in evidence
            ):
                unavailable = True
                rejections["expired_quote"] += 1
                continue
            cost, loss, refund = financials(branch.actions)
            candidates.append(
                CandidatePlan(
                    id=f"plan_{uuid4().hex}",
                    based_on_version=world.version,
                    world=branch.world,
                    actions=branch.actions,
                    intent_quality=quality,
                    additional_cost=cost,
                    loss=loss,
                    refund=refund,
                    soft_delay_minutes=sum(v.delay_minutes for v in assessment.violations),
                    assessment=assessment,
                    explanation=" ".join(a.explanation for a in branch.actions),
                )
            )
        pareto = tuple(
            c for c in candidates if not any(dominates(other, c) for other in candidates)
        )
        if pareto:
            status = "PARTIAL" if incomplete or unavailable else "COMPLETE"
        elif incomplete:
            status = "BUDGET_EXHAUSTED"
        elif unavailable:
            status = "BLOCKED"
        else:
            status = "NO_FEASIBLE_PLAN"
        notes.append(
            "Feasibility and exhaustion apply only to the queried fixture inventory and policy."
        )
        notes.append(
            "Plans are hypothetical and require approval and fresh checks before execution."
        )
        return PlanningResult(
            id=f"search_{uuid4().hex}",
            incident_id=incident.id,
            based_on_version=world.version,
            status=status,
            candidates=pareto,
            provider_results=tuple(evidence),
            rejections=dict(rejections),
            tool_calls=calls,
            expansions=expansions,
            policy=policy,
            notes=tuple(notes),
        )
