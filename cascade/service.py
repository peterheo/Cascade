from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock

from cascade.constraints.engine import evaluate
from cascade.domain.models import (
    Assessment,
    Commitment,
    EventResult,
    Incident,
    Mutation,
    World,
)
from cascade.execution.executor import PlanExecutor
from cascade.execution.models import ExecutionResult
from cascade.graph.traversal import descendants
from cascade.planning.demo import demo_planner
from cascade.planning.models import CandidatePlan, PlanningResult, SearchPolicy
from cascade.planning.planner import RecoveryPlanner
from cascade.planning.severity import classify
from cascade.planning.skills import Skill, load_skills, select
from cascade.security.approvals import ApprovalRequest, grant, reject
from cascade.tools.demo import demo_gateway
from cascade.tools.gateway import ExecutionContext, ToolGateway
from cascade.tools.permissions import PermissionPolicy


class ConflictError(ValueError):
    """A stale version or reused event ID conflicts with authoritative state."""


class CascadeService:
    """Single-process repository, event transaction boundary, and read-only planning."""

    def __init__(
        self,
        world: World,
        gateway: ToolGateway | None = None,
        permissions: PermissionPolicy | None = None,
        planner: RecoveryPlanner | None = None,
    ):
        evaluate(world)
        self.world = world
        self.events: dict[str, tuple[Mutation, EventResult]] = {}
        self.incidents: list[Incident] = []
        self.audit: list[dict] = []
        self.lock = RLock()
        self.plans: dict[str, CandidatePlan] = {}
        self.plan_incidents: dict[str, tuple[str, ...]] = {}
        self.planner = planner or demo_planner()
        self.gateway = gateway or demo_gateway()
        self.executor = PlanExecutor(self.gateway)
        self.permissions = permissions or PermissionPolicy()
        self.skills = load_skills()
        self.approvals: dict[str, ApprovalRequest] = {}
        self.executions: dict[str, ExecutionResult] = {}

    def incident(self, incident_id: str) -> Incident:
        with self.lock:
            for item in self.incidents:
                if item.id == incident_id:
                    return item
        raise KeyError(incident_id)

    def _replace_incident(self, incident: Incident) -> None:
        self.incidents = [incident if i.id == incident.id else i for i in self.incidents]

    def _reconcile(self, assessment: Assessment) -> None:
        """Close incidents whose violations no longer exist, whatever resolved them."""
        active = {v.constraint_id for v in assessment.violations}
        for incident in list(self.incidents):
            if incident.status != "OPEN":
                continue
            if not any(v.constraint_id in active for v in incident.violations):
                self._replace_incident(
                    incident.model_copy(
                        update={
                            "status": "RESOLVED",
                            "resolved_at": datetime.now(UTC),
                            "resolution_note": "No violation from this incident remains in state.",
                        }
                    )
                )
                self.audit.append({"type": "incident.resolved", "incident_id": incident.id})

    def dismiss(self, incident_id: str, actor: str, note: str) -> Incident:
        """The user can decline recovery; that is an explicit outcome, not a failure."""
        with self.lock:
            incident = self.incident(incident_id)
            if incident.status != "OPEN":
                raise ConflictError("this incident is already closed")
            dismissed = incident.model_copy(
                update={
                    "status": "DISMISSED",
                    "resolved_at": datetime.now(UTC),
                    "resolution_note": note,
                }
            )
            self._replace_incident(dismissed)
            self.audit.append(
                {
                    "type": "incident.dismissed",
                    "incident_id": incident_id,
                    "actor": actor,
                    "note": note,
                }
            )
            return dismissed

    def approve(
        self,
        request_id: str,
        actor: str,
        approved_action_ids: tuple[str, ...],
        acknowledged_amount: Decimal,
    ) -> ApprovalRequest:
        with self.lock:
            if request_id not in self.approvals:
                raise KeyError(request_id)
            request = self.approvals[request_id]
            if request.based_on_version != self.world.version:
                raise ConflictError("the world changed; request approval against current state")
            decided = grant(request, actor, approved_action_ids, acknowledged_amount)
            self.approvals[request_id] = decided
            self.audit.append(
                {"type": "approval.granted", "approval": decided.model_dump(mode="json")}
            )
            return decided

    def reject(self, request_id: str, actor: str, note: str) -> ApprovalRequest:
        with self.lock:
            if request_id not in self.approvals:
                raise KeyError(request_id)
            decided = reject(self.approvals[request_id], actor, note)
            self.approvals[request_id] = decided
            self.audit.append(
                {"type": "approval.rejected", "approval": decided.model_dump(mode="json")}
            )
            return decided

    def execute(self, plan_id: str, expected_version: int, actor: str) -> ExecutionResult:
        """Run one approved plan. Side effects commit step by step, never in bulk."""
        with self.lock:
            if plan_id not in self.plans:
                raise KeyError(plan_id)
            if expected_version != self.world.version:
                raise ConflictError("stale world version; fetch state before executing")
            plan = self.plans[plan_id]
            incident_id = next(
                (i.id for i in self.incidents if plan.id in self.plan_incidents.get(i.id, ())),
                "",
            )
            context = ExecutionContext(
                plan_id=plan.id,
                world_version=self.world.version,
                actor=actor,
                policy=self.permissions,
            )
            result = self.executor.execute(
                self.world,
                plan,
                incident_id,
                context,
                tuple(self.approvals.values()),
            )
            if result.approval_request is not None:
                self.approvals[result.approval_request.id] = result.approval_request
            if result.world.version != self.world.version:
                self.world = result.world
                self._reconcile(evaluate(self.world))
            if result.status == "COMPLETED" and incident_id:
                incident = self.incident(incident_id)
                if incident.status == "OPEN":
                    self._replace_incident(
                        incident.model_copy(
                            update={
                                "status": "RESOLVED",
                                "resolved_at": datetime.now(UTC),
                                "resolution_note": (
                                    f"Recovery plan {plan.id} executed and verified."
                                ),
                            }
                        )
                    )
            self.executions[result.id] = result
            self.audit.append(
                {"type": "recovery.executed", "result": result.model_dump(mode="json")}
            )
            return result

    def skill_for(self, incident: Incident, name: str | None) -> Skill | None:
        """An explicit name wins; otherwise the most specific matching trigger does."""
        if name is None:
            return select(self.world, incident, self.skills)
        chosen = [s for s in self.skills if s.name == name]
        if not chosen:
            raise ValueError(f"unknown recovery skill: {name}")
        return max(chosen, key=lambda s: s.version)

    def plan(
        self,
        incident_id: str,
        expected_version: int,
        policy: SearchPolicy | None = None,
        operator_priorities: dict[str, tuple[str, ...]] | None = None,
        skill_name: str | None = None,
    ) -> PlanningResult:
        with self.lock:
            if expected_version != self.world.version:
                raise ConflictError("stale world version; fetch state before planning")
            incident = next((i for i in self.incidents if i.id == incident_id), None)
            if incident is None:
                raise KeyError(incident_id)
            skill = self.skill_for(incident, skill_name)
            if skill is not None:
                # An explicit policy from the caller outranks the template's limits, and
                # explicit operator priorities outrank its ordering.
                policy = skill.policy(SearchPolicy()) if policy is None else policy
                operator_priorities = operator_priorities or skill.priorities(self.world, incident)
            result = self.planner.plan(
                self.world,
                incident,
                policy,
                operator_priorities,
                skill.ref if skill else None,
            )
            self.plans.update({p.id: p for p in result.candidates})
            self.plan_incidents[incident.id] = tuple(p.id for p in result.candidates)
            if incident.status == "OPEN":
                self._replace_incident(incident.model_copy(update={"severity": classify(result)}))
            self.audit.append(
                {"type": "recovery.planned", "result": result.model_dump(mode="json")}
            )
            return result

    def ingest(self, event: Mutation) -> EventResult:
        with self.lock:
            if event.event_id in self.events:
                original, result = self.events[event.event_id]
                if original != event:
                    raise ConflictError("event ID already used with a different payload")
                return result
            if event.expected_version != self.world.version:
                raise ConflictError("stale world version; fetch state and retry")
            if event.source.confidence < 0.9:
                raise ConflictError("low-confidence mutation requires confirmation")
            current = next((c for c in self.world.commitments if c.id == event.commitment_id), None)
            if current is None:
                raise KeyError(event.commitment_id)
            updated = Commitment.model_validate(
                {
                    **current.model_dump(),
                    "start_at": event.new_start_at,
                    "end_at": event.new_end_at,
                    "source": event.source,
                }
            )
            before = evaluate(self.world)
            world = World.model_validate(
                {
                    **self.world.model_dump(),
                    "commitments": tuple(
                        updated if c.id == updated.id else c for c in self.world.commitments
                    ),
                    "version": self.world.version + 1,
                }
            )
            assessment = evaluate(world)
            previous = {v.constraint_id: v.delay_minutes for v in before.violations}
            worsened = tuple(
                v
                for v in assessment.violations
                if v.delay_minutes > previous.get(v.constraint_id, 0)
            )
            affected = descendants(world, current.id)
            incident = None
            if worsened:
                threatened = {cid for v in worsened for cid in v.affected_commitment_ids}
                incident = Incident(
                    id=f"inc_{event.event_id}",
                    trigger_event_id=event.event_id,
                    trigger_commitment_id=current.id,
                    affected_commitment_ids=affected,
                    threatened_intent_ids=tuple(
                        sorted(
                            {
                                c.intent_id
                                for c in world.commitments
                                if c.id in threatened and c.id != current.id
                            }
                        )
                    ),
                    violations=worsened,
                )
            result = EventResult(
                event_id=event.event_id,
                world_version=world.version,
                incident=incident,
                assessment=assessment,
            )
            self.world = world
            self.events[event.event_id] = (event, result)
            if incident:
                self.incidents.append(incident)
            self.audit.append(
                {
                    "event_id": event.event_id,
                    "type": "state.assessed",
                    "world_version": world.version,
                    "source": event.source.model_dump(),
                    "mutation": event.model_dump(mode="json"),
                    "before": current.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                }
            )
            return result
