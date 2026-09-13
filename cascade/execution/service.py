import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from cascade.constraints.engine import evaluate
from cascade.execution.simulation_models import ActionOutcome, Approval, Execution
from cascade.planning.demo import demo_planner
from cascade.planning.operators import apply_option
from cascade.service import CascadeService, ConflictError


def digest(plan):
    return hashlib.sha256(plan.model_dump_json().encode()).hexdigest()


class ExecutionService:
    """Each advance applies one mock action and verifies the remaining full plan.

    The provider inventory is queried again before effects. The in-memory world is
    the mock provider's recorded state; no external reservation APIs are contacted.
    """

    def __init__(self, core: CascadeService, providers=None):
        self.core = core
        self.providers = providers if providers is not None else demo_planner().providers
        self.approvals: dict[str, Approval] = {}
        self.executions: dict[str, Execution] = {}
        self.bases = {}
        self.fail_action_ids: set[str] = set()  # Explicit test injection, never exposed over HTTP.

    def _plan(self, plan_id, expected_version):
        if plan_id not in self.core.plans:
            raise KeyError(plan_id)
        plan = self.core.plans[plan_id]
        if expected_version != self.core.world.version or plan.based_on_version != expected_version:
            raise ConflictError("plan is stale; compare fresh recoveries before approval")
        if any(v.severity == "hard" for v in evaluate(plan.world).violations):
            raise ConflictError("plan fails deterministic feasibility")
        return plan

    def decide(self, incident_id, plan_id, expected_version, approve):
        with self.core.lock:
            plan = self._plan(plan_id, expected_version)
            if plan_id not in self.core.plan_incidents.get(incident_id, ()):
                raise ConflictError("plan belongs to a different incident")
            if any(e.status == "RUNNING" for e in self.executions.values()):
                raise ConflictError("finish or cancel the active execution first")
            status = "APPROVED" if approve else "REJECTED"
            prior = self.approvals.get(plan_id)
            if prior and prior.plan_digest == digest(plan) and prior.status == status:
                return prior
            result = Approval(
                id=f"approval_{uuid4().hex}",
                incident_id=incident_id,
                plan_id=plan_id,
                plan_digest=digest(plan),
                world_version=expected_version,
                additional_cost=plan.additional_cost,
                status=status,
                created_at=datetime.now(UTC),
            )
            self.approvals[plan_id] = result
            self.core.audit.append(
                {
                    "type": "plan.approved" if approve else "plan.rejected",
                    "approval": result.model_dump(mode="json"),
                }
            )
            return result

    def _fresh(self, base, action):
        if action.resolution == "PRESERVED":
            return
        current = next(c for c in base.commitments if c.id == action.commitment_id)
        provider = self.providers.get(current.kind)
        if not provider:
            raise ConflictError("provider unavailable; execution is blocked")
        try:
            result = provider.query(current, base)
        except Exception:
            raise ConflictError("provider query failed; no action executed") from None
        if result.status != "AVAILABLE" or result.expires_at <= datetime.now(UTC):
            raise ConflictError("provider availability is no longer confirmed")
        if not any(option == action for option in result.options):
            raise ConflictError("provider terms changed; a new plan and approval are required")

    def start(self, plan_id, approval_id, expected_version):
        with self.core.lock:
            for execution in self.executions.values():
                if execution.plan_id == plan_id and execution.approval_id == approval_id:
                    return execution
            plan = self._plan(plan_id, expected_version)
            approval = self.approvals.get(plan_id)
            if not approval or approval.id != approval_id or approval.status != "APPROVED":
                raise ConflictError("explicit approval is required before execution")
            if (
                approval.plan_digest != digest(plan)
                or approval.additional_cost != plan.additional_cost
            ):
                raise ConflictError("approval does not match this plan")
            if any(e.status == "RUNNING" for e in self.executions.values()):
                raise ConflictError("another execution is already running")
            for action in plan.actions:
                self._fresh(self.core.world, action)
            execution = Execution(
                id=f"execution_{uuid4().hex}",
                plan_id=plan_id,
                approval_id=approval_id,
                status="RUNNING",
                next_step=0,
                total_steps=len(plan.actions),
                expected_world_version=expected_version,
                outcomes=(),
                assessment=evaluate(self.core.world),
                message="Approved. Ready to simulate changes.",
            )
            self.bases[execution.id] = self.core.world
            self.executions[execution.id] = execution
            self.approvals[plan_id] = approval.model_copy(update={"status": "CONSUMED"})
            self.core.audit.append(
                {"type": "execution.started", "execution": execution.model_dump(mode="json")}
            )
            return execution

    def advance(self, execution_id, expected_step):
        with self.core.lock:
            if execution_id not in self.executions:
                raise KeyError(execution_id)
            execution = self.executions[execution_id]
            if expected_step < execution.next_step:
                return execution  # Retried step never creates a second effect.
            if expected_step != execution.next_step:
                raise ConflictError("execution step is out of order")
            if execution.status != "RUNNING":
                return execution
            plan = self.core.plans[execution.plan_id]
            approval = self.approvals[execution.plan_id]
            try:
                if digest(plan) != approval.plan_digest:
                    raise ConflictError("approved plan changed")
                if execution.expected_world_version != self.core.world.version:
                    raise ConflictError("world changed during execution; replan from current state")
                action = plan.actions[expected_step]
                self._fresh(self.bases[execution_id], action)
                # Verify the current remainder before committing the next side effect.
                projected = self.core.world
                for remaining in plan.actions[expected_step:]:
                    projected = apply_option(projected, remaining)
                if any(v.severity == "hard" for v in evaluate(projected).violations):
                    raise ConflictError("remaining actions are no longer feasible")
            except (ConflictError, ValueError, StopIteration) as exc:
                updated = execution.model_copy(
                    update={
                        "status": "BLOCKED",
                        "message": str(exc),
                        "assessment": evaluate(self.core.world),
                    }
                )
                self.executions[execution_id] = updated
                self.core.audit.append(
                    {"type": "execution.blocked", "execution": updated.model_dump(mode="json")}
                )
                return updated
            if action.id in self.fail_action_ids:
                outcome = ActionOutcome(
                    action_id=action.id,
                    commitment_id=action.commitment_id,
                    resolution=action.resolution,
                    status="FAILED",
                    explanation="Mock provider rejected the action.",
                    occurred_at=datetime.now(UTC),
                    world_version=self.core.world.version,
                    side_effect=False,
                )
                updated = execution.model_copy(
                    update={
                        "status": "FAILED",
                        "message": outcome.explanation,
                        "outcomes": (*execution.outcomes, outcome),
                    }
                )
            else:
                self.core.audit.append(
                    {"type": "action.started", "action_id": action.id, "execution_id": execution.id}
                )
                next_world = apply_option(self.core.world, action)
                changed = action.resolution != "PRESERVED"
                if changed:
                    next_world = next_world.model_copy(
                        update={"version": self.core.world.version + 1}
                    )
                    self.core.world = next_world
                recorded = next(
                    (c for c in self.core.world.commitments if c.id == action.commitment_id), None
                )
                verified = recorded == action.replacement
                assessment = evaluate(self.core.world)
                last = expected_step + 1 == len(plan.actions)
                verified = verified and (
                    not last or not any(v.severity == "hard" for v in assessment.violations)
                )
                outcome = ActionOutcome(
                    action_id=action.id,
                    commitment_id=action.commitment_id,
                    resolution=action.resolution,
                    status="VERIFIED" if verified else "FAILED",
                    explanation=action.explanation,
                    occurred_at=datetime.now(UTC),
                    world_version=self.core.world.version,
                    side_effect=changed,
                )
                updated = execution.model_copy(
                    update={
                        "status": ("SUCCEEDED" if last else "RUNNING") if verified else "FAILED",
                        "next_step": expected_step + 1,
                        "expected_world_version": self.core.world.version,
                        "outcomes": (*execution.outcomes, outcome),
                        "assessment": assessment,
                        "message": "Your rebuilt itinerary passes all hard checks."
                        if last and verified
                        else "Action verified in the simulated provider state."
                        if verified
                        else "Postcondition failed.",
                    }
                )
                if last and verified:
                    self.core._reconcile(assessment)
            self.executions[execution_id] = updated
            self.core.audit.append(
                {
                    "type": "action.verified" if outcome.status == "VERIFIED" else "action.failed",
                    "execution_id": execution.id,
                    "outcome": outcome.model_dump(mode="json"),
                }
            )
            if updated.status == "SUCCEEDED":
                self.core.audit.append(
                    {"type": "incident.resolved", "incident_id": approval.incident_id}
                )
            return updated

    def cancel(self, execution_id):
        with self.core.lock:
            if execution_id not in self.executions:
                raise KeyError(execution_id)
            execution = self.executions[execution_id]
            if execution.status == "RUNNING":
                execution = execution.model_copy(
                    update={
                        "status": "CANCELLED",
                        "message": "Remaining actions cancelled. Completed changes are retained.",
                    }
                )
                self.executions[execution_id] = execution
                self.core.audit.append(
                    {"type": "execution.cancelled", "execution_id": execution_id}
                )
            return execution
