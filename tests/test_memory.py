from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from cascade.demo import delay_event, demo_world
from cascade.memory.preferences import PreferenceError, narrow, preference
from cascade.memory.resolutions import MIN_EVIDENCE, suggest
from cascade.planning.models import SearchPolicy
from cascade.service import CascadeService
from cascade.tools.demo import demo_gateway


@pytest.fixture
def disrupted():
    service = CascadeService(demo_world(), demo_gateway())
    return service, service.ingest(delay_event()).incident


def resolve(service, incident, plan):
    request = service.execute(plan.id, service.world.version, "user").approval_request
    service.approve(
        request.id, "user", tuple(i.action_id for i in request.items), request.total_amount
    )
    return service.execute(plan.id, service.world.version, "user")


def test_an_explicit_preference_narrows_the_search(disrupted):
    service, incident = disrupted
    before = service.plan(incident.id, service.world.version)
    assert any(p.intent_quality["intent_restaurant"] < 1 for p in before.candidates)
    service.add_preference(
        preference("Protect the celebration dinner.", "require_intent", "intent_restaurant")
    )
    after = service.plan(incident.id, service.world.version)
    assert after.candidates
    assert len(after.candidates) < len(before.candidates)
    assert all(p.intent_quality["intent_restaurant"] == 1 for p in after.candidates)
    assert "intent_restaurant" in after.policy.required_intent_ids


def test_preferences_only_narrow_and_never_widen(disrupted):
    service, _ = disrupted
    items = (
        preference("Spend no more than 210.", "limit_spend", "210"),
        preference("Never silently drop a commitment.", "forbid_operator", "ABANDONED"),
    )
    generous = SearchPolicy(max_additional_cost=Decimal("500"))
    tightened = narrow(generous, items, service.world)
    assert tightened.max_additional_cost == Decimal("210")
    assert "ABANDONED" not in tightened.allowed_resolutions
    # A preference cannot raise a caller's tighter ceiling.
    strict = SearchPolicy(max_additional_cost=Decimal("100"))
    assert narrow(strict, items, service.world).max_additional_cost == Decimal("100")


def test_a_preference_that_forbids_everything_is_refused(disrupted):
    service, _ = disrupted
    for resolution in ("RESCHEDULED", "SUBSTITUTED", "COMPENSATED"):
        service.add_preference(preference(f"No {resolution}.", "forbid_operator", resolution))
    with pytest.raises(PreferenceError, match="every recovery operator"):
        service.add_preference(preference("No ABANDONED.", "forbid_operator", "ABANDONED"))


def test_an_unknown_intent_cannot_be_required(disrupted):
    service, _ = disrupted
    with pytest.raises(PreferenceError, match="unknown intent"):
        service.add_preference(preference("Protect nothing.", "require_intent", "intent_missing"))


def test_a_preference_must_be_actionable():
    with pytest.raises(ValidationError, match="provider operator"):
        preference("Never preserve.", "forbid_operator", "PRESERVED")
    with pytest.raises(ValidationError, match="decimal amount"):
        preference("Spend little.", "limit_spend", "not-a-number")
    with pytest.raises(ValidationError, match="not a guess"):
        preference("Protect the hotel.", "require_intent", "intent_hotel", confidence=0.5)


def test_a_learned_preference_is_never_policy_until_a_person_promotes_it(disrupted):
    service, _ = disrupted
    learned = preference(
        "Observed choice.",
        "require_intent",
        "intent_hotel",
        source="learned",
        confidence=0.84,
        evidence=("res_a", "res_b", "res_c"),
    )
    assert learned.status == "SUGGESTED"
    with pytest.raises(PreferenceError, match="promoted, not created active"):
        service.add_preference(learned.model_copy(update={"status": "ACTIVE"}))
    stored = service.add_preference(learned)
    assert narrow(SearchPolicy(), (stored,), service.world).required_intent_ids == (
        "intent_transfer",
        "intent_hotel",
    )
    promoted = service.set_preference_status(stored.id, "ACTIVE", "user")
    assert promoted.status == "ACTIVE"
    assert promoted.source == "learned"
    assert promoted.confidence == 0.84
    assert promoted.evidence_count == 3


def test_only_a_person_can_promote_a_learned_preference(disrupted):
    service, _ = disrupted
    stored = service.add_preference(
        preference("Observed.", "require_intent", "intent_hotel", source="learned", evidence=("r",))
    )
    with pytest.raises(PreferenceError, match="only a person"):
        service.set_preference_status(stored.id, "ACTIVE", "cascade")


def test_preferences_can_be_inspected_and_deleted(disrupted):
    service, _ = disrupted
    stored = service.add_preference(
        preference("Protect the hotel.", "require_intent", "intent_hotel")
    )
    assert list(service.preferences) == [stored.id]
    service.delete_preference(stored.id)
    assert not service.preferences
    with pytest.raises(KeyError):
        service.delete_preference(stored.id)


def test_a_resolution_is_recorded_with_what_it_was_chosen_over(disrupted):
    service, incident = disrupted
    planning = service.plan(incident.id, service.world.version)
    chosen = next(p for p in planning.candidates if p.intent_quality["intent_ticket"] == 1)
    execution = resolve(service, incident, chosen)
    assert execution.status == "COMPLETED"
    assert len(service.resolutions) == 1
    record = service.resolutions[0]
    assert record.outcome == "EXECUTED"
    assert record.selected_plan_id == chosen.id
    assert len(record.rejected_plan_ids) == len(planning.candidates) - 1
    assert len(record.rejected_intents) == len(record.rejected_plan_ids)
    assert record.executed_actions
    assert record.skill == "flight_delay_recovery"


def test_a_dismissal_is_also_an_outcome(disrupted):
    service, incident = disrupted
    service.plan(incident.id, service.world.version)
    service.dismiss(incident.id, "user", "Travelling tomorrow instead.")
    assert [r.outcome for r in service.resolutions] == ["DISMISSED"]
    assert service.suggestions() == ()


def test_a_suggestion_needs_repeated_agreement(disrupted):
    service, incident = disrupted
    planning = service.plan(incident.id, service.world.version)
    chosen = next(
        p
        for p in planning.candidates
        if p.intent_quality["intent_ticket"] == 1 and p.intent_quality["intent_restaurant"] == 0
    )
    resolve(service, incident, chosen)
    # One choice is an anecdote, not a preference.
    assert service.suggestions() == ()
    record = service.resolutions[0]
    repeats = tuple(record.model_copy(update={"id": f"res_{n}"}) for n in range(MIN_EVIDENCE))
    learned = suggest(repeats, service.world)
    assert [p.value for p in learned] == ["intent_ticket"]
    assert learned[0].source == "learned"
    assert learned[0].status == "SUGGESTED"
    assert learned[0].confidence == 1
    assert learned[0].evidence_count == MIN_EVIDENCE
    assert "intent_restaurant" not in [p.value for p in learned]


def test_a_reversed_choice_lowers_confidence_immediately(disrupted):
    service, incident = disrupted
    planning = service.plan(incident.id, service.world.version)
    keeps_movie = next(
        p
        for p in planning.candidates
        if p.intent_quality["intent_ticket"] == 1 and p.intent_quality["intent_restaurant"] == 0
    )
    keeps_dinner = next(
        p
        for p in planning.candidates
        if p.intent_quality["intent_restaurant"] == 1 and p.intent_quality["intent_ticket"] == 0
    )
    resolve(service, incident, keeps_movie)
    consistent = service.resolutions[0]
    reversed_record = consistent.model_copy(
        update={
            "id": "res_reversed",
            "selected_plan_id": keeps_dinner.id,
            "chosen_intents": tuple(
                outcome.model_copy(update={"kept": not outcome.kept})
                if outcome.intent_id in ("intent_ticket", "intent_restaurant")
                else outcome
                for outcome in consistent.chosen_intents
            ),
        }
    )
    agreeing = tuple(consistent.model_copy(update={"id": f"res_{n}"}) for n in range(3))
    assert suggest(agreeing, service.world)[0].confidence == 1
    mixed = suggest((*agreeing, reversed_record), service.world)
    assert mixed and mixed[0].confidence < 1
    assert mixed[0].evidence_count == 3


def test_api_preferences_and_memory():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        path = f"/v1/incidents/{incident['id']}/plan"
        before = client.post(path, json={"expected_version": 1}).json()
        created = client.post(
            "/v1/preferences",
            json={
                "statement": "Protect the celebration dinner.",
                "directive": "require_intent",
                "value": "intent_restaurant",
            },
        )
        assert created.status_code == 200
        stored = created.json()
        assert stored["source"] == "explicit_user" and stored["status"] == "ACTIVE"
        after = client.post(path, json={"expected_version": 1}).json()
        assert len(after["candidates"]) < len(before["candidates"])
        assert client.get("/v1/preferences").json()[0]["id"] == stored["id"]
        assert (
            client.patch(f"/v1/preferences/{stored['id']}", json={"status": "RETIRED"}).json()[
                "status"
            ]
            == "RETIRED"
        )
        restored = client.post(path, json={"expected_version": 1}).json()
        assert len(restored["candidates"]) == len(before["candidates"])
        assert client.delete(f"/v1/preferences/{stored['id']}").status_code == 204
        assert client.get("/v1/preferences").json() == []
        assert client.delete(f"/v1/preferences/{stored['id']}").status_code == 404
        assert (
            client.post(
                "/v1/preferences",
                json={"statement": "s", "directive": "require_intent", "value": "nope"},
            ).status_code
            == 422
        )
        assert client.get("/v1/memory/resolutions").json() == []
        assert client.get("/v1/memory/suggestions").json() == []
