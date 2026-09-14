from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from cascade.constraints.engine import evaluate
from cascade.demo import at, delay_event, demo_world
from cascade.domain.models import Commitment, Dependency, Intent, SourceRef
from cascade.planning.demo import demo_planner
from cascade.planning.models import ProviderResult, RecoveryOption, SearchPolicy
from cascade.planning.operators import apply_option
from cascade.planning.planner import dominates
from cascade.service import CascadeService


@pytest.fixture
def disrupted():
    service = CascadeService(demo_world())
    event = service.ingest(delay_event())
    return service, event.incident


def test_primary_scenario_produces_global_pareto_tradeoffs(disrupted):
    service, incident = disrupted
    before = service.world
    result = demo_planner().plan(before, incident)
    assert result.status == "COMPLETE"
    assert len(result.candidates) == 5
    assert result.tool_calls == 4
    assert result.expansions == 17
    assert service.world == before
    assert all(r.provider.startswith("mock_") for r in result.provider_results)
    assert all("RESCHEDULE" in r.exhausted for r in result.provider_results)
    profiles = [p.intent_quality for p in result.candidates]
    assert any(p["intent_restaurant"] == 1 and p["intent_ticket"] == 0 for p in profiles)
    assert any(p["intent_restaurant"] == 0.55 and p["intent_ticket"] == 1 for p in profiles)
    for plan in result.candidates:
        assert evaluate(plan.world).violations == ()
        assert plan.intent_quality["intent_hotel"] == plan.intent_quality["intent_transfer"] == 1
        assert plan.status == "AWAITING_APPROVAL"
        assert not any(dominates(other, plan) for other in result.candidates)
        assert plan.additional_cost == sum(a.additional_cost for a in plan.actions)
    assert result.rejections["hard_constraint"] > 0


@pytest.mark.parametrize("budget", [0, 100, 204, 205, 219, 220, 229, 230, 244, 245, 299, 300])
def test_spending_limits_and_exhaustion(disrupted, budget):
    service, incident = disrupted
    result = demo_planner().plan(service.world, incident, SearchPolicy(max_additional_cost=budget))
    assert all(p.additional_cost <= budget for p in result.candidates)
    assert (result.status == "NO_FEASIBLE_PLAN") == (budget < 205)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_tool_calls": 0},
        {"max_depth": 1},
        {"max_expansions": 1},
        {"max_seconds": 0.000000001},
    ],
)
def test_search_budget_is_not_exhaustion(disrupted, limits):
    service, incident = disrupted
    policy = SearchPolicy(**limits)
    result = demo_planner().plan(service.world, incident, policy)
    assert result.status == "BUDGET_EXHAUSTED"
    assert result.candidates == ()
    assert result.tool_calls <= policy.max_tool_calls
    assert result.expansions <= policy.max_expansions


@pytest.mark.parametrize("limits", [{"max_candidates": 1}, {"max_options_per_commitment": 1}])
def test_truncated_frontier_does_not_claim_complete_search(disrupted, limits):
    service, incident = disrupted
    result = demo_planner().plan(service.world, incident, SearchPolicy(**limits))
    assert result.status in ("PARTIAL", "BUDGET_EXHAUSTED")
    assert all(not p.assessment.violations for p in result.candidates)


class FailureProvider:
    def query(self, commitment, world):
        raise TimeoutError("simulated outage")


class EmptyProvider:
    def query(self, commitment, world):
        now = datetime.now(UTC)
        return ProviderResult(
            provider="empty_fixture",
            commitment_id=commitment.id,
            status="UNAVAILABLE",
            evidence="Fixture inventory checked: no options for this date.",
            exhausted={"SUBSTITUTE": "No hotel substitutes in fixture inventory."},
            checked_at=now,
            expires_at=now + timedelta(minutes=5),
        )


def test_provider_outage_is_blocked_not_unrecoverable(disrupted):
    service, incident = disrupted
    planner = demo_planner()
    planner.providers["hotel"] = FailureProvider()
    result = planner.plan(service.world, incident)
    assert result.status == "BLOCKED"
    assert result.candidates == ()
    assert not any(r.commitment_id == "hotel" for r in result.provider_results)
    assert any("TimeoutError" in n for n in result.notes)


def test_known_empty_inventory_is_no_feasible_plan(disrupted):
    service, incident = disrupted
    planner = demo_planner()
    planner.providers["hotel"] = EmptyProvider()
    result = planner.plan(service.world, incident)
    assert result.status == "NO_FEASIBLE_PLAN"
    assert result.provider_results[-1].exhausted["SUBSTITUTE"]


def test_missing_provider_is_unknown(disrupted):
    service, incident = disrupted
    planner = demo_planner()
    del planner.providers["hotel"]
    assert planner.plan(service.world, incident).status == "BLOCKED"


def test_expired_quotes_cannot_back_candidates(disrupted):
    service, incident = disrupted
    planner = demo_planner()
    original = planner.providers["hotel"]

    class Expired:
        def query(self, commitment, world):
            result = original.query(commitment, world)
            return result.model_copy(
                update={
                    "checked_at": datetime.now(UTC) - timedelta(minutes=10),
                    "expires_at": datetime.now(UTC) - timedelta(minutes=5),
                }
            )

    planner.providers["hotel"] = Expired()
    result = planner.plan(service.world, incident)
    assert result.status == "BLOCKED"
    assert result.candidates == ()


def test_user_deadline_cannot_be_erased_by_hotel_substitution(disrupted):
    service, incident = disrupted
    world = service.world.model_copy(
        update={
            "deadlines": (
                service.world.deadlines[0].model_copy(
                    update={"source": SourceRef(source="explicit_user", external_id="user_cutoff")}
                ),
            )
        }
    )
    result = demo_planner().plan(world, incident)
    assert result.candidates == ()
    assert result.rejections["invalid_operator"] == 1


def test_preexisting_unrelated_hard_constraint_survives_global_check(disrupted):
    service, incident = disrupted
    meeting_intent = Intent(
        id="intent_meetings",
        description="Attend the meetings",
        importance=0.5,
    )
    first = Commitment(
        id="meeting_1",
        kind="meeting",
        title="Morning meeting",
        intent_id=meeting_intent.id,
        start_at=at("10:00"),
        end_at=at("12:00"),
        source=SourceRef(source="explicit_user", external_id="meeting_1"),
    )
    second = Commitment(
        id="meeting_2",
        kind="meeting",
        title="Afternoon meeting",
        intent_id=meeting_intent.id,
        start_at=at("11:00"),
        end_at=at("12:30"),
        source=SourceRef(source="explicit_user", external_id="meeting_2"),
    )
    world = service.world.model_copy(
        update={
            "intents": (*service.world.intents, meeting_intent),
            "commitments": (*service.world.commitments, first, second),
            "dependencies": (
                *service.world.dependencies,
                Dependency(
                    id="meeting_1_to_meeting_2",
                    from_id=first.id,
                    to_id=second.id,
                    explanation="The meetings cannot overlap.",
                ),
            ),
        }
    )
    result = demo_planner().plan(world, incident)
    assert result.status == "COMPLETE"
    assert result.candidates
    assert result.rejections.get("global_hard_constraint", 0) == 0
    assert all(
        any(v.constraint_id == "meeting_1_to_meeting_2" for v in plan.assessment.violations)
        for plan in result.candidates
    )


def test_compensation_preserves_transit_and_lost_intent(disrupted):
    service, incident = disrupted
    result = demo_planner().plan(service.world, incident)
    plan = next(p for p in result.candidates if p.refund > 0 and p.intent_quality["intent_ticket"])
    assert "restaurant" not in {c.id for c in plan.world.commitments}
    assert plan.intent_quality["intent_restaurant"] == 0
    assert plan.refund == Decimal("30")
    bypass = next(
        d for d in plan.world.dependencies if d.from_id == "hotel" and d.to_id == "ticket"
    )
    assert bypass.lag_minutes == 60
    assert plan.additional_cost == Decimal("220")  # Refund does not extend spending authority.


def test_hard_requirement_cannot_be_bypassed(disrupted):
    service, _ = disrupted
    world = service.world.model_copy(
        update={
            "dependencies": (
                *service.world.dependencies,
                Dependency(
                    id="dinner_required",
                    from_id="restaurant",
                    to_id="ticket",
                    relation="REQUIRES",
                    explanation="Dinner is a hard prerequisite.",
                ),
            )
        }
    )
    option = RecoveryOption(
        id="remove",
        commitment_id="restaurant",
        resolution="ABANDONED",
        intent_quality=0,
        explanation="skip",
        evidence="fixture",
    )
    with pytest.raises(ValueError, match="prerequisite"):
        apply_option(world, option)


def test_reschedule_operator_preserves_identity(disrupted):
    service, _ = disrupted
    current = service.world.commitments[1]
    option = RecoveryOption(
        id="later_pickup",
        commitment_id=current.id,
        resolution="RESCHEDULED",
        replacement=current.model_copy(update={"start_at": at("19:40"), "end_at": at("20:25")}),
        explanation="Same provider confirmed later pickup.",
        evidence="fixture:reschedule",
    )
    world = apply_option(service.world, option)
    assert world.commitments[1].intent_id == current.intent_id
    assert not any(v.constraint_id == "flight_to_transfer" for v in evaluate(world).violations)


def test_healthy_plan_can_be_preserved():
    service = CascadeService(demo_world())
    incident = service.ingest(delay_event()).incident
    result = demo_planner().plan(demo_world(), incident)
    assert any(all(a.resolution == "PRESERVED" for a in p.actions) for p in result.candidates)


def test_invalid_provider_failure_cannot_claim_exhaustion():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="exhaustion"):
        ProviderResult(
            provider="bad",
            commitment_id="hotel",
            status="ERROR",
            evidence="outage",
            exhausted={"SUBSTITUTE": "none"},
            checked_at=now,
            expires_at=now + timedelta(minutes=1),
        )


def test_mutually_exclusive_required_intents_are_explicit(disrupted):
    service, incident = disrupted
    policy = SearchPolicy(
        required_intent_ids=(
            "intent_transfer",
            "intent_hotel",
            "intent_restaurant",
            "intent_ticket",
        )
    )
    result = demo_planner().plan(service.world, incident, policy)
    assert result.status == "NO_FEASIBLE_PLAN"
    assert result.candidates == ()
    assert result.rejections["required_intent"] > 0


def test_protecting_dinner_changes_available_tradeoffs(disrupted):
    service, incident = disrupted
    policy = SearchPolicy(
        required_intent_ids=(
            "intent_transfer",
            "intent_hotel",
            "intent_restaurant",
        )
    )
    result = demo_planner().plan(service.world, incident, policy)
    assert len(result.candidates) == 1
    assert result.candidates[0].intent_quality["intent_restaurant"] == 1
    assert result.candidates[0].intent_quality["intent_ticket"] == 0


def test_api_planning_is_read_only_versioned_and_audited():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        path = f"/v1/incidents/{incident['id']}/plan"
        state = client.get("/v1/state").json()
        assert client.post(path, json={"expected_version": 0}).status_code == 409
        response = client.post(path, json={"expected_version": 1})
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "COMPLETE"
        assert len(result["candidates"]) == 5
        assert client.get("/v1/state").json() == state
        assert client.get("/v1/audit").json()[-1]["type"] == "recovery.planned"
        plan_path = f"/v1/recovery-plans/{result['candidates'][0]['id']}"
        assert client.get(plan_path).json()["stale"] is False
        client.post("/v1/events", json=delay_event(version=1).model_dump(mode="json"))
        assert client.get(plan_path).json()["stale"] is True
        assert client.get("/v1/recovery-plans/missing").status_code == 404
        assert (
            client.post("/v1/incidents/missing/plan", json={"expected_version": 2}).status_code
            == 404
        )
        assert (
            client.post(
                path, json={"expected_version": 2, "policy": {"required_intent_ids": ["missing"]}}
            ).status_code
            == 422
        )
