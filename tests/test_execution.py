from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.execution.service import ExecutionService
from cascade.service import CascadeService, ConflictError


@pytest.fixture
def ready():
    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    plan = core.plan(incident.id, 1).candidates[1]
    return core, incident, plan, ExecutionService(core)


def test_execution_requires_exact_approval(ready):
    core, incident, plan, executor = ready
    with pytest.raises(ConflictError, match="approval"):
        executor.start(plan.id, "missing", 1)
    rejected = executor.decide(incident.id, plan.id, 1, False)
    with pytest.raises(ConflictError):
        executor.start(plan.id, rejected.id, 1)
    assert core.world.version == 1


def test_approved_simulation_verifies_every_action_and_resolves(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    execution = executor.start(plan.id, approval.id, 1)
    assert executor.start(plan.id, approval.id, 1) == execution
    while execution.status == "RUNNING":
        step = execution.next_step
        execution = executor.advance(execution.id, step)
        assert executor.advance(execution.id, step) == execution
    assert execution.status == "SUCCEEDED"
    assert len(execution.outcomes) == len(plan.actions)
    assert all(o.status == "VERIFIED" for o in execution.outcomes)
    assert not evaluate(core.world).violations
    assert core.incidents[0].status == "RESOLVED"
    assert core.world.version == 5


def test_stale_plan_cannot_be_approved(ready):
    core, incident, plan, executor = ready
    core.ingest(delay_event(version=1, arrival="20:00"))
    with pytest.raises(ConflictError, match="stale"):
        executor.decide(incident.id, plan.id, 1, True)


def test_changed_approved_plan_cannot_execute(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    core.plans[plan.id] = plan.model_copy(update={"additional_cost": Decimal("999")})
    with pytest.raises(ConflictError, match="match"):
        executor.start(plan.id, approval.id, 1)


def test_partial_failure_retains_verified_changes(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    execution = executor.start(plan.id, approval.id, 1)
    executor.fail_action_ids.add(plan.actions[1].id)
    first = executor.advance(execution.id, 0)
    second = executor.advance(execution.id, 1)
    assert first.status == "RUNNING" and second.status == "FAILED"
    assert core.world.version == 2
    assert core.world.commitments[1] == plan.actions[0].replacement
    assert second.outcomes[-1].side_effect is False
    assert executor.advance(execution.id, 1) == second


def test_external_change_blocks_remaining_actions(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    execution = executor.start(plan.id, approval.id, 1)
    executor.advance(execution.id, 0)
    core.ingest(delay_event(version=2, arrival="20:00"))
    result = executor.advance(execution.id, 1)
    assert result.status == "BLOCKED" and core.world.version == 3


def test_cancel_stops_future_effects(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    execution = executor.start(plan.id, approval.id, 1)
    executor.advance(execution.id, 0)
    cancelled = executor.cancel(execution.id)
    assert cancelled.status == "CANCELLED"
    assert executor.advance(execution.id, 1) == cancelled
    assert core.world.version == 2


def test_missing_provider_blocks_start_without_side_effects(ready):
    core, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    executor.providers.pop("hotel")
    with pytest.raises(ConflictError, match="unavailable"):
        executor.start(plan.id, approval.id, 1)
    assert core.world.version == 1


def test_out_of_order_step_rejected(ready):
    _, incident, plan, executor = ready
    approval = executor.decide(incident.id, plan.id, 1, True)
    execution = executor.start(plan.id, approval.id, 1)
    with pytest.raises(ConflictError, match="order"):
        executor.advance(execution.id, 2)


def test_api_approval_execution_and_monotonic_reset():
    with TestClient(create_app()) as client:
        result = client.post("/v1/demo/scenarios/flight_delay/inject").json()
        incident = result["incident"]["id"]
        plan = client.post(f"/v1/incidents/{incident}/plan", json={"expected_version": 1}).json()[
            "candidates"
        ][0]
        approval = client.post(
            f"/v1/incidents/{incident}/approve", json={"expected_version": 1, "plan_id": plan["id"]}
        ).json()
        execution = client.post(
            f"/v1/recovery-plans/{plan['id']}/execute",
            json={"expected_version": 1, "approval_id": approval["id"]},
        ).json()
        assert client.post("/v1/demo/reset").status_code == 409
        while execution["status"] == "RUNNING":
            execution = client.post(
                f"/v1/executions/{execution['id']}/advance",
                json={"expected_step": execution["next_step"]},
            ).json()
        assert execution["status"] == "SUCCEEDED"
        assert client.get("/v1/workspace").json()["incidents"][0]["status"] == "RESOLVED"
        version = client.post("/v1/demo/reset").json()["world"]["version"]
        assert version > execution["expected_world_version"]
        assert client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 200
