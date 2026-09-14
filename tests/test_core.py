from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from cascade.constraints.engine import evaluate
from cascade.demo import at, delay_event, demo_world
from cascade.domain.models import Dependency, Mutation, World
from cascade.graph.traversal import descendants
from cascade.service import CascadeService, ConflictError


def test_baseline_is_feasible():
    assert evaluate(demo_world()).violations == ()


def test_unlinked_overlapping_commitments_are_a_hard_violation():
    world = demo_world()
    ticket = next(c for c in world.commitments if c.id == "ticket").model_copy(
        update={"start_at": at("19:45"), "end_at": at("21:30")}
    )
    world = world.model_copy(
        update={
            "commitments": tuple(ticket if c.id == "ticket" else c for c in world.commitments),
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket"),
        }
    )

    overlap = [v for v in evaluate(world).violations if v.constraint_id.startswith("overlap:")]
    assert len(overlap) == 1
    violation = overlap[0]
    assert violation.constraint_id == "overlap:restaurant:ticket"
    assert violation.affected_commitment_ids == ("restaurant", "ticket")
    assert violation.delay_minutes == 75


def test_ordered_overlapping_commitments_do_not_duplicate_an_overlap_violation():
    world = demo_world()
    ticket = next(c for c in world.commitments if c.id == "ticket").model_copy(
        update={"start_at": at("19:45"), "end_at": at("21:30")}
    )
    world = world.model_copy(
        update={"commitments": tuple(ticket if c.id == "ticket" else c for c in world.commitments)}
    )

    violations = evaluate(world).violations
    assert not any(v.constraint_id.startswith("overlap:") for v in violations)
    assert any(v.constraint_id == "restaurant_to_ticket" for v in violations)


def test_touching_unlinked_commitments_do_not_overlap():
    world = demo_world()
    ticket = next(c for c in world.commitments if c.id == "ticket").model_copy(
        update={"start_at": at("21:00"), "end_at": at("22:30")}
    )
    world = world.model_copy(
        update={
            "commitments": tuple(ticket if c.id == "ticket" else c for c in world.commitments),
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket"),
        }
    )

    assert not any(v.constraint_id.startswith("overlap:") for v in evaluate(world).violations)


def test_transitively_ordered_commitments_do_not_overlap():
    world = demo_world()
    restaurant = next(c for c in world.commitments if c.id == "restaurant").model_copy(
        update={"start_at": at("16:50"), "end_at": at("17:30")}
    )
    world = world.model_copy(
        update={
            "commitments": tuple(
                restaurant if c.id == "restaurant" else c for c in world.commitments
            ),
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket"),
        }
    )

    assert not any(
        v.constraint_id == "overlap:restaurant:transfer" for v in evaluate(world).violations
    )


def test_projected_shift_detects_an_unlinked_overlap():
    world = demo_world()
    world = world.model_copy(
        update={
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket")
        }
    )

    result = CascadeService(world).ingest(delay_event())
    assert any(v.constraint_id == "overlap:restaurant:ticket" for v in result.assessment.violations)


def test_overlap_constraint_id_is_stable_when_start_order_changes():
    world = demo_world()
    ticket = next(c for c in world.commitments if c.id == "ticket").model_copy(
        update={"start_at": at("19:45"), "end_at": at("21:30")}
    )
    first = world.model_copy(
        update={
            "commitments": tuple(ticket if c.id == "ticket" else c for c in world.commitments),
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket"),
        }
    )
    restaurant = next(c for c in world.commitments if c.id == "restaurant").model_copy(
        update={"start_at": at("20:00"), "end_at": at("22:00")}
    )
    second = world.model_copy(
        update={
            "commitments": tuple(
                restaurant if c.id == "restaurant" else ticket if c.id == "ticket" else c
                for c in world.commitments
            ),
            "dependencies": tuple(d for d in world.dependencies if d.id != "restaurant_to_ticket"),
        }
    )

    first_overlap = next(
        v for v in evaluate(first).violations if v.constraint_id.startswith("overlap:")
    )
    second_overlap = next(
        v for v in evaluate(second).violations if v.constraint_id.startswith("overlap:")
    )
    assert (
        first_overlap.constraint_id == second_overlap.constraint_id == "overlap:restaurant:ticket"
    )
    assert first_overlap.affected_commitment_ids == ("restaurant", "ticket")
    assert second_overlap.affected_commitment_ids == ("ticket", "restaurant")


def test_primary_demo_propagates_all_four_levels_without_rebooking():
    service = CascadeService(demo_world())
    result = service.ingest(delay_event())
    assert result.incident.affected_commitment_ids == ("transfer", "hotel", "restaurant", "ticket")
    assert {v.constraint_id for v in result.assessment.violations} == {
        "flight_to_transfer",
        "transfer_to_hotel",
        "hotel_to_restaurant",
        "restaurant_to_ticket",
        "hotel_cutoff",
    }
    assert result.assessment.projected_starts["ticket"] == at("23:45")
    assert service.world.commitments[-1].start_at == at("21:45")
    assert len(result.incident.threatened_intent_ids) == 4


@pytest.mark.parametrize("minutes", [-60, -1, 0, 1, 15, 30, 31, 45, 46, 60, 120, 175, 240])
def test_delay_thresholds(minutes):
    arrival = at("16:10") + timedelta(minutes=minutes)
    event = delay_event().model_copy(update={"new_start_at": arrival, "new_end_at": arrival})
    result = CascadeService(demo_world()).ingest(event)
    assert (result.incident is not None) == (minutes > 0)
    hotel_cutoff = any(v.constraint_id == "hotel_cutoff" for v in result.assessment.violations)
    assert hotel_cutoff == (minutes > 30)


def test_timezone_equivalence():
    from datetime import UTC

    world = demo_world()
    data = world.model_dump()
    for commitment in data["commitments"]:
        commitment["start_at"] = commitment["start_at"].astimezone(UTC)
        commitment["end_at"] = commitment["end_at"].astimezone(UTC)
    assert evaluate(World.model_validate(data)).violations == ()


def test_duplicate_event_is_idempotent():
    service = CascadeService(demo_world())
    first = service.ingest(delay_event())
    assert service.ingest(delay_event()) == first
    assert service.world.version == 1
    assert len(service.audit) == len(service.incidents) == 1


@pytest.mark.parametrize("case", ["reused_id", "stale", "confidence", "missing", "interval"])
def test_invalid_mutations_are_atomic(case):
    service = CascadeService(demo_world())
    service.ingest(delay_event())
    changes = {"event_id": "second", "expected_version": 1}
    if case == "reused_id":
        changes["event_id"] = delay_event().event_id
    elif case == "stale":
        changes["expected_version"] = 0
    elif case == "confidence":
        changes["source"] = {"source": "model", "external_id": "x", "confidence": 0.5}
    elif case == "missing":
        changes["commitment_id"] = "unknown"
    else:
        changes["new_end_at"] = at("15:00")
    event = Mutation.model_validate({**delay_event().model_dump(), **changes})
    before = service.world
    with pytest.raises((ConflictError, KeyError, ValidationError)):
        service.ingest(event)
    assert service.world == before
    assert len(service.audit) == 1


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        Mutation.model_validate({**delay_event().model_dump(), "new_start_at": "2026-09-11T19:05"})


def test_cycle_rejected():
    world = demo_world()
    edge = Dependency(id="cycle", from_id="ticket", to_id="flight", explanation="invalid")
    with pytest.raises(ValueError, match="cycle"):
        evaluate(world.model_copy(update={"dependencies": (*world.dependencies, edge)}))


def test_unknown_reference_rejected():
    data = demo_world().model_dump()
    data["dependencies"][0]["to_id"] = "missing"
    with pytest.raises(ValidationError, match="endpoint"):
        World.model_validate(data)


def test_soft_constraint_does_not_force_downstream_shift():
    world = demo_world()
    edges = (world.dependencies[0].model_copy(update={"hard": False}), *world.dependencies[1:])
    service = CascadeService(world.model_copy(update={"dependencies": edges}))
    result = service.ingest(delay_event())
    assert len(result.assessment.violations) == 1
    assert result.assessment.violations[0].severity == "soft"
    assert result.assessment.projected_starts["hotel"] == at("18:00")


def test_multiple_parents_take_latest_bound():
    world = demo_world()
    edge = Dependency(
        id="direct",
        from_id="flight",
        to_id="hotel",
        lag_minutes=200,
        explanation="additional hard prerequisite",
    )
    result = evaluate(world.model_copy(update={"dependencies": (*world.dependencies, edge)}))
    assert result.projected_starts["hotel"] == at("19:30")
    assert descendants(world, "ticket") == ()


def test_identical_state_does_not_create_new_incident():
    service = CascadeService(demo_world())
    service.ingest(delay_event())
    assert service.ingest(delay_event(version=1)).incident is None


def test_api_demo_and_audit():
    with TestClient(create_app()) as client:
        assert client.get("/v1/state").json()["assessment"]["violations"] == []
        response = client.post("/v1/demo/scenarios/flight_delay/inject")
        assert response.status_code == 200
        result = response.json()
        assert len(result["incident"]["affected_commitment_ids"]) == 4
        assert client.post("/v1/demo/scenarios/flight_delay/inject").json() == result
        assert len(client.get("/v1/audit").json()) == 1
        assert client.get(f"/v1/incidents/{result['incident']['id']}").status_code == 200
        assert client.get("/v1/incidents/missing").status_code == 404
        assert client.post("/v1/demo/scenarios/missing/inject").status_code == 404
        stale = delay_event().model_dump(mode="json") | {"event_id": "stale"}
        assert client.post("/v1/events", json=stale).status_code == 409


def test_api_instances_are_isolated():
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        first.post("/v1/demo/scenarios/flight_delay/inject")
        assert second.get("/v1/state").json()["world"]["version"] == 0
