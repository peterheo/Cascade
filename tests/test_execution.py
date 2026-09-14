from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.execution.executor import PlanExecutor
from cascade.planning.demo import demo_planner
from cascade.planning.models import SearchPolicy
from cascade.planning.severity import classify
from cascade.security.approvals import ApprovalError
from cascade.security.openshell import PolicySandbox, SandboxPolicy
from cascade.service import CascadeService, ConflictError
from cascade.tools.demo import demo_gateway
from cascade.tools.gateway import (
    ExecutionContext,
    PostCondition,
    ToolAction,
    ToolGateway,
    build_action,
)
from cascade.tools.permissions import PermissionPolicy, risk_tier


@pytest.fixture
def disrupted():
    gateway = demo_gateway()
    service = CascadeService(demo_world(), gateway)
    incident = service.ingest(delay_event()).incident
    result = service.plan(incident.id, service.world.version)
    plan = min(result.candidates, key=lambda p: p.additional_cost)
    return service, incident, plan, gateway


def approve_all(service, request):
    return service.approve(
        request.id,
        "user",
        tuple(item.action_id for item in request.items),
        request.total_amount,
    )


def test_nothing_executes_before_approval(disrupted):
    service, incident, plan, gateway = disrupted
    before = service.world
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "AWAITING_APPROVAL"
    assert result.side_effects == 0
    assert service.world == before
    assert all(s.status == "REQUIRES_APPROVAL" for s in result.steps)
    assert all(not p.ledger for p in gateway.providers.values())
    request = result.approval_request
    assert request.total_amount == plan.additional_cost
    assert {i.risk_tier for i in request.items} == {3, 4}
    assert service.incident(incident.id).status == "OPEN"


def test_approved_plan_executes_verifies_and_resolves(disrupted):
    service, incident, plan, gateway = disrupted
    pending = service.execute(plan.id, service.world.version, "user")
    approved = approve_all(service, pending.approval_request)
    assert approved.status == "APPROVED"
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "COMPLETED"
    assert result.verified and not result.replan_required
    assert result.side_effects == len([a for a in plan.actions if a.resolution != "PRESERVED"])
    assert [s.status for s in result.steps] == ["EXECUTED"] * len(plan.actions)
    # Every write was read back from the provider before the step was accepted.
    assert all(s.call.result.verified and s.call.result.external_reference for s in result.steps)
    assert evaluate(service.world).violations == ()
    assert result.remaining_violations == ()
    assert service.incident(incident.id).status == "RESOLVED"
    assert service.world.version == 1 + result.side_effects
    types = [entry.get("type") for entry in service.audit]
    assert types[-1] == "recovery.executed"
    assert "approval.granted" in types


def test_execution_is_idempotent_against_a_replayed_plan(disrupted):
    service, _, plan, gateway = disrupted
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    first = service.execute(plan.id, service.world.version, "user")
    assert first.status == "COMPLETED"
    version = service.world.version
    with pytest.raises(ConflictError):
        service.execute(plan.id, plan.based_on_version, "user")
    replay = service.execute(plan.id, version, "user")
    assert replay.status == "STALE"
    assert replay.side_effects == 0
    assert service.world.version == version


def test_approval_must_be_informed(disrupted):
    service, _, plan, _ = disrupted
    request = service.execute(plan.id, service.world.version, "user").approval_request
    partial = (request.items[0].action_id,)
    with pytest.raises(ApprovalError, match="every requested action"):
        service.approve(request.id, "user", partial, request.total_amount)
    everything = tuple(i.action_id for i in request.items)
    with pytest.raises(ApprovalError, match="acknowledged amount"):
        service.approve(request.id, "user", everything, request.total_amount - 1)
    approve_all(service, request)
    with pytest.raises(ApprovalError, match="already decided"):
        approve_all(service, service.approvals[request.id])


def test_rejected_approval_leaves_the_world_untouched(disrupted):
    service, incident, plan, gateway = disrupted
    request = service.execute(plan.id, service.world.version, "user").approval_request
    service.reject(request.id, "user", "Prefer to keep the evening free.")
    again = service.execute(plan.id, service.world.version, "user")
    assert again.status == "AWAITING_APPROVAL"
    assert service.world.version == 1
    assert all(not p.ledger for p in gateway.providers.values())
    dismissed = service.dismiss(incident.id, "user", "Handled offline.")
    assert dismissed.status == "DISMISSED"
    with pytest.raises(ConflictError):
        service.dismiss(incident.id, "user", "again")


def test_policy_denial_blocks_spending_entirely(disrupted):
    service, _, plan, gateway = disrupted
    # Tier 4 above the approvable ceiling: no approval can unlock it.
    service.permissions = PermissionPolicy(approval_max_tier=3)
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "DENIED"
    assert result.side_effects == 0
    assert result.approval_request is None
    denied = [s for s in result.steps if s.status == "DENIED"]
    assert denied and all("above the approvable maximum" in s.note for s in denied)
    assert all(not p.ledger for p in gateway.providers.values())


def test_sandbox_denies_then_the_same_action_succeeds_after_a_grant(disrupted):
    service, _, plan, gateway = disrupted
    sandbox = PolicySandbox(SandboxPolicy(name="locked-down", allowed_providers=("mock_transfer",)))
    gateway.sandbox = sandbox
    blocked = service.execute(plan.id, service.world.version, "user")
    # The boundary refuses before the user is ever asked to approve.
    assert blocked.status == "DENIED"
    assert blocked.side_effects == 0
    assert blocked.approval_request is None
    assert [d.provider for d in sandbox.denials] == ["mock_hotel", "mock_restaurant", "mock_ticket"]
    assert all("outside the sandbox profile" in d.reason for d in sandbox.denials)
    for provider in ("mock_hotel", "mock_restaurant", "mock_ticket"):
        sandbox.grant_provider(provider)
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    allowed = service.execute(plan.id, service.world.version, "user")
    assert allowed.status == "COMPLETED"
    assert len(sandbox.denials) == 3


def test_sandbox_spend_ceiling_is_independent_of_user_approval(disrupted):
    service, _, plan, gateway = disrupted
    gateway.sandbox = PolicySandbox(
        SandboxPolicy(
            allowed_providers=tuple(gateway.providers),
            max_amount=Decimal("100"),
        )
    )
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "DENIED"
    assert any("spend ceiling" in s.note for s in result.steps)
    assert all(not p.ledger for p in gateway.providers.values())


def test_withdrawn_inventory_is_caught_before_any_write(disrupted):
    service, incident, plan, gateway = disrupted
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    gateway.providers["mock_transfer"].withdrawn.add("taxi")
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "BLOCKED"
    assert result.side_effects == 0
    assert result.replan_required
    assert result.steps[0].status == "PRECHECK_FAILED"
    assert "no longer available" in result.steps[0].note
    assert service.world.version == 1
    assert service.incident(incident.id).status == "OPEN"


def test_partial_execution_commits_what_landed_and_demands_a_replan(disrupted):
    service, incident, plan, gateway = disrupted
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    gateway.providers["mock_restaurant"].failing.add("dinner_waiver")
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "PARTIAL"
    assert result.side_effects == 2
    assert result.replan_required and not result.verified
    statuses = [s.status for s in result.steps]
    assert statuses == ["EXECUTED", "EXECUTED", "FAILED", "NOT_ATTEMPTED"]
    # The two confirmed writes are authoritative state now, not a rollback candidate.
    assert service.world.version == 3
    assert service.world.commitments[1].title.startswith("Taxi")
    assert service.incident(incident.id).status == "OPEN"
    assert any("replan" in note.lower() for note in result.notes)


def test_replanning_after_partial_execution_uses_the_actual_state(disrupted):
    service, incident, plan, gateway = disrupted
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    gateway.providers["mock_restaurant"].failing.add("dinner_waiver")
    service.execute(plan.id, service.world.version, "user")
    again = service.plan(incident.id, service.world.version)
    assert again.based_on_version == service.world.version
    assert again.candidates
    assert all(p.based_on_version == service.world.version for p in again.candidates)
    assert all(evaluate(p.world).violations == () for p in again.candidates)


def test_unverifiable_write_is_not_success(disrupted):
    service, _, plan, gateway = disrupted

    class Amnesiac:
        """Confirms the write but has no record of it afterwards."""

        def __init__(self, inner):
            self.inner = inner
            self.ledger = {}

        def check(self, action):
            return self.inner.check(action)

        def apply(self, action):
            return self.inner.apply(action).model_copy(update={"external_reference": "ghost"})

        def verify(self, action):
            return self.inner.verify(action).model_copy(
                update={"observed_start_at": None, "observed_end_at": None}
            )

    gateway.providers["mock_transfer"] = Amnesiac(gateway.providers["mock_transfer"])
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "PARTIAL"
    assert result.steps[0].status == "FAILED"
    assert "Postcondition unverified" in result.steps[0].call.result.detail
    assert result.steps[0].call.result.side_effect
    assert not result.steps[0].call.result.verified
    # The side effect happened but the world never adopted an unverified change.
    assert service.world.version == 1


def test_write_that_raises_after_landing_is_verified_without_a_second_booking(disrupted):
    service, _, plan, gateway = disrupted
    provider = gateway.providers["mock_transfer"]
    original_apply = provider.apply

    def apply_then_raise(action):
        original_apply(action)
        raise TimeoutError("vendor write timed out after confirmation")

    provider.apply = apply_then_raise
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    result = service.execute(plan.id, service.world.version, "user")

    assert result.status == "COMPLETED"
    assert result.steps[0].status == "EXECUTED"
    assert result.steps[0].call.result.success
    assert result.steps[0].call.result.verified
    assert result.steps[0].call.result.side_effect
    assert len(provider.ledger) == 1


def test_write_that_raises_before_landing_is_known_to_have_no_side_effect(disrupted):
    service, _, plan, gateway = disrupted
    provider = gateway.providers["mock_transfer"]

    def apply_times_out(action):
        raise TimeoutError("vendor write timed out before confirmation")

    provider.apply = apply_times_out
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    result = service.execute(plan.id, service.world.version, "user")

    assert result.steps[0].status == "FAILED"
    assert result.steps[0].call.result.success is False
    assert result.steps[0].call.result.side_effect is False
    assert "No provider record exists" in result.steps[0].call.result.detail
    assert not provider.ledger


def test_write_and_readback_that_raise_require_reconciliation(disrupted):
    service, _, plan, gateway = disrupted
    provider = gateway.providers["mock_transfer"]

    def apply_times_out(action):
        raise TimeoutError("vendor write timed out")

    def verify_times_out(action):
        raise TimeoutError("provider read timed out")

    provider.apply = apply_times_out
    provider.verify = verify_times_out
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    result = service.execute(plan.id, service.world.version, "user")

    assert result.steps[0].status == "FAILED"
    assert result.steps[0].call.result.success is False
    assert result.steps[0].call.result.side_effect
    assert "Read-back also failed" in result.steps[0].call.result.detail
    assert any("reconcile with the provider" in note for note in result.notes)


def test_adapter_failure_is_unknown_state_not_success(disrupted):
    service, _, plan, gateway = disrupted

    class Broken:
        def check(self, action):
            raise TimeoutError("provider unreachable")

        def apply(self, action):
            raise TimeoutError("provider unreachable")

        def verify(self, action):
            raise TimeoutError("provider unreachable")

    gateway.providers["mock_transfer"] = Broken()
    approve_all(service, service.execute(plan.id, service.world.version, "user").approval_request)
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "BLOCKED"
    assert result.side_effects == 0
    assert "TimeoutError" in result.steps[0].precheck.result.detail
    assert "unknown" in result.steps[0].precheck.result.detail


def test_idempotency_key_prevents_a_double_booking(disrupted):
    service, _, plan, gateway = disrupted
    executor = PlanExecutor(gateway)
    context = ExecutionContext(plan_id=plan.id, world_version=1, actor="test")
    option, action = next((o, a) for o, a in executor.actions(service.world, plan) if a)
    provider = gateway.providers[action.provider]
    first = provider.apply(action)
    second = provider.apply(action)
    assert first.side_effect and not second.side_effect
    assert first.external_reference == second.external_reference
    assert len(provider.ledger) == 1
    assert context.approved_action_ids == ()


def test_risk_tier_and_mode_cannot_be_forged():
    postcondition = PostCondition(commitment_present=False)
    assert risk_tier("book", Decimal("1")) == 4
    assert risk_tier("book", Decimal("0")) == 3
    assert risk_tier("check_availability", Decimal("0")) == 0
    with pytest.raises(ValidationError, match="cannot be lowered"):
        ToolAction(
            id="a",
            plan_id="p",
            commitment_id="hotel",
            provider="mock_hotel",
            operation="book",
            mode="WRITE",
            risk_tier=1,
            amount=Decimal("140"),
            idempotency_key="k",
            postcondition=postcondition,
            description="forged tier",
        )
    with pytest.raises(ValidationError, match="derived from the operation"):
        ToolAction(
            id="a",
            plan_id="p",
            commitment_id="hotel",
            provider="mock_hotel",
            operation="book",
            mode="READ",
            risk_tier=3,
            idempotency_key="k",
            postcondition=postcondition,
            description="write disguised as a read",
        )


def test_gateway_separates_queries_from_mutations():
    gateway = demo_gateway()
    context = ExecutionContext(plan_id="p", world_version=0, actor="test")
    write = build_action(
        plan_id="p",
        commitment_id="hotel",
        provider="mock_hotel",
        operation="cancel",
        idempotency_key="k",
        postcondition=PostCondition(commitment_present=False),
        description="cancel",
    )
    read = build_action(
        plan_id="p",
        commitment_id="hotel",
        provider="mock_hotel",
        operation="check_availability",
        idempotency_key="k",
        postcondition=PostCondition(commitment_present=False),
        description="check",
    )
    with pytest.raises(ValueError, match="read operations only"):
        gateway.query(write, context)
    with pytest.raises(ValueError, match="write operations only"):
        gateway.execute(read, context)
    assert gateway.query(read, context).result is not None


def test_unknown_provider_is_reported_not_assumed(disrupted):
    service, _, plan, _ = disrupted
    service.gateway = ToolGateway({})
    service.executor = PlanExecutor(service.gateway)
    result = service.execute(plan.id, service.world.version, "user")
    assert result.status == "DENIED"
    assert all("No adapter registered" in s.note for s in result.steps if s.status == "DENIED")


def test_severity_reflects_what_search_found(disrupted):
    service, incident, _, _ = disrupted
    # No feasible plan keeps every intent alive, so the user owns the tradeoff.
    assert service.incident(incident.id).severity == "RED"
    unaffordable = service.plan(
        incident.id, service.world.version, SearchPolicy(max_additional_cost=Decimal("0"))
    )
    assert classify(unaffordable) == "BLACK"
    assert service.incident(incident.id).severity == "BLACK"
    healthy = demo_planner().plan(demo_world(), incident)
    assert classify(healthy) == "GREEN"
    degraded = healthy.model_copy(
        update={
            "candidates": tuple(
                p.model_copy(update={"additional_cost": Decimal("40")}) for p in healthy.candidates
            )
        }
    )
    assert classify(degraded) == "YELLOW"


def test_severity_is_withheld_until_search_is_exhaustive(disrupted):
    service, incident, _, _ = disrupted
    complete = service.plan(incident.id, service.world.version)

    for status in ("BLOCKED", "BUDGET_EXHAUSTED"):
        assert classify(complete.model_copy(update={"status": status, "candidates": ()})) is None
    for status in ("NO_FEASIBLE_PLAN", "COMPLETE"):
        assert classify(complete.model_copy(update={"status": status, "candidates": ()})) == "BLACK"

    non_full = complete.candidates[0].model_copy(
        update={"intent_quality": {key: 0 for key in complete.candidates[0].intent_quality}}
    )
    partial_non_full = complete.model_copy(update={"status": "PARTIAL", "candidates": (non_full,)})
    complete_non_full = complete.model_copy(
        update={"status": "COMPLETE", "candidates": (non_full,)}
    )
    assert classify(partial_non_full) is None
    assert classify(complete_non_full) == "RED"

    healthy = demo_planner().plan(demo_world(), incident)
    assert classify(healthy.model_copy(update={"status": "PARTIAL"})) == "GREEN"
    degraded = healthy.candidates[0].model_copy(update={"additional_cost": Decimal("40")})
    assert (
        classify(healthy.model_copy(update={"status": "PARTIAL", "candidates": (degraded,)}))
        == "YELLOW"
    )


def test_api_approval_and_execution_flow():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        planned = client.post(
            f"/v1/incidents/{incident['id']}/plan", json={"expected_version": 1}
        ).json()
        plan = min(planned["candidates"], key=lambda p: Decimal(p["additional_cost"]))
        assert client.get(f"/v1/incidents/{incident['id']}").json()["severity"] == "RED"
        pending = client.post(
            f"/v1/recovery-plans/{plan['id']}/execute", json={"expected_version": 1}
        ).json()
        assert pending["status"] == "AWAITING_APPROVAL"
        assert client.get("/v1/state").json()["world"]["version"] == 1
        request = pending["approval_request"]
        assert client.get(f"/v1/approvals/{request['id']}").json()["stale"] is False
        body = {
            "approved_action_ids": [i["action_id"] for i in request["items"]],
            "acknowledged_amount": request["total_amount"],
        }
        uninformed = {**body, "acknowledged_amount": "1"}
        path = f"/v1/approvals/{request['id']}/approve"
        assert client.post(path, json=uninformed).status_code == 422
        assert client.post(path, json=body).status_code == 200
        executed = client.post(
            f"/v1/recovery-plans/{plan['id']}/execute", json={"expected_version": 1}
        ).json()
        assert executed["status"] == "COMPLETED"
        assert executed["remaining_violations"] == []
        assert client.get(f"/v1/executions/{executed['id']}").json()["verified"] is True
        assert client.get(f"/v1/incidents/{incident['id']}").json()["status"] == "RESOLVED"
        assert client.get("/v1/state").json()["assessment"]["violations"] == []
        assert client.get("/v1/security/sandbox").json()["enforced"] is True
        assert client.get("/v1/permissions").json()["auto_max_tier"] == 2
        assert (
            client.post(
                "/v1/recovery-plans/missing/execute", json={"expected_version": 5}
            ).status_code
            == 404
        )
        assert client.get("/v1/approvals/missing").status_code == 404


def test_api_incident_approval_requires_the_exact_cost():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        planned = client.post(
            f"/v1/incidents/{incident['id']}/plan", json={"expected_version": 1}
        ).json()
        plan = max(planned["candidates"], key=lambda p: Decimal(p["additional_cost"]))
        path = f"/v1/incidents/{incident['id']}/approve"
        wrong = client.post(
            path,
            json={"plan_id": plan["id"], "expected_version": 1, "acknowledged_amount": "1"},
        )
        assert wrong.status_code == 422
        assert client.get("/v1/state").json()["world"]["version"] == 1
        assert (
            client.post(
                path,
                json={"plan_id": "plan_missing", "expected_version": 1, "acknowledged_amount": "1"},
            ).status_code
            == 404
        )
        result = client.post(
            path,
            json={
                "plan_id": plan["id"],
                "expected_version": 1,
                "acknowledged_amount": plan["additional_cost"],
            },
        ).json()
        assert result["status"] == "COMPLETED"
        assert client.get(f"/v1/incidents/{incident['id']}").json()["status"] == "RESOLVED"


def test_api_incident_rejection_closes_it_without_side_effects():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        response = client.post(
            f"/v1/incidents/{incident['id']}/reject", json={"note": "Travelling tomorrow instead."}
        )
        assert response.json()["status"] == "DISMISSED"
        assert client.get("/v1/state").json()["world"]["version"] == 1
        assert (
            client.post(
                f"/v1/incidents/{incident['id']}/reject", json={"note": "again"}
            ).status_code
            == 409
        )
