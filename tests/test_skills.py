from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from cascade.demo import delay_event, demo_world
from cascade.planning.models import SearchPolicy
from cascade.planning.skills import Skill, load_skills, select
from cascade.service import CascadeService


@pytest.fixture
def disrupted():
    service = CascadeService(demo_world())
    return service, service.ingest(delay_event()).incident


def skill(name: str) -> Skill:
    return next(s for s in load_skills() if s.name == name)


def test_shipped_skills_are_valid_and_uniquely_versioned():
    skills = load_skills()
    assert len(skills) >= 3
    assert len({(s.name, s.version) for s in skills}) == len(skills)
    assert all(s.operators and s.description and s.trigger.commitment_kinds for s in skills)


def test_the_matching_template_is_selected_from_the_incident(disrupted):
    service, incident = disrupted
    chosen = select(service.world, incident)
    assert chosen is not None
    assert chosen.name == "flight_delay_recovery"
    # A reservation template does not fire on a flight change.
    assert not skill("reservation_conflict_recovery").matches(service.world, incident)


def test_an_opt_in_template_is_never_chosen_for_the_user(disrupted):
    service, incident = disrupted
    restrictive = skill("accommodation_first_recovery")
    assert restrictive.auto_select is False
    # Its trigger genuinely matches; only the opt-in flag keeps it out of selection.
    assert restrictive.matches(service.world, incident)
    assert select(service.world, incident).name != restrictive.name
    assert service.skill_for(incident, restrictive.name).name == restrictive.name


def test_withholding_an_operator_narrows_the_surfaced_plans(disrupted):
    service, incident = disrupted
    default = service.plan(incident.id, service.world.version)
    assert default.skill.name == "flight_delay_recovery"
    assert len(default.candidates) == 5
    assert any(a.resolution == "ABANDONED" for p in default.candidates for a in p.actions)

    restricted = service.plan(
        incident.id, service.world.version, skill_name="accommodation_first_recovery"
    )
    assert restricted.skill.name == "accommodation_first_recovery"
    assert 0 < len(restricted.candidates) < len(default.candidates)
    assert not any(a.resolution == "ABANDONED" for p in restricted.candidates for a in p.actions)
    assert restricted.rejections["operator_not_allowed"] > 0
    # Narrower, but never unsound: the survivors still pass the constraint engine.
    assert all(not p.assessment.violations for p in restricted.candidates)


def test_template_limits_apply_and_an_explicit_policy_outranks_them(disrupted):
    service, incident = disrupted
    templated = service.plan(incident.id, service.world.version)
    assert templated.policy.max_candidates == skill("flight_delay_recovery").limits.max_candidates
    explicit = SearchPolicy(max_candidates=3, max_additional_cost=Decimal("210"))
    overridden = service.plan(incident.id, service.world.version, explicit)
    assert overridden.policy == explicit
    assert overridden.skill.name == "flight_delay_recovery"
    assert all(p.additional_cost <= 210 for p in overridden.candidates)


def test_an_unknown_template_is_an_error_not_a_silent_default(disrupted):
    service, incident = disrupted
    with pytest.raises(ValueError, match="unknown recovery skill"):
        service.plan(incident.id, service.world.version, skill_name="does_not_exist")


def test_preservation_cannot_be_restricted():
    with pytest.raises(ValidationError, match="always available"):
        SearchPolicy(allowed_resolutions=("PRESERVED", "SUBSTITUTED"))
    with pytest.raises(ValidationError, match="always available"):
        Skill(
            name="bad",
            version=1,
            description="d",
            trigger={"commitment_kinds": ["flight"]},
            inspect_kinds=["hotel"],
            operators=["PRESERVED"],
        )


def test_a_template_cannot_order_an_operator_it_forbids():
    with pytest.raises(ValidationError, match="does not allow"):
        Skill(
            name="bad",
            version=1,
            description="d",
            trigger={"commitment_kinds": ["flight"]},
            inspect_kinds=["hotel"],
            operators=["SUBSTITUTED"],
            order=["SUBSTITUTED", "ABANDONED"],
        )


def test_ordering_changes_search_order_without_changing_what_is_feasible(disrupted):
    service, incident = disrupted
    template = skill("flight_delay_recovery")
    priorities = template.priorities(service.world, incident)
    assert set(priorities) == set(incident.affected_commitment_ids)
    ordered = service.plan(incident.id, service.world.version)
    unordered = service.planner.plan(service.world, incident, ordered.policy)
    assert {p.explanation for p in ordered.candidates} == {
        p.explanation for p in unordered.candidates
    }


def test_api_exposes_templates_and_accepts_an_explicit_choice():
    with TestClient(create_app()) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        catalogue = client.get("/v1/skills").json()
        assert {s["name"] for s in catalogue} >= {"flight_delay_recovery"}
        assert all(s["version"] >= 1 for s in catalogue)
        path = f"/v1/incidents/{incident['id']}/plan"
        default = client.post(path, json={"expected_version": 1}).json()
        assert default["skill"]["name"] == "flight_delay_recovery"
        chosen = client.post(
            path, json={"expected_version": 1, "skill": "accommodation_first_recovery"}
        ).json()
        assert chosen["skill"]["name"] == "accommodation_first_recovery"
        assert len(chosen["candidates"]) < len(default["candidates"])
        assert client.post(path, json={"expected_version": 1, "skill": "nope"}).status_code == 422
