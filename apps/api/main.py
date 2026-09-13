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
from cascade.service import CascadeService, ConflictError


class PlanRequest(Record):
    expected_version: int = Field(ge=0)
    policy: SearchPolicy = Field(default_factory=SearchPolicy)


class ConfirmRequest(Record):
    expected_version: int = Field(ge=0)


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

    @app.post("/v1/demo/scenarios/{scenario_id}/inject")
    def inject(scenario_id: str):
        if scenario_id != "flight_delay":
            raise HTTPException(404, "unknown scenario")
        # Fixed event identity makes repeated clicks idempotent.
        return ingest(delay_event())

    return app


app = create_app()
