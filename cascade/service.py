from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock

from cascade.connectors.calendar import CalendarEvent
from cascade.constraints.engine import evaluate
from cascade.domain.models import (
    Assessment,
    Commitment,
    EventResult,
    Incident,
    Intent,
    Mutation,
    World,
)
from cascade.execution.executor import PlanExecutor
from cascade.execution.models import ExecutionResult
from cascade.graph.traversal import descendants
from cascade.memory.preferences import Preference, PreferenceError, narrow, promote
from cascade.memory.resolutions import (
    ResolutionRecord,
    record_dismissal,
    record_execution,
    suggest,
)
from cascade.observability.events import EventStream
from cascade.persistence import MemoryStore, StateStore
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
        store: StateStore | None = None,
    ):
        self.store = store or MemoryStore()
        persisted = self.store.load()
        initial_world = persisted.world if persisted and persisted.world is not None else world
        evaluate(initial_world)
        self.world = initial_world
        self.lock = RLock()
        self.events: dict[str, tuple[Mutation, EventResult]] = persisted.events if persisted else {}
        self.incidents: list[Incident] = persisted.incidents if persisted else []
        self.audit: list[dict] = persisted.audit if persisted else []
        self.plans: dict[str, CandidatePlan] = {}
        self.plan_incidents: dict[str, tuple[str, ...]] = (
            dict(persisted.plan_incidents) if persisted else {}
        )
        self.planner = planner or demo_planner()
        self.gateway = gateway or demo_gateway()
        self.calendar_links: dict[str, dict] = dict(persisted.calendar_links) if persisted else {}
        self.calendar_undos: dict[str, dict] = dict(persisted.calendar_undos) if persisted else {}
        self.calendar_metadata: dict[str, dict] = (
            dict(persisted.calendar_metadata) if persisted else {}
        )
        self.executor = PlanExecutor(self.gateway, calendar_links=self.calendar_links)
        self.permissions = permissions or PermissionPolicy()
        self.skills = load_skills()
        self.preferences: dict[str, Preference] = persisted.preferences if persisted else {}
        self.resolutions: list[ResolutionRecord] = persisted.resolutions if persisted else []
        self.searches: dict[str, PlanningResult] = {}
        self.latest_planning: PlanningResult | None = None
        self.stream = EventStream()
        self.approvals: dict[str, ApprovalRequest] = dict(persisted.approvals) if persisted else {}
        self.executions: dict[str, ExecutionResult] = (
            dict(persisted.executions) if persisted else {}
        )
        self.ledger_orphans: dict[str, dict] = dict(persisted.ledger_orphans) if persisted else {}
        if persisted:
            for planning in persisted.searches.values():
                for candidate in planning.candidates:
                    self.plans[candidate.id] = candidate
                    self.searches[candidate.id] = planning
            self.latest_planning = (
                persisted.searches.get(persisted.latest_search_id)
                if persisted.latest_search_id
                else None
            )
        if persisted is None or persisted.world is None:
            self.store.save_world(self.world)
        self._detect_ledger_orphans()

    def _record_audit(self, body: dict) -> None:
        with self.store.transaction():
            self.audit.append(body)
            self.store.append("audit", body)

    def _record_resolution(self, record: ResolutionRecord) -> None:
        with self.store.transaction():
            self.resolutions.append(record)
            self.store.append("resolution", record.model_dump(mode="json"))

    def _save_world(self) -> None:
        self.store.save_world(self.world)

    def _save_incident(self, incident: Incident) -> None:
        self.store.put("incident", incident.id, incident)

    def _save_preference(self, preference: Preference) -> None:
        self.store.put("preference", preference.id, preference)

    def _detect_ledger_orphans(self) -> None:
        """Report durable provider writes that no persisted execution can explain."""
        all_entries = []
        for provider_name, provider in self.gateway.providers.items():
            ledger = getattr(provider, "ledger", None)
            if ledger is None:
                continue
            all_entries.extend(
                (provider_name, key, value)
                for key, value in ledger.items()
                if provider_name != "icloud"
                or (value.get("put_started") and value.get("state") != "refused")
            )
        if not all_entries:
            return
        referenced = {
            (step.call.action.provider, step.call.action.idempotency_key)
            for execution in self.executions.values()
            for step in execution.steps
            if step.call is not None
            and (
                step.status == "EXECUTED"
                or (step.call.result is not None and step.call.result.side_effect)
            )
        }
        with self.lock, self.store.transaction():
            for provider, key, value in all_entries:
                orphan_key = f"{provider}:{key}"
                if (provider, key) in referenced or orphan_key in self.ledger_orphans:
                    continue
                body = {
                    "type": "ledger.orphan_detected",
                    "provider": provider,
                    "idempotency_key": key,
                    "reference": value.get("reference"),
                }
                self.ledger_orphans[orphan_key] = body
                self.store.put("ledger_orphan", orphan_key, body)
                self._record_audit(body)

    def ledger_orphan_entries(self) -> tuple[dict, ...]:
        with self.lock:
            return tuple(self.ledger_orphans.values())

    def sync_calendar(
        self,
        events: tuple[CalendarEvent, ...],
        *,
        skipped_all_day: int = 0,
        skipped_recurring: int = 0,
        sync_started_version: int | None = None,
        persist_event_text: bool = False,
    ) -> dict:
        """Merge imported calendar commitments and retain incident-linked deletions."""
        incoming = {item.commitment.id: item for item in events}
        with self.lock, self.store.transaction():
            if sync_started_version is not None and self.world.version != sync_started_version:
                return {
                    "imported": 0,
                    "skipped_all_day": skipped_all_day,
                    "skipped_recurring": skipped_recurring,
                    "stale_retained": 0,
                    "changed": False,
                    "stale": True,
                    "version": self.world.version,
                }
            old_ids = set(self.calendar_links)
            stale_retained = 0
            retained_ids: set[str] = set()
            open_refs = {
                commitment_id
                for incident in self.incidents
                if incident.status == "OPEN"
                for commitment_id in (
                    incident.affected_commitment_ids + (incident.trigger_commitment_id,)
                )
            }
            for commitment_id in old_ids - set(incoming):
                if commitment_id in open_refs:
                    retained_ids.add(commitment_id)
                    stale_retained += 1
                else:
                    self.calendar_links.pop(commitment_id, None)
                    self.calendar_metadata.pop(commitment_id, None)
                    self.store.delete("calendar_link", commitment_id)
                    self.store.delete("calendar_metadata", commitment_id)

            imported_old = {c.id: c for c in self.world.commitments if c.id in old_ids}
            ordinary = [c for c in self.world.commitments if c.id not in old_ids]
            merged = [*ordinary]
            intents = [
                i for i in self.world.intents if i.id not in {f"intent_{cid}" for cid in old_ids}
            ]
            for commitment_id, item in incoming.items():
                merged.append(item.commitment)
                intents.append(
                    Intent(
                        id=item.commitment.intent_id,
                        description=item.commitment.title,
                        importance=0.5,
                        source="derived",
                    )
                )
                self.calendar_links[commitment_id] = item.link
                self.store.put("calendar_link", commitment_id, item.link)
                metadata = {
                    key: value
                    for key, value in item.metadata.items()
                    if key == "STATUS" or persist_event_text
                }
                self.calendar_metadata[commitment_id] = metadata
                self.store.put("calendar_metadata", commitment_id, metadata)
            for commitment_id in retained_ids:
                if commitment_id in imported_old:
                    merged.append(imported_old[commitment_id])
                    intents.append(
                        Intent(
                            id=imported_old[commitment_id].intent_id,
                            description=imported_old[commitment_id].title,
                            importance=0.5,
                            source="derived",
                        )
                    )
            merged.sort(key=lambda item: item.id)
            intents.sort(key=lambda item: item.id)
            ids = {c.id for c in merged}
            world = self.world.model_copy(
                update={
                    "commitments": tuple(merged),
                    "intents": tuple(intents),
                    "dependencies": tuple(
                        d for d in self.world.dependencies if d.from_id in ids and d.to_id in ids
                    ),
                    "deadlines": tuple(d for d in self.world.deadlines if d.commitment_id in ids),
                }
            )
            changed = world.model_dump(exclude={"version"}) != self.world.model_dump(
                exclude={"version"}
            )
            if changed:
                self.world = world.model_copy(update={"version": self.world.version + 1})
                self._save_world()
                self._reconcile(evaluate(self.world))
                self.stream.publish("state.changed", self.world.version, reason="calendar_sync")
            self.executor.calendar_links = self.calendar_links
            return {
                "imported": len(incoming),
                "skipped_all_day": skipped_all_day,
                "skipped_recurring": skipped_recurring,
                "stale_retained": stale_retained,
                "changed": changed,
                "stale": False,
                "version": self.world.version,
            }

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
                resolved = incident.model_copy(
                    update={
                        "status": "RESOLVED",
                        "resolved_at": datetime.now(UTC),
                        "resolution_note": "No violation from this incident remains in state.",
                    }
                )
                self._replace_incident(resolved)
                self._save_incident(resolved)
                self._record_audit({"type": "incident.resolved", "incident_id": incident.id})
                self.stream.publish(
                    "incident.resolved", self.world.version, incident_id=incident.id
                )

    def dismiss(self, incident_id: str, actor: str, note: str) -> Incident:
        """The user can decline recovery; that is an explicit outcome, not a failure."""
        with self.lock, self.store.transaction():
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
            self._save_incident(dismissed)
            self._record_resolution(record_dismissal(incident_id, incident.severity))
            self._record_audit(
                {
                    "type": "incident.dismissed",
                    "incident_id": incident_id,
                    "actor": actor,
                    "note": note,
                }
            )
            self.stream.publish(
                "incident.updated", self.world.version, incident_id=incident_id, status="DISMISSED"
            )
            return dismissed

    def add_preference(self, item: Preference) -> Preference:
        with self.lock, self.store.transaction():
            if item.source == "learned" and item.status == "ACTIVE":
                raise PreferenceError("a learned preference must be promoted, not created active")
            narrow(SearchPolicy(), (*self.preferences.values(), item), self.world)
            self.preferences[item.id] = item
            self._save_preference(item)
            self._record_audit(
                {"type": "preference.added", "preference": item.model_dump(mode="json")}
            )
            self.stream.publish("preference.changed", self.world.version, preference_id=item.id)
            return item

    def set_preference_status(self, preference_id: str, status: str, actor: str) -> Preference:
        with self.lock, self.store.transaction():
            if preference_id not in self.preferences:
                raise KeyError(preference_id)
            updated = promote(self.preferences[preference_id], status, actor)
            others = tuple(p for p in self.preferences.values() if p.id != preference_id)
            narrow(SearchPolicy(), (*others, updated), self.world)
            self.preferences[preference_id] = updated
            self._save_preference(updated)
            self._record_audit(
                {
                    "type": "preference.updated",
                    "preference_id": preference_id,
                    "status": status,
                    "actor": actor,
                }
            )
            self.stream.publish(
                "preference.changed", self.world.version, preference_id=preference_id
            )
            return updated

    def delete_preference(self, preference_id: str) -> None:
        with self.lock, self.store.transaction():
            if preference_id not in self.preferences:
                raise KeyError(preference_id)
            del self.preferences[preference_id]
            self.store.delete("preference", preference_id)
            self._record_audit({"type": "preference.deleted", "preference_id": preference_id})
            self.stream.publish(
                "preference.changed", self.world.version, preference_id=preference_id
            )

    def suggestions(self) -> tuple[Preference, ...]:
        """What repeated choices imply. Nothing here is applied until a person says so."""
        with self.lock:
            return suggest(tuple(self.resolutions), self.world)

    def approve(
        self,
        request_id: str,
        actor: str,
        approved_action_ids: tuple[str, ...],
        acknowledged_amount: Decimal,
    ) -> ApprovalRequest:
        with self.lock, self.store.transaction():
            if request_id not in self.approvals:
                raise KeyError(request_id)
            request = self.approvals[request_id]
            if request.based_on_version != self.world.version:
                raise ConflictError("the world changed; request approval against current state")
            decided = grant(request, actor, approved_action_ids, acknowledged_amount)
            self.approvals[request_id] = decided
            self.store.put("approval", request_id, decided)
            self._record_audit(
                {"type": "approval.granted", "approval": decided.model_dump(mode="json")}
            )
            self.stream.publish(
                "approval.decided", self.world.version, approval_id=request_id, approved=True
            )
            return decided

    def reject(self, request_id: str, actor: str, note: str) -> ApprovalRequest:
        with self.lock, self.store.transaction():
            if request_id not in self.approvals:
                raise KeyError(request_id)
            decided = reject(self.approvals[request_id], actor, note)
            self.approvals[request_id] = decided
            self.store.put("approval", request_id, decided)
            self._record_audit(
                {"type": "approval.rejected", "approval": decided.model_dump(mode="json")}
            )
            self.stream.publish(
                "approval.decided", self.world.version, approval_id=request_id, approved=False
            )
            return decided

    def execute(self, plan_id: str, expected_version: int, actor: str) -> ExecutionResult:
        """Run one approved plan. Side effects commit step by step, never in bulk."""
        with self.lock, self.store.transaction():
            before_world = self.world
            before_incidents = self.incidents.copy()
            before_resolutions = self.resolutions.copy()
            before_audit = self.audit.copy()
            before_approvals = self.approvals.copy()
            before_executions = self.executions.copy()
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
            try:
                result = self.executor.execute(
                    self.world,
                    plan,
                    incident_id,
                    context,
                    tuple(self.approvals.values()),
                )
                if result.approval_request is not None:
                    self.approvals[result.approval_request.id] = result.approval_request
                    self.store.put("approval", result.approval_request.id, result.approval_request)
                if result.world.version != self.world.version:
                    self.world = result.world
                    self._save_world()
                    self._reconcile(evaluate(self.world))
                if result.status == "COMPLETED" and incident_id:
                    incident = self.incident(incident_id)
                    if incident.status == "OPEN":
                        resolved = incident.model_copy(
                            update={
                                "status": "RESOLVED",
                                "resolved_at": datetime.now(UTC),
                                "resolution_note": (
                                    f"Recovery plan {plan.id} executed and verified."
                                ),
                            }
                        )
                        self._replace_incident(resolved)
                        self._save_incident(resolved)
                self.executions[result.id] = result
                self.store.put("execution", result.id, result)
                if result.status == "COMPLETED" and plan.id in self.searches:
                    severity = self.incident(incident_id).severity if incident_id else None
                    self._record_resolution(
                        record_execution(self.searches[plan.id], result, severity)
                    )
                self._record_audit(
                    {"type": "recovery.executed", "result": result.model_dump(mode="json")}
                )
            except BaseException:
                # A provider ledger commits independently. If the service transaction
                # fails after that write, restore the in-memory view as well so the
                # next restart reports the ledger entry as an orphan.
                self.world = before_world
                self.incidents = before_incidents
                self.resolutions = before_resolutions
                self.audit = before_audit
                self.approvals = before_approvals
                self.executions = before_executions
                raise
            if result.approval_request is not None:
                self.stream.publish(
                    "approval.required",
                    self.world.version,
                    approval_id=result.approval_request.id,
                    plan_id=plan.id,
                )
            self.stream.publish(
                "action.completed",
                self.world.version,
                execution_id=result.id,
                status=result.status,
                side_effects=result.side_effects,
            )
            if result.side_effects:
                self.stream.publish("state.changed", self.world.version, reason="execution")
            return result

    def record_calendar_undo(self, execution_id: str, step_id: str, outcome: dict) -> None:
        """Apply the prior calendar interval to the authoritative world after an undo."""
        with self.lock, self.store.transaction():
            execution = self.executions.get(execution_id)
            if execution is None:
                raise KeyError(execution_id)
            step = next((item for item in execution.steps if item.id == step_id), None)
            if step is None or step.call is None or step.call.action.provider != "icloud":
                raise KeyError(step_id)
            commitment_id = outcome.get("commitment_id") or step.commitment_id
            start_at, end_at = outcome.get("start_at"), outcome.get("end_at")
            if start_at is not None and end_at is not None:
                commitments = tuple(
                    item.model_copy(update={"start_at": start_at, "end_at": end_at})
                    if item.id == commitment_id
                    else item
                    for item in self.world.commitments
                )
                if commitments != self.world.commitments:
                    self.world = self.world.model_copy(
                        update={"commitments": commitments, "version": self.world.version + 1}
                    )
                    self._save_world()
                    self._reconcile(evaluate(self.world))
            link = self.calendar_links.get(commitment_id)
            if link is not None and outcome.get("etag"):
                self.calendar_links[commitment_id] = {**link, "etag": outcome["etag"]}
                self.store.put("calendar_link", commitment_id, self.calendar_links[commitment_id])
            self._record_audit(
                {"type": "calendar.undone", "execution_id": execution_id, "step_id": step_id}
            )
            self.stream.publish(
                "state.changed",
                self.world.version,
                reason="calendar_undo",
                execution_id=execution_id,
            )

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
        with self.lock, self.store.transaction():
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
            # Stored preferences are the highest authority, so they narrow whatever
            # policy is in effect rather than being overridden by it.
            policy = narrow(policy or SearchPolicy(), tuple(self.preferences.values()), self.world)
            result = self.planner.plan(
                self.world,
                incident,
                policy,
                operator_priorities,
                skill.ref if skill else None,
            )
            self.plans.update({p.id: p for p in result.candidates})
            self.searches.update({p.id: result for p in result.candidates})
            self.latest_planning = result
            self.plan_incidents[incident.id] = tuple(p.id for p in result.candidates)
            self.store.put("search", result.id, result)
            for candidate in result.candidates:
                self.store.put("plan", candidate.id, {"search_id": result.id})
            self.store.put(
                "incident_plans",
                incident.id,
                {"plan_ids": list(self.plan_incidents[incident.id])},
            )
            if incident.status == "OPEN":
                updated_incident = incident.model_copy(update={"severity": classify(result)})
                self._replace_incident(updated_incident)
                self._save_incident(updated_incident)
            self._record_audit(
                {"type": "recovery.planned", "result": result.model_dump(mode="json")}
            )
            self.stream.publish(
                "recovery.plan.created",
                self.world.version,
                incident_id=incident_id,
                search_id=result.id,
                status=result.status,
                candidates=len(result.candidates),
            )
            return result

    def ingest(self, event: Mutation) -> EventResult:
        with self.lock, self.store.transaction():
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
            self._save_world()
            self.store.put("event", event.event_id, (event, result))
            if incident:
                self.incidents.append(incident)
                self._save_incident(incident)
            self._record_audit(
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
            self.stream.publish(
                "state.changed", world.version, reason="event", event_id=event.event_id
            )
            if incident:
                self.stream.publish(
                    "incident.created",
                    world.version,
                    incident_id=incident.id,
                    violations=len(incident.violations),
                )
            return result
