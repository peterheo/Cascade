from threading import RLock

from cascade.constraints.engine import evaluate
from cascade.domain.models import Commitment, EventResult, Incident, Mutation, World
from cascade.graph.traversal import descendants
from cascade.planning.demo import demo_planner
from cascade.planning.models import CandidatePlan, PlanningResult, SearchPolicy


class ConflictError(ValueError):
    """A stale version or reused event ID conflicts with authoritative state."""


class CascadeService:
    """Single-process repository, event transaction boundary, and read-only planning."""

    def __init__(self, world: World):
        evaluate(world)
        self.world = world
        self.events: dict[str, tuple[Mutation, EventResult]] = {}
        self.incidents: list[Incident] = []
        self.audit: list[dict] = []
        self.lock = RLock()
        self.plans: dict[str, CandidatePlan] = {}
        self.plan_incidents: dict[str, str] = {}
        self.latest_planning: PlanningResult | None = None

    def plan(
        self,
        incident_id: str,
        expected_version: int,
        policy: SearchPolicy | None = None,
        operator_priorities: dict[str, tuple[str, ...]] | None = None,
    ) -> PlanningResult:
        with self.lock:
            if expected_version != self.world.version:
                raise ConflictError("stale world version; fetch state before planning")
            incident = next((i for i in self.incidents if i.id == incident_id), None)
            if incident is None:
                raise KeyError(incident_id)
            result = demo_planner().plan(self.world, incident, policy, operator_priorities)
            self.plans.update({p.id: p for p in result.candidates})
            self.plan_incidents.update({p.id: incident_id for p in result.candidates})
            self.latest_planning = result
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
