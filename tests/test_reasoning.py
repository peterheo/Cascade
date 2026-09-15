import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from apps.api.main import create_app
from apps.api.simulation import create_app as create_simulation_app
from cascade.demo import at, delay_event, demo_world
from cascade.persistence import SqliteStore
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import (
    ExtractedChange,
    NaturalEventRequest,
    PlanComparison,
    PrivacySettings,
)
from cascade.reasoning.nebius import NebiusReasoner, NebiusSettings, ReasoningError
from cascade.reasoning.service import SemanticService
from cascade.service import CascadeService, ConflictError

TEXT = "Flight arrival is now 19:05 on September 11, 2026, local Nice time."


def change(**updates):
    return {
        "outcome": "UPDATE",
        "commitment_id": "flight",
        "new_start_at": "2026-09-11T19:05:00+02:00",
        "new_end_at": "2026-09-11T19:05:00+02:00",
        "confidence": 0.98,
        "evidence_quote": TEXT,
        "explanation": "Flight arrival moved to 19:05.",
        **updates,
    }


def completion(output, **updates):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(output),
                },
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30},
        **updates,
    }


def reasoner(handler):
    return NebiusReasoner(
        NebiusSettings(api_key=SecretStr("test-key-never-log")),
        transport=httpx.MockTransport(handler),
    )


def extraction_request(**updates):
    return NaturalEventRequest(event_id="text_event", expected_version=0, text=TEXT, **updates)


def test_nebius_wire_contract_and_local_validation():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=completion(change()))

    provider = reasoner(handler)
    parsed, trace = asyncio.run(
        provider.structured("extract", ExtractedChange, "extract", {"text": TEXT})
    )
    assert parsed.new_start_at == at("19:05")
    assert trace.input_tokens == 50 and trace.attempts == 1
    payload = json.loads(captured[0].content)
    assert str(captured[0].url) == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert payload["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in payload
    assert "test-key-never-log" not in json.dumps(payload)
    assert "test-key-never-log" not in trace.model_dump_json()
    assert provider.status()["live_verified"] is False  # Mock HTTP is not a live check.


def test_model_routing():
    models = []

    def handler(request):
        models.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            json=completion(
                {
                    "plans": [],
                    "recommended_plan_id": None,
                    "rationale": "No candidates.",
                }
            ),
        )

    provider = NebiusReasoner(
        NebiusSettings(api_key=SecretStr("test"), planning_model="configured-model"),
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(provider.structured("compare", PlanComparison, "compare", {}))
    assert models == ["configured-model"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_failures_retry_within_bound(status):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(
            status if len(attempts) == 1 else 200,
            json={} if len(attempts) == 1 else completion(change()),
        )

    _, trace = asyncio.run(reasoner(handler).structured("extract", ExtractedChange, "", {}))
    assert trace.attempts == len(attempts) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_failures_do_not_retry_or_leak_body(status):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(status, text="test-key-never-log and private provider body")

    with pytest.raises(ReasoningError) as error:
        asyncio.run(reasoner(handler).structured("extract", ExtractedChange, "", {}))
    assert len(attempts) == 1
    assert "private" not in str(error.value) and "test-key" not in str(error.value)


def test_retries_exhaust_and_timeout_are_bounded():
    attempts = []

    def failing(request):
        attempts.append(1)
        raise httpx.ConnectError("network unavailable")

    with pytest.raises(ReasoningError) as error:
        asyncio.run(reasoner(failing).structured("extract", ExtractedChange, "", {}))
    assert error.value.code == "unavailable" and len(attempts) == 2

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=completion(change()))

    provider = NebiusReasoner(
        NebiusSettings(api_key=SecretStr("test"), timeout_seconds=0.01),
        transport=httpx.MockTransport(slow),
    )
    with pytest.raises(ReasoningError) as error:
        asyncio.run(provider.structured("extract", ExtractedChange, "", {}))
    assert error.value.code == "timeout"


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"choices": []},
        {"choices": [1]},
        {"choices": ["bad"]},
        {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"refusal": "cannot comply"}}]},
        completion(change(new_start_at="2026-09-11T19:05:00")),
        completion(change(new_end_at="2026-09-11T18:05:00+02:00")),
        completion(change(confidence=1.1)),
        completion(change(execute=True)),
    ],
)
def test_invalid_model_output_is_rejected(output):
    with pytest.raises(ReasoningError) as error:
        asyncio.run(
            reasoner(lambda r: httpx.Response(200, json=output)).structured(
                "extract", ExtractedChange, "", {}
            )
        )
    assert error.value.code == "invalid_output"


def test_missing_credentials_make_no_network_call():
    provider = NebiusReasoner(
        NebiusSettings(),
        transport=httpx.MockTransport(lambda request: pytest.fail("must not call network")),
    )
    with pytest.raises(ReasoningError) as error:
        asyncio.run(provider.structured("extract", ExtractedChange, "", {}))
    assert error.value.code == "not_configured"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.tokenfactory.nebius.com/v1",
        "https://other.example/v1",
        "https://api.tokenfactory.nebius.com/v1?key=secret",
        "https://user:pass@api.tokenfactory.nebius.com/v1",
    ],
)
def test_configuration_rejects_unintended_credential_destinations(url):
    with pytest.raises(ValidationError):
        NebiusSettings(base_url=url)


def test_preview_apply_and_replay_are_idempotent():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=completion(change()))

    core = CascadeService(demo_world())
    semantic = SemanticService(core, reasoner(handler))

    async def scenario():
        request = extraction_request()
        preview = await semantic.extract(request)
        assert preview.status == "PROPOSED" and core.world.version == 0
        confirmed = await semantic.confirm(preview.id, 0)
        assert confirmed.status == "APPLIED" and core.world.version == 1
        assert await semantic.confirm(preview.id, 0) == confirmed
        assert await semantic.extract(request) == confirmed
        assert len(calls) == 1
        with pytest.raises(ConflictError):
            await semantic.extract(request.model_copy(update={"text": "different"}))

    asyncio.run(scenario())


@pytest.mark.parametrize("confidence", [0, 0.5, 0.89, 0.9, 1.0])
def test_low_confidence_requires_explicit_confirmation(confidence):
    core = CascadeService(demo_world())
    provider = reasoner(
        lambda r: httpx.Response(200, json=completion(change(confidence=confidence)))
    )
    semantic = SemanticService(core, provider)

    async def scenario():
        result = await semantic.extract(extraction_request(apply=True))
        assert (result.status == "APPLIED") == (confidence >= 0.9)
        if confidence < 0.9:
            assert core.world.version == 0
            assert result.status == "NEEDS_CONFIRMATION"
            result = await semantic.confirm(result.id, 0)
            assert core.world.commitments[0].source.source == "explicit_user_confirmation"
        assert result.event_result.incident is not None
        assert core.world.version == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "updates",
    [
        {"commitment_id": "invented"},
        {"evidence_quote": "not in the source"},
        {"evidence_quote": ""},
    ],
)
def test_ungrounded_extraction_never_mutates_state(updates):
    core = CascadeService(demo_world())
    semantic = SemanticService(
        core, reasoner(lambda r: httpx.Response(200, json=completion(change(**updates))))
    )
    with pytest.raises(ReasoningError):
        asyncio.run(semantic.extract(extraction_request(apply=True)))
    assert core.world.version == 0 and core.events == {}


@pytest.mark.parametrize("outcome", ["NO_CHANGE", "UNSUPPORTED", "NEEDS_CLARIFICATION"])
def test_non_mutation_outcomes_remain_non_mutating(outcome):
    core = CascadeService(demo_world())
    provider = reasoner(
        lambda r: httpx.Response(
            200,
            json=completion(
                change(outcome=outcome, commitment_id=None, new_start_at=None, new_end_at=None)
            ),
        )
    )
    semantic = SemanticService(core, provider)

    async def scenario():
        result = await semantic.extract(extraction_request(apply=True))
        assert result.mutation is None and core.world.version == 0
        with pytest.raises(ConflictError):
            await semantic.confirm(result.id, 0)

    asyncio.run(scenario())


def test_state_change_during_inference_rejects_stale_result():
    core = CascadeService(demo_world())

    def handler(request):
        core.ingest(delay_event())
        return httpx.Response(200, json=completion(change()))

    semantic = SemanticService(core, reasoner(handler))
    with pytest.raises(ConflictError):
        asyncio.run(semantic.extract(extraction_request(apply=True)))
    assert core.world.version == 1 and "text_event" not in core.events


def semantic_handler(request):
    payload = json.loads(request.content)
    task = payload["response_format"]["json_schema"]["name"]
    context = json.loads(payload["messages"][1]["content"])
    if task == "ExtractedChange":
        output = change()
    elif task == "ProposedStrategy":
        output = {
            "suggestions": [
                {
                    "commitment_id": "restaurant",
                    "priorities": ["COMPENSATED", "SUBSTITUTED"],
                    "rationale": "Show loss mitigation as well as substitution.",
                }
            ],
            "assumptions": ["Preferences are unknown."],
            "decisions_required": ["Dinner or movie?"],
        }
    else:
        output = {
            "plans": [
                {
                    "plan_id": p["id"],
                    "explanation": "A feasible proposed recovery.",
                    "tradeoff": "Review optional goals and spending.",
                }
                for p in context["candidates"]
            ],
            "recommended_plan_id": None,
            "rationale": "The user should choose among the tradeoffs.",
        }
    return httpx.Response(200, json=completion(output))


def test_assisted_plan_preserves_feasibility_and_policy():
    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    before = core.world
    semantic = SemanticService(core, reasoner(semantic_handler))
    result = asyncio.run(semantic.assisted_plan(incident.id, 1, SearchPolicy()))
    assert len(result.planning.candidates) == 5
    assert result.strategy is not None and result.comparison is not None
    assert result.warnings == () and len(result.model_calls) == 2
    assert all(not p.assessment.violations for p in result.planning.candidates)
    assert core.world == before
    assert any(a["type"] == "reasoning.completed" for a in core.audit)


def test_invented_plan_cannot_be_recommended():
    def handler(request):
        response = semantic_handler(request)
        if (
            json.loads(request.content)["response_format"]["json_schema"]["name"]
            == "PlanComparison"
        ):
            envelope = response.json()
            output = json.loads(envelope["choices"][0]["message"]["content"])
            output["recommended_plan_id"] = "invented-plan"
            return httpx.Response(200, json=completion(output))
        return response

    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    result = asyncio.run(
        SemanticService(core, reasoner(handler)).assisted_plan(incident.id, 1, SearchPolicy())
    )
    assert result.comparison is None and len(result.planning.candidates) == 5
    assert "invalid_output" in result.warnings[0]


def test_missing_model_does_not_disable_deterministic_planning():
    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    result = asyncio.run(
        SemanticService(core, NebiusReasoner(NebiusSettings())).assisted_plan(
            incident.id, 1, SearchPolicy()
        )
    )
    assert len(result.planning.candidates) == 5
    assert result.strategy is result.comparison is None
    assert len(result.warnings) == 2 and result.model_calls == ()


def test_natural_event_api_end_to_end():
    with TestClient(create_app(reasoner(semantic_handler))) as client:
        request = extraction_request().model_dump(mode="json")
        preview = client.post("/v1/events/text", json=request)
        assert preview.status_code == 200
        extraction = preview.json()
        assert extraction["status"] == "PROPOSED"
        assert client.get("/v1/state").json()["world"]["version"] == 0
        path = f"/v1/extractions/{extraction['id']}"
        confirmed = client.post(path + "/confirm", json={"expected_version": 0}).json()
        assert confirmed["status"] == "APPLIED"
        assert client.get(path).json()["stale"] is False
        incident = confirmed["event_result"]["incident"]
        assisted = client.post(
            f"/v1/incidents/{incident['id']}/plan/assisted", json={"expected_version": 1}
        )
        assert assisted.status_code == 200 and assisted.json()["comparison"] is not None
        assert client.post("/v1/events/text", json=request).json()["status"] == "APPLIED"
        request["text"] = "changed"
        assert client.post("/v1/events/text", json=request).status_code == 409
        assert client.get("/v1/extractions/missing").status_code == 404


def test_missing_credentials_api_reports_unconfigured():
    with TestClient(create_app(NebiusReasoner(NebiusSettings()))) as client:
        assert client.get("/v1/reasoning/status").json()["configured"] is False
        response = client.post(
            "/v1/events/text", json=extraction_request(apply=True).model_dump(mode="json")
        )
        assert response.status_code == 503 and response.json()["code"] == "not_configured"
        assert client.get("/v1/state").json()["world"]["version"] == 0


def test_concurrent_replay_makes_one_model_call():
    calls = []

    async def handler(request):
        calls.append(1)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=completion(change()))

    core = CascadeService(demo_world())
    semantic = SemanticService(core, reasoner(handler))

    async def scenario():
        request = extraction_request(apply=True)
        first, second = await asyncio.gather(semantic.extract(request), semantic.extract(request))
        assert first == second

    asyncio.run(scenario())
    assert len(calls) == 1 and core.world.version == 1


def test_stale_confirmation_cannot_overwrite_new_state():
    core = CascadeService(demo_world())
    semantic = SemanticService(core, reasoner(semantic_handler))

    async def scenario():
        preview = await semantic.extract(extraction_request())
        core.ingest(delay_event(arrival="20:00"))
        with pytest.raises(ConflictError):
            await semantic.confirm(preview.id, 0)

    asyncio.run(scenario())
    assert core.world.commitments[0].start_at == at("20:00")


def test_repeated_fact_does_not_increment_world_version():
    core = CascadeService(demo_world())
    provider = reasoner(
        lambda r: httpx.Response(
            200,
            json=completion(
                change(
                    new_start_at=at("16:10").isoformat(),
                    new_end_at=at("16:10").isoformat(),
                )
            ),
        )
    )
    result = asyncio.run(SemanticService(core, provider).extract(extraction_request(apply=True)))
    assert result.status == "NO_CHANGE" and core.world.version == 0


@pytest.mark.parametrize("invalid", ["strategy_id", "strategy_operator", "comparison_missing"])
def test_invalid_semantic_advice_falls_back_to_verified_facts(invalid):
    def handler(request):
        response = semantic_handler(request)
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        output = json.loads(response.json()["choices"][0]["message"]["content"])
        if name == "ProposedStrategy" and invalid == "strategy_id":
            output["suggestions"][0]["commitment_id"] = "flight"
        if name == "ProposedStrategy" and invalid == "strategy_operator":
            output["suggestions"][0]["priorities"] = ["SPEND_WITHOUT_APPROVAL"]
        if name == "PlanComparison" and invalid == "comparison_missing":
            output["plans"] = []
        return httpx.Response(200, json=completion(output))

    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    result = asyncio.run(
        SemanticService(core, reasoner(handler)).assisted_plan(
            incident.id,
            1,
            SearchPolicy(),
        )
    )
    assert len(result.planning.candidates) == 5
    assert result.warnings and all(not p.assessment.violations for p in result.planning.candidates)
    assert core.world.version == 1


def test_state_change_during_comparison_rejects_stale_advice():
    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident

    def handler(request):
        if (
            json.loads(request.content)["response_format"]["json_schema"]["name"]
            == "PlanComparison"
        ):
            core.ingest(delay_event(version=1, arrival="20:00"))
        return semantic_handler(request)

    with pytest.raises(ConflictError):
        asyncio.run(
            SemanticService(core, reasoner(handler)).assisted_plan(
                incident.id,
                1,
                SearchPolicy(),
            )
        )
    assert core.world.version == 2


def _json_context(request):
    return json.loads(request.content)["messages"][1]["content"]


def _contains_key(value, key):
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_reasoning_contexts_are_minimized_and_manifests_match_wire_payload():
    captured = []

    def handler(request):
        captured.append(request)
        return semantic_handler(request)

    core = CascadeService(demo_world())
    semantic = SemanticService(core, reasoner(handler))
    extraction = asyncio.run(semantic.extract(extraction_request()))
    extract_context_payload = json.loads(_json_context(captured[0]))
    assert all(
        set(commitment) == {"id", "kind", "title", "start_at", "end_at"}
        for commitment in extract_context_payload["world"]["commitments"]
    )
    assert not _contains_key(extract_context_payload, "source")
    assert not _contains_key(extract_context_payload, "intent_id")
    extract_manifest = extraction.model_call.manifest
    assert extract_manifest.input_hash == extraction.model_call.input_hash
    assert extract_manifest.bytes_sent == len(_json_context(captured[0]).encode())

    incident = core.ingest(delay_event()).incident
    assisted = asyncio.run(semantic.assisted_plan(incident.id, 1, SearchPolicy()))
    assert assisted.strategy is not None and assisted.comparison is not None
    strategy_payload = json.loads(_json_context(captured[1]))
    assert {item["id"] for item in strategy_payload["commitments"]} == set(
        incident.affected_commitment_ids
    )
    assert strategy_payload["allowed_resolutions"] == [
        "PRESERVED",
        "RESCHEDULED",
        "SUBSTITUTED",
        "COMPENSATED",
        "ABANDONED",
    ]
    assert not _contains_key(strategy_payload, "source")
    assert not _contains_key(strategy_payload, "intent_id")
    compare_payload = json.loads(_json_context(captured[2]))
    assert not _contains_key(compare_payload, "world")
    assert all(
        set(action) == {"commitment_id", "resolution", "option_id"}
        for candidate in compare_payload["candidates"]
        for action in candidate["actions"]
    )
    for request in captured:
        assert request.content
    assert all(call.manifest.input_hash == call.input_hash for call in assisted.model_calls)
    assert all(
        call.manifest.bytes_sent == len(_json_context(request).encode())
        for call, request in zip(assisted.model_calls, captured[1:], strict=True)
    )


def test_live_inference_can_be_disabled_without_transport_calls():
    calls = []

    def handler(request):
        calls.append(request)
        return semantic_handler(request)

    core = CascadeService(demo_world())
    incident = core.ingest(delay_event()).incident
    semantic = SemanticService(core, reasoner(handler), PrivacySettings(live_inference=False))
    result = asyncio.run(semantic.assisted_plan(incident.id, 1, SearchPolicy()))
    assert not calls
    assert len(result.planning.candidates) == 5
    assert result.model_calls == ()
    assert all("disabled" in warning for warning in result.warnings)


def test_event_text_is_hashed_in_persisted_audit_by_default(tmp_path):
    core = CascadeService(demo_world(), store=SqliteStore(tmp_path / "gateway.db"))
    semantic = SemanticService(
        core,
        reasoner(lambda request: httpx.Response(200, json=completion(change()))),
    )
    asyncio.run(semantic.extract(extraction_request()))
    persisted = SqliteStore(tmp_path / "gateway.db").load()
    assert persisted is not None
    extracted_audits = [entry for entry in persisted.audit if entry["type"] == "event.extracted"]
    assert len(extracted_audits) == 1
    entry = extracted_audits[0]
    assert entry["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert "text" not in entry
    assert TEXT not in json.dumps(persisted.audit)


def test_privacy_endpoints_exist_in_gateway_and_simulation():
    for factory in (create_app, create_simulation_app):
        with TestClient(factory(NebiusReasoner(NebiusSettings()))) as client:
            assert client.get("/v1/privacy").json() == {
                "live_inference": True,
                "persist_event_text": False,
            }
            updated = client.patch("/v1/privacy", json={"live_inference": False})
            assert updated.status_code == 200
            assert updated.json()["live_inference"] is False
            assert client.get("/v1/reasoning/status").json()["live_inference"] is False
