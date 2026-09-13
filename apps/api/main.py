from decimal import Decimal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.domain.models import Mutation, Record
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import NebiusReasoner, ReasoningError, ReasoningProvider
from cascade.reasoning.service import SemanticService
from cascade.security.approvals import ApprovalError
from cascade.service import CascadeService, ConflictError


class PlanRequest(Record):
    expected_version: int = Field(ge=0)
    policy: SearchPolicy = Field(default_factory=SearchPolicy)


class ConfirmRequest(Record):
    expected_version: int = Field(ge=0)


class ExecuteRequest(Record):
    expected_version: int = Field(ge=0)
    actor: str = Field(default="user", min_length=1, max_length=100)


class ApproveRequest(Record):
    """Informed consent: the caller echoes the exact actions and the exact total."""

    actor: str = Field(default="user", min_length=1, max_length=100)
    approved_action_ids: tuple[str, ...] = Field(min_length=1)
    acknowledged_amount: Decimal = Field(ge=0)


class RejectRequest(Record):
    actor: str = Field(default="user", min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)


class IncidentApproveRequest(Record):
    plan_id: str
    expected_version: int = Field(ge=0)
    acknowledged_amount: Decimal = Field(ge=0)
    actor: str = Field(default="user", min_length=1, max_length=100)


def create_app(reasoning_provider: ReasoningProvider | None = None) -> FastAPI:
    app = FastAPI(
        title="Cascade", version="0.3.0", description="Deterministic recovery with Nemotron"
    )
    service = CascadeService(demo_world())
    reasoner = reasoning_provider or NebiusReasoner()
    semantic = SemanticService(service, reasoner)

    @app.exception_handler(ReasoningError)
    async def reasoning_error(request, exc):
        status = 503 if exc.code in ("not_configured", "unavailable", "timeout") else 502
        return JSONResponse(status_code=status, content={"code": exc.code, "detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_error(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/v1/reasoning/status")
    def reasoning_status():
        if isinstance(reasoner, NebiusReasoner):
            return reasoner.status()
        return {"provider": "injected_test_provider", "configured": True, "live_verified": False}

    @app.post("/v1/events/text")
    async def natural_event(request: NaturalEventRequest):
        return await semantic.extract(request)

    @app.get("/v1/extractions/{extraction_id}")
    def extraction(extraction_id: str):
        with service.lock:
            if extraction_id not in semantic.extractions:
                raise HTTPException(404, "unknown extraction")
            result = semantic.extractions[extraction_id]
            version = (
                result.event_result.world_version
                if result.event_result
                else result.based_on_version
            )
            return {"extraction": result, "stale": version != service.world.version}

    @app.post("/v1/extractions/{extraction_id}/confirm")
    async def confirm(extraction_id: str, request: ConfirmRequest):
        try:
            return await semantic.confirm(extraction_id, request.expected_version)
        except KeyError as exc:
            raise HTTPException(404, "unknown extraction") from exc

    @app.post("/v1/incidents/{incident_id}/plan/assisted")
    async def assisted_plan(incident_id: str, request: PlanRequest):
        try:
            return await semantic.assisted_plan(
                incident_id, request.expected_version, request.policy
            )
        except KeyError as exc:
            raise HTTPException(404, "unknown incident") from exc
        except ConflictError:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/health")
    def health():
        return {"status": "ok", "storage": "in_memory", "mode": "demo"}

    @app.get("/v1/state")
    def state():
        with service.lock:
            return {"world": service.world, "assessment": evaluate(service.world)}

    @app.get("/v1/commitments")
    def commitments():
        return service.world.commitments

    @app.post("/v1/events")
    def ingest(event: Mutation):
        try:
            return service.ingest(event)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown commitment") from exc
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/incidents")
    def incidents():
        with service.lock:
            return tuple(service.incidents)

    @app.get("/v1/incidents/{incident_id}")
    def incident(incident_id: str):
        with service.lock:
            for item in service.incidents:
                if item.id == incident_id:
                    return item
        raise HTTPException(404, "unknown incident")

    @app.get("/v1/audit")
    def audit():
        with service.lock:
            return tuple(service.audit)

    @app.post("/v1/incidents/{incident_id}/plan")
    @app.post("/v1/incidents/{incident_id}/replan")
    def plan(incident_id: str, request: PlanRequest):
        try:
            return service.plan(incident_id, request.expected_version, request.policy)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown incident") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/recovery-plans/{plan_id}")
    def recovery_plan(plan_id: str):
        with service.lock:
            if plan_id not in service.plans:
                raise HTTPException(404, "unknown recovery plan")
            candidate = service.plans[plan_id]
            return {"plan": candidate, "stale": candidate.based_on_version != service.world.version}

    @app.post("/v1/recovery-plans/{plan_id}/execute")
    def execute(plan_id: str, request: ExecuteRequest):
        try:
            return service.execute(plan_id, request.expected_version, request.actor)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown recovery plan") from exc

    @app.get("/v1/approvals")
    def approvals():
        with service.lock:
            return tuple(service.approvals.values())

    @app.get("/v1/approvals/{request_id}")
    def approval(request_id: str):
        with service.lock:
            if request_id not in service.approvals:
                raise HTTPException(404, "unknown approval request")
            current = service.approvals[request_id]
            return {
                "approval": current,
                "stale": current.based_on_version != service.world.version,
            }

    @app.post("/v1/approvals/{request_id}/approve")
    def approve(request_id: str, request: ApproveRequest):
        try:
            return service.approve(
                request_id,
                request.actor,
                request.approved_action_ids,
                request.acknowledged_amount,
            )
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown approval request") from exc
        except ApprovalError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/approvals/{request_id}/reject")
    def reject_approval(request_id: str, request: RejectRequest):
        try:
            return service.reject(request_id, request.actor, request.note)
        except KeyError as exc:
            raise HTTPException(404, "unknown approval request") from exc
        except ApprovalError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/executions")
    def executions():
        with service.lock:
            return tuple(service.executions.values())

    @app.get("/v1/executions/{execution_id}")
    def execution(execution_id: str):
        with service.lock:
            if execution_id not in service.executions:
                raise HTTPException(404, "unknown execution")
            return service.executions[execution_id]

    @app.post("/v1/incidents/{incident_id}/approve")
    def approve_incident(incident_id: str, request: IncidentApproveRequest):
        """Choose a plan, consent to its exact cost, and run it in one informed step."""
        with service.lock:
            if request.plan_id not in service.plan_incidents.get(incident_id, ()):
                raise HTTPException(404, "that plan does not belong to this incident")
        proposed = ExecuteRequest(expected_version=request.expected_version, actor=request.actor)
        first = execute(request.plan_id, proposed)
        if first.status != "AWAITING_APPROVAL":
            return first
        approve(
            first.approval_request.id,
            ApproveRequest(
                actor=request.actor,
                approved_action_ids=tuple(i.action_id for i in first.approval_request.items),
                acknowledged_amount=request.acknowledged_amount,
            ),
        )
        return execute(
            request.plan_id,
            ExecuteRequest(expected_version=request.expected_version, actor=request.actor),
        )

    @app.post("/v1/incidents/{incident_id}/reject")
    def reject_incident(incident_id: str, request: RejectRequest):
        try:
            return service.dismiss(incident_id, request.actor, request.note)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "unknown incident") from exc

    @app.get("/v1/security/sandbox")
    def sandbox():
        """Denied actions stay visible; the boundary is evidence, not decoration."""
        boundary = service.gateway.sandbox
        if boundary is None:
            return {"enforced": False, "policy": None, "denials": []}
        return {"enforced": True, "policy": boundary.policy, "denials": boundary.denials}

    @app.get("/v1/permissions")
    def permissions():
        return service.permissions

    @app.post("/v1/demo/scenarios/{scenario_id}/inject")
    def inject(scenario_id: str):
        if scenario_id != "flight_delay":
            raise HTTPException(404, "unknown scenario")
        # Fixed event identity makes repeated clicks idempotent.
        return ingest(delay_event())

    return app


app = create_app()
