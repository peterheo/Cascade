"""Isolated stepwise fixture simulation; never shares state with the gateway API."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.domain.models import Mutation, Record
from cascade.execution.service import ExecutionService
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import NebiusReasoner, ReasoningError, ReasoningProvider
from cascade.reasoning.service import SemanticService
from cascade.service import CascadeService, ConflictError


class PlanRequest(Record):
    expected_version: int = Field(ge=0)
    policy: SearchPolicy = Field(default_factory=SearchPolicy)


class ConfirmRequest(Record):
    expected_version: int = Field(ge=0)


class ApprovalRequest(ConfirmRequest):
    plan_id: str


class ExecuteRequest(ConfirmRequest):
    approval_id: str


class AdvanceRequest(Record):
    expected_step: int = Field(ge=0)


def create_app(reasoning_provider: ReasoningProvider | None = None) -> FastAPI:
    app = FastAPI(
        title="Cascade", version="0.4.0", description="Deterministic recovery with Nemotron"
    )
    service = CascadeService(demo_world())
    reasoner = reasoning_provider or NebiusReasoner()
    semantic = SemanticService(service, reasoner)
    executor = ExecutionService(service)
    demo_event_id = None

    @app.get("/v1/workspace")
    def workspace():
        with service.lock:
            return {
                "state": {"world": service.world, "assessment": evaluate(service.world)},
                "incidents": service.incidents,
                "planning": service.latest_planning,
                "assisted": semantic.latest_assisted,
                "approvals": tuple(executor.approvals.values()),
                "executions": tuple(executor.executions.values()),
                "audit": service.audit,
                "reasoning": reasoner.status() if isinstance(reasoner, NebiusReasoner) else None,
            }

    @app.post("/v1/incidents/{incident_id}/approve")
    def approve(incident_id: str, request: ApprovalRequest):
        try:
            return executor.decide(incident_id, request.plan_id, request.expected_version, True)
        except KeyError as exc:
            raise HTTPException(404, "unknown plan") from exc

    @app.post("/v1/incidents/{incident_id}/reject")
    def reject(incident_id: str, request: ApprovalRequest):
        try:
            return executor.decide(incident_id, request.plan_id, request.expected_version, False)
        except KeyError as exc:
            raise HTTPException(404, "unknown plan") from exc

    @app.post("/v1/recovery-plans/{plan_id}/execute")
    def execute(plan_id: str, request: ExecuteRequest):
        try:
            return executor.start(plan_id, request.approval_id, request.expected_version)
        except KeyError as exc:
            raise HTTPException(404, "unknown plan") from exc

    @app.get("/v1/executions/{execution_id}")
    def execution(execution_id: str):
        with service.lock:
            if execution_id not in executor.executions:
                raise HTTPException(404, "unknown execution")
            return executor.executions[execution_id]

    @app.post("/v1/executions/{execution_id}/advance")
    def advance(execution_id: str, request: AdvanceRequest):
        try:
            return executor.advance(execution_id, request.expected_step)
        except KeyError as exc:
            raise HTTPException(404, "unknown execution") from exc

    @app.post("/v1/executions/{execution_id}/cancel")
    def cancel(execution_id: str):
        try:
            return executor.cancel(execution_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown execution") from exc

    @app.post("/v1/demo/reset")
    async def reset():
        nonlocal demo_event_id
        async with semantic.lock:
            with service.lock:
                if any(e.status == "RUNNING" for e in executor.executions.values()):
                    raise ConflictError("cancel active execution before resetting the demo")
                service.world = demo_world().model_copy(
                    update={"version": service.world.version + 1}
                )
                service.events.clear()
                service.incidents.clear()
                service.plans.clear()
                service.plan_incidents.clear()
                service.latest_planning = None
                service.searches.clear()
                semantic.requests.clear()
                semantic.extractions.clear()
                semantic.event_to_extraction.clear()
                semantic.latest_assisted = None
                executor.approvals.clear()
                executor.executions.clear()
                executor.bases.clear()
                demo_event_id = None
                service.audit.append({"type": "demo.reset", "world_version": service.world.version})
                return {"world": service.world, "assessment": evaluate(service.world)}

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

    @app.post("/v1/demo/scenarios/{scenario_id}/inject")
    def inject(scenario_id: str):
        nonlocal demo_event_id
        if scenario_id != "flight_delay":
            raise HTTPException(404, "unknown scenario")
        with service.lock:
            if demo_event_id in service.events:
                return service.events[demo_event_id][1]
            event = delay_event(version=service.world.version)
            result = ingest(event)
            demo_event_id = event.event_id
            return result

    return app


app = create_app()
