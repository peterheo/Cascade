from datetime import UTC, datetime
from uuid import uuid4

from cascade.constraints.engine import evaluate
from cascade.domain.models import World
from cascade.execution.models import ExecutionResult, ExecutionStep
from cascade.planning.models import CandidatePlan, RecoveryOption
from cascade.planning.operators import apply_option
from cascade.security.approvals import ApprovalRequest, approval_request
from cascade.tools.gateway import (
    ExecutionContext,
    PostCondition,
    ToolAction,
    ToolCall,
    ToolGateway,
    build_action,
)
from cascade.tools.permissions import Operation

# Each resolution maps to exactly one external operation. PRESERVED has none.
OPERATIONS: dict[str, Operation] = {
    "RESCHEDULED": "reschedule",
    "SUBSTITUTED": "book",
    "COMPENSATED": "refund",
    "ABANDONED": "cancel",
}


def action_for(plan: CandidatePlan, option: RecoveryOption, provider: str) -> ToolAction | None:
    """Derive the one write a recovery option needs. IDs are stable across retries."""
    if option.resolution == "PRESERVED":
        return None
    replacement = option.replacement
    postcondition = PostCondition(
        commitment_present=replacement is not None,
        start_at=replacement.start_at if replacement else None,
        end_at=replacement.end_at if replacement else None,
        min_refund=option.refund,
    )
    return build_action(
        action_id=f"act:{plan.id}:{option.id}",
        plan_id=plan.id,
        commitment_id=option.commitment_id,
        provider=provider,
        operation=OPERATIONS[option.resolution],
        amount=option.additional_cost,
        option_id=option.id,
        idempotency_key=f"{plan.based_on_version}:{option.commitment_id}:{option.id}",
        postcondition=postcondition,
        description=option.explanation,
    )


def precheck_for(action: ToolAction) -> ToolAction:
    return build_action(
        action_id=f"{action.id}:check",
        plan_id=action.plan_id,
        commitment_id=action.commitment_id,
        provider=action.provider,
        operation="check_availability",
        option_id=action.option_id,
        idempotency_key=f"{action.idempotency_key}:check",
        postcondition=action.postcondition,
        description=f"Re-check availability before {action.operation}.",
    )


class PlanExecutor:
    """Authorize everything first, then execute one verified step at a time."""

    def __init__(self, gateway: ToolGateway, calendar_links: dict[str, dict] | None = None):
        self.gateway = gateway
        self.calendar_links = {} if calendar_links is None else calendar_links

    def actions(
        self, world: World, plan: CandidatePlan
    ) -> tuple[tuple[RecoveryOption, ToolAction | None], ...]:
        kinds = {c.id: c.kind for c in world.commitments}
        return tuple(
            (
                option,
                action_for(
                    plan,
                    option,
                    "icloud"
                    if option.commitment_id in self.calendar_links
                    else f"mock_{kinds[option.commitment_id]}",
                ),
            )
            for option in plan.actions
        )

    def execute(
        self,
        world: World,
        plan: CandidatePlan,
        incident_id: str,
        context: ExecutionContext,
        approvals: tuple[ApprovalRequest, ...] = (),
    ) -> ExecutionResult:
        started = datetime.now(UTC)
        granted = tuple(
            action_id
            for approval in approvals
            if approval.status == "APPROVED"
            and approval.plan_id == plan.id
            and approval.based_on_version == world.version
            for action_id in approval.approved_action_ids
        )
        context = context.model_copy(
            update={"approved_action_ids": (*context.approved_action_ids, *granted)}
        )

        def result(
            status,
            steps,
            *,
            notes=(),
            world_after=world,
            request=None,
            effects=0,
            replan=False,
            verified=False,
        ):
            assessment = evaluate(world_after)
            return ExecutionResult(
                id=f"exec_{uuid4().hex}",
                plan_id=plan.id,
                incident_id=incident_id,
                based_on_version=plan.based_on_version,
                status=status,
                steps=tuple(steps),
                approval_request=request,
                world=world_after,
                world_version_after=world_after.version,
                remaining_violations=assessment.violations,
                side_effects=effects,
                replan_required=replan,
                verified=verified,
                started_at=started,
                notes=tuple(notes),
            )

        if plan.based_on_version != world.version:
            return result(
                "STALE",
                [
                    self._step(o, "NOT_ATTEMPTED", "The world changed after this plan was built.")
                    for o in plan.actions
                ],
                notes=("Replan against the current version before executing.",),
                replan=True,
            )
        if any(v.severity == "hard" for v in evaluate(plan.world).violations):
            return result(
                "BLOCKED",
                [
                    self._step(o, "NOT_ATTEMPTED", "This plan no longer passes the constraints.")
                    for o in plan.actions
                ],
                notes=("Constraint verification precedes permission checks.",),
                replan=True,
            )
        pairs = self.actions(world, plan)

        # Authorization sweep: policy and sandbox clear every write before any of them runs.
        denied: dict[str, ToolCall] = {}
        pending: dict[str, ToolCall] = {}
        for option, action in pairs:
            if action is None:
                continue
            blocked = self.gateway.authorize(action, context)
            if blocked is None:
                continue
            if blocked.decision == "REQUIRES_APPROVAL":
                pending[option.id] = blocked
            else:
                denied[option.id] = blocked
        if denied:
            return result(
                "DENIED",
                [
                    self._step(
                        o,
                        "DENIED" if o.id in denied else "NOT_ATTEMPTED",
                        denied[o.id].reason
                        if o.id in denied
                        else "Not attempted because another action was denied.",
                        call=denied.get(o.id),
                    )
                    for o, _ in pairs
                ],
                notes=("Permission is independent of confidence; nothing was executed.",),
            )
        if pending:
            return result(
                "AWAITING_APPROVAL",
                [
                    self._step(
                        o,
                        "REQUIRES_APPROVAL" if o.id in pending else "NOT_ATTEMPTED",
                        pending[o.id].reason
                        if o.id in pending
                        else "Held until the approval decision is made.",
                        call=pending.get(o.id),
                    )
                    for o, _ in pairs
                ],
                request=approval_request(
                    plan.id,
                    incident_id,
                    world.version,
                    tuple((call.action, call.reason) for call in pending.values()),
                ),
                notes=("No external effect has occurred.",),
            )

        current = world
        steps: list[ExecutionStep] = []
        effects = 0
        notes: list[str] = []
        remaining = [option.commitment_id for option, action in pairs if action is not None]
        stopped = False
        for option, action in pairs:
            if stopped:
                steps.append(
                    self._step(option, "NOT_ATTEMPTED", "Execution stopped before this step.")
                )
                continue
            if action is None:
                steps.append(
                    self._step(
                        option,
                        "SKIPPED",
                        "Preserved commitments need no external action.",
                        version=current.version,
                    )
                )
                continue
            remaining.remove(option.commitment_id)
            precheck = self.gateway.query(precheck_for(action), context)
            if precheck.result is None or not precheck.result.success:
                steps.append(
                    ExecutionStep(
                        id=f"step_{uuid4().hex}",
                        commitment_id=option.commitment_id,
                        resolution=option.resolution,
                        status="PRECHECK_FAILED",
                        precheck=precheck,
                        note=precheck.result.detail if precheck.result else precheck.reason,
                    )
                )
                notes.append(f"External state moved before {option.commitment_id} was written.")
                stopped = True
                continue
            call = self.gateway.execute(action, context)
            landed = bool(call.result and call.result.side_effect)
            effects += int(landed)
            if call.result is None or not call.result.success:
                steps.append(
                    ExecutionStep(
                        id=f"step_{uuid4().hex}",
                        commitment_id=option.commitment_id,
                        resolution=option.resolution,
                        status="FAILED",
                        precheck=precheck,
                        call=call,
                        note=call.result.detail if call.result else call.reason,
                    )
                )
                if call.result is not None and call.result.side_effect:
                    notes.append(
                        f"{option.commitment_id} external state is uncertain; reconcile with "
                        "the provider before replanning."
                    )
                else:
                    notes.append(
                        f"{option.commitment_id} did not reach a verified state; replan from"
                        " the actual world."
                    )
                stopped = True
                continue
            # Recompute immediately: the rest of the plan is not assumed to still hold.
            applied = apply_option(current, option)
            current = World.model_validate({**applied.model_dump(), "version": current.version + 1})
            assessment = evaluate(current)
            steps.append(
                ExecutionStep(
                    id=f"step_{uuid4().hex}",
                    commitment_id=option.commitment_id,
                    resolution=option.resolution,
                    status="EXECUTED",
                    precheck=precheck,
                    call=call,
                    world_version_after=current.version,
                    violations_after=assessment.violations,
                    note=f"{call.result.operation} verified against"
                    f" {call.result.external_reference}.",
                )
            )
            unrepairable = [
                v
                for v in assessment.violations
                if v.severity == "hard" and v.affected_commitment_ids[-1] not in remaining
            ]
            if unrepairable:
                notes.append(
                    "A hard violation appeared at a commitment with no remaining step; stopping."
                )
                stopped = True

        assessment = evaluate(current)
        hard = [v for v in assessment.violations if v.severity == "hard"]
        expected = plan.world.model_dump(exclude={"version"})
        matches_plan = current.model_dump(exclude={"version"}) == expected
        completed = not stopped and not hard and matches_plan
        if completed:
            notes.append("Every action is confirmed by its provider and re-verified in the world.")
            status = "COMPLETED"
        elif effects:
            status = "PARTIAL"
        else:
            status = "BLOCKED"
        if not completed:
            notes.append("Replan from the actual current state rather than the original plan.")
        return result(
            status,
            steps,
            notes=notes,
            world_after=current,
            effects=effects,
            replan=not completed,
            verified=completed,
        )

    @staticmethod
    def _step(
        option: RecoveryOption,
        status: str,
        note: str,
        version: int | None = None,
        call: ToolCall | None = None,
    ) -> ExecutionStep:
        return ExecutionStep(
            id=f"step_{uuid4().hex}",
            commitment_id=option.commitment_id,
            resolution=option.resolution,
            status=status,
            world_version_after=version,
            call=call,
            note=note,
        )
