import asyncio
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from apps.api.preferences import register_preference_routes
from apps.api.privacy import register_privacy_routes
from cascade.connectors.mail import ImapMailSource
from cascade.connectors.mail_watcher import MailWatcher
from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.domain.models import Mutation, Record
from cascade.persistence import MemoryStore, SqliteStore
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import NebiusReasoner, ReasoningError, ReasoningProvider
from cascade.reasoning.service import SemanticService
from cascade.security.approvals import ApprovalError
from cascade.security.auth import AuthManager
from cascade.service import CascadeService, ConflictError
from cascade.tools.demo import demo_gateway

STATIC = Path(__file__).parent / "static"


class PlanRequest(Record):
    expected_version: int = Field(ge=0)
    # None means "use the matched skill's limits"; an explicit policy overrides them.
    policy: SearchPolicy | None = None
    skill: str | None = None


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
    auth = AuthManager("gateway")
    app = FastAPI(
        title="Cascade",
        version="0.4.0",
        description="Deterministic recovery with Nemotron",
        dependencies=[Depends(auth.dependency)],
    )
    db_path = os.environ.get("CASCADE_DB")
    ledger_path = os.environ.get("CASCADE_LEDGER_DB")
    store = SqliteStore(Path(db_path)) if db_path else MemoryStore()
    gateway = demo_gateway(ledger_path=Path(ledger_path)) if ledger_path else demo_gateway()
    service = CascadeService(demo_world(), gateway=gateway, store=store)
    reasoner = reasoning_provider or NebiusReasoner()
    semantic = SemanticService(service, reasoner)
    mail_watcher = None
    mail_user = os.environ.get("CASCADE_ICLOUD_USER")
    mail_password = os.environ.get("CASCADE_ICLOUD_APP_PASSWORD")
    if mail_user and mail_password:
        folder = os.environ.get("CASCADE_ICLOUD_MAIL_FOLDER", "Cascade")
        try:
            configured_poll = int(os.environ.get("CASCADE_MAIL_POLL_SECONDS", "90"))
        except ValueError:
            configured_poll = 90
        poll_seconds = min(3600, max(60, configured_poll))
        mail_watcher = MailWatcher(
            ImapMailSource(mail_user, mail_password, folder=folder),
            semantic,
            store,
            folder=folder,
            poll_seconds=poll_seconds,
        )
    app.state.mail_watcher = mail_watcher

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if mail_watcher is not None:

            async def poll_loop():
                while True:
                    await mail_watcher.poll_once()
                    await asyncio.sleep(max(1.0, mail_watcher.seconds_until_next_poll()))

            task = asyncio.create_task(poll_loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app.router.lifespan_context = lifespan
    # Exposed for tests and for anything that has the app but not the closure.
    app.state.service = service
    app.state.auth = auth
    auth.register_routes(app)
    register_preference_routes(app, service)
    register_privacy_routes(app, semantic)

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
            status = reasoner.status()
        else:
            status = {
                "provider": "injected_test_provider",
                "configured": True,
                "live_verified": False,
            }
        return {**status, "live_inference": semantic.privacy.live_inference}

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
                incident_id,
                request.expected_version,
                request.policy or SearchPolicy(),
                request.skill,
            )
        except KeyError as exc:
            raise HTTPException(404, "unknown incident") from exc
        except ConflictError:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        """The product surface is served by the API itself; there is no build step."""
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "storage": "sqlite" if db_path else "in_memory",
            "mode": "demo",
        }

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

    @app.get("/v1/stream")
    async def stream(request: Request):
        """Server-sent notifications. They say what changed; state still comes from /v1."""
        queue = service.stream.subscribe()
        if queue is None:
            raise HTTPException(503, "too many stream subscribers")

        async def notifications():
            try:
                while not await request.is_disconnected():
                    while queue:
                        yield queue.popleft().encode()
                    # A heartbeat keeps proxies from closing an idle stream silently.
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                service.stream.unsubscribe(queue)

        return StreamingResponse(
            notifications(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/audit")
    def audit():
        with service.lock:
            return tuple(service.audit)

    @app.get("/v1/ledger/orphans")
    def ledger_orphans():
        return service.ledger_orphan_entries()

    @app.get("/v1/connectors/mail/status")
    def mail_status():
        if mail_watcher is None:
            return {
                "enabled": False,
                "folder": os.environ.get("CASCADE_ICLOUD_MAIL_FOLDER", "Cascade"),
                "last_poll_at": None,
                "last_error_class": None,
                "processed_count": 0,
                "recent": [],
            }
        return mail_watcher.status()

    @app.post("/v1/incidents/{incident_id}/plan")
    @app.post("/v1/incidents/{incident_id}/replan")
    def plan(incident_id: str, request: PlanRequest):
        try:
            return service.plan(
                incident_id,
                request.expected_version,
                request.policy,
                skill_name=request.skill,
            )
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

    @app.get("/v1/skills")
    def skills():
        """Versioned recovery templates. They bound the planner; they are not prompts."""
        return service.skills

    @app.get("/v1/memory/resolutions")
    def resolutions():
        with service.lock:
            return tuple(service.resolutions)

    @app.get("/v1/memory/suggestions")
    def suggestions():
        """Derived from past choices. Promote one explicitly to make it policy."""
        return service.suggestions()

    @app.get("/v1/permissions")
    def permissions():
        return service.permissions

    @app.post("/v1/demo/scenarios/{scenario_id}/inject")
    def inject(scenario_id: str):
        if scenario_id != "flight_delay":
            raise HTTPException(404, "unknown scenario")
        # Fixed event identity makes repeated clicks idempotent.
        return ingest(delay_event())

    from apps.api.simulation import create_app as create_simulation_app

    app.mount("/simulation", create_simulation_app(reasoning_provider))
    return app


app = create_app()
