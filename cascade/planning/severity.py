from cascade.domain.models import IncidentSeverity
from cascade.planning.models import CandidatePlan, PlanningResult


def _undisturbed(plan: CandidatePlan) -> bool:
    return (
        all(a.resolution in ("PRESERVED", "RESCHEDULED") for a in plan.actions)
        and not plan.additional_cost
        and not plan.loss
    )


def classify(result: PlanningResult) -> IncidentSeverity:
    """Assign severity from what search actually found, never from model prose.

    GREEN  — a same-provider recovery exists at no cost and no loss.
    YELLOW — every intent can still be satisfied, but only with degradation.
    RED    — feasible plans exist; none of them keeps every intent alive.
    BLACK  — nothing feasible was found under this policy and inventory.
    """
    if not result.candidates:
        return "BLACK"
    if any(
        all(q == 1 for q in plan.intent_quality.values()) and _undisturbed(plan)
        for plan in result.candidates
    ):
        return "GREEN"
    if any(all(q == 1 for q in plan.intent_quality.values()) for plan in result.candidates):
        return "YELLOW"
    return "RED"
