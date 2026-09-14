from cascade.domain.models import IncidentSeverity
from cascade.planning.models import CandidatePlan, PlanningResult


def _undisturbed(plan: CandidatePlan) -> bool:
    return (
        all(a.resolution in ("PRESERVED", "RESCHEDULED") for a in plan.actions)
        and not plan.additional_cost
        and not plan.loss
    )


def classify(result: PlanningResult) -> IncidentSeverity | None:
    """Assign severity from search evidence, withholding non-existence claims when incomplete.

    GREEN and YELLOW report a qualifying plan that was found, regardless of search status.
    RED and BLACK report that no qualifying plan exists only after an exhaustive search.
    """
    if not result.candidates:
        return "BLACK" if result.status in ("NO_FEASIBLE_PLAN", "COMPLETE") else None
    if any(
        all(q == 1 for q in plan.intent_quality.values()) and _undisturbed(plan)
        for plan in result.candidates
    ):
        return "GREEN"
    if any(all(q == 1 for q in plan.intent_quality.values()) for plan in result.candidates):
        return "YELLOW"
    return "RED" if result.status == "COMPLETE" else None
