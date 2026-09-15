import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.api.simulation import create_app as create_simulation_app
from cascade.demo import delay_event, demo_world
from cascade.memory.preferences import preference
from cascade.memory.resolutions import record_dismissal
from cascade.persistence import MemoryStore, SqliteStore
from cascade.service import CascadeService, ConflictError
from cascade.tools.demo import demo_gateway


def test_memory_store_is_empty_and_sqlite_round_trips_every_record_kind_and_stream(tmp_path):
    assert MemoryStore().load() is None
    event = delay_event()
    source = CascadeService(demo_world())
    result = source.ingest(event)
    pref = preference("Protect the hotel.", "require_intent", "intent_hotel")
    resolution = record_dismissal(result.incident.id, "YELLOW")
    path = tmp_path / "state.db"
    store = SqliteStore(path)
    with store.transaction():
        store.save_world(source.world)
        store.put("event", event.event_id, (event, result))
        store.put("incident", result.incident.id, result.incident)
        store.put("preference", pref.id, pref)
        store.append("resolution", resolution.model_dump(mode="json"))
        store.append("audit", {"type": "test", "value": 1})

    loaded = SqliteStore(path).load()
    assert loaded is not None
    assert loaded.world == source.world
    assert loaded.events[event.event_id] == (event, result)
    assert loaded.incidents == [result.incident]
    assert loaded.preferences == {pref.id: pref}
    assert loaded.resolutions == [resolution]
    assert loaded.audit == [{"type": "test", "value": 1}]


def test_service_restart_restores_world_incidents_preferences_resolutions_and_audit(tmp_path):
    path = tmp_path / "gateway.db"
    service_a = CascadeService(demo_world(), store=SqliteStore(path))
    result = service_a.ingest(delay_event())
    pref = preference("Spend no more than one euro.", "limit_spend", "1")
    service_a.add_preference(pref)
    dismissed = service_a.dismiss(result.incident.id, "user", "Keep the original plan.")

    service_b = CascadeService(demo_world(), store=SqliteStore(path))
    assert service_b.world == service_a.world
    assert service_b.events == service_a.events
    assert service_b.incidents == [dismissed]
    assert service_b.preferences == {pref.id: pref}
    assert service_b.resolutions == service_a.resolutions
    assert service_b.audit == service_a.audit


def test_persisted_memory_still_constrains_recovery_after_restart(tmp_path):
    path = tmp_path / "gateway.db"
    service_a = CascadeService(demo_world(), store=SqliteStore(path))
    incident = service_a.ingest(delay_event()).incident
    service_a.add_preference(preference("Spend no more than one euro.", "limit_spend", "1"))

    service_b = CascadeService(demo_world(), store=SqliteStore(path))
    planning = service_b.plan(incident.id, service_b.world.version)
    assert planning.policy.max_additional_cost == 1
    assert planning.candidates == ()


def test_incident_record_order_survives_updates_and_restart(tmp_path):
    path = tmp_path / "order.db"
    service_a = CascadeService(demo_world(), store=SqliteStore(path))
    first = service_a.ingest(delay_event()).incident
    second_event = delay_event(version=1, arrival="20:00").model_copy(
        update={"event_id": "second-delay"}
    )
    second = service_a.ingest(second_event).incident
    service_a.plan(first.id, 2)

    service_b = CascadeService(demo_world(), store=SqliteStore(path))
    assert [incident.id for incident in service_b.incidents] == [first.id, second.id]


def test_preference_record_order_survives_status_update_and_restart(tmp_path):
    path = tmp_path / "preference-order.db"
    service_a = CascadeService(demo_world(), store=SqliteStore(path))
    first = preference("Protect the hotel.", "require_intent", "intent_hotel")
    second = preference("Protect the dinner.", "require_intent", "intent_restaurant")
    service_a.add_preference(first)
    service_a.add_preference(second)
    service_a.set_preference_status(first.id, "RETIRED", "user")

    service_b = CascadeService(demo_world(), store=SqliteStore(path))
    assert list(service_b.preferences) == [first.id, second.id]


def test_idempotency_and_conflicts_survive_restart(tmp_path):
    path = tmp_path / "gateway.db"
    service_a = CascadeService(demo_world(), store=SqliteStore(path))
    first = service_a.ingest(delay_event())
    service_b = CascadeService(demo_world(), store=SqliteStore(path))

    assert service_b.ingest(delay_event()) == first
    stale = delay_event().model_copy(
        update={"event_id": "stale-after-restart", "expected_version": 0}
    )
    with pytest.raises(ConflictError):
        service_b.ingest(stale)
    assert service_b.world.version == first.world_version


def test_simulation_preferences_survive_app_recreation_and_demo_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_SIMULATION_DB", str(tmp_path / "simulation.db"))
    first = create_simulation_app()
    with TestClient(first) as client:
        created = client.post(
            "/v1/preferences",
            json={
                "statement": "Protect the hotel.",
                "directive": "require_intent",
                "value": "intent_hotel",
            },
        )
        assert created.status_code == 200
        assert client.post("/v1/demo/reset").status_code == 200

    second = create_simulation_app()
    with TestClient(second) as client:
        preferences = client.get("/v1/preferences").json()
        assert [item["id"] for item in preferences] == [created.json()["id"]]
        assert client.post("/v1/demo/reset").status_code == 200
        assert client.get("/v1/preferences").json() == preferences


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '2')")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="newer"):
        SqliteStore(path)


def _durable_service(db_path, ledger_path):
    return CascadeService(
        demo_world(),
        gateway=demo_gateway(ledger_path=ledger_path),
        store=SqliteStore(db_path),
    )


def _planned_durable_service(db_path, ledger_path):
    service = _durable_service(db_path, ledger_path)
    incident = service.ingest(delay_event()).incident
    planning = service.plan(incident.id, service.world.version)
    return service, incident, planning.candidates[0]


def test_pending_approval_survives_restart_and_can_be_completed(tmp_path):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    service_a, incident, plan = _planned_durable_service(db_path, ledger_path)

    pending = service_a.execute(plan.id, service_a.world.version, "user")
    request = pending.approval_request
    assert request is not None

    service_b = _durable_service(db_path, ledger_path)
    assert service_b.approvals[request.id].status == "PENDING"
    assert service_b.plan_incidents[incident.id] == tuple(service_b.plans)

    approved = service_b.approve(
        request.id,
        "user",
        tuple(item.action_id for item in request.items),
        request.total_amount,
    )
    completed = service_b.execute(plan.id, service_b.world.version, "user")
    assert approved.status == "APPROVED"
    assert completed.status == "COMPLETED"
    assert completed.id in service_b.executions


def test_approval_restored_after_restart_rejects_stale_world(tmp_path):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    service_a, _, plan = _planned_durable_service(db_path, ledger_path)
    pending = service_a.execute(plan.id, service_a.world.version, "user")
    request = pending.approval_request
    assert request is not None

    service_b = _durable_service(db_path, ledger_path)
    service_b.ingest(
        delay_event(version=1, arrival="20:00").model_copy(update={"event_id": "new-delay"})
    )
    with pytest.raises(ConflictError, match="world changed"):
        service_b.approve(
            request.id,
            "user",
            tuple(item.action_id for item in request.items),
            request.total_amount,
        )


def test_execution_history_and_provider_ledger_survive_restart(tmp_path):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    service_a, _, plan = _planned_durable_service(db_path, ledger_path)
    pending = service_a.execute(plan.id, service_a.world.version, "user")
    request = pending.approval_request
    assert request is not None
    service_a.approve(
        request.id,
        "user",
        tuple(item.action_id for item in request.items),
        request.total_amount,
    )
    completed = service_a.execute(plan.id, service_a.world.version, "user")
    assert completed.status == "COMPLETED"

    service_b = _durable_service(db_path, ledger_path)
    assert service_b.executions[completed.id] == completed
    step = next(step for step in completed.steps if step.call is not None)
    action = step.call.action
    provider = service_b.gateway.providers[action.provider]
    before = len(provider.ledger)
    replay = provider.apply(action)
    assert replay.success is True
    assert replay.side_effect is False
    assert len(provider.ledger) == before


def test_search_result_is_stored_once_per_planning_call(tmp_path):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    service = _durable_service(db_path, ledger_path)
    incident = service.ingest(delay_event()).incident
    service.plan(incident.id, service.world.version)
    service.plan(incident.id, service.world.version)

    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM records WHERE kind = 'search'").fetchone()[
            0
        ]
    finally:
        connection.close()
    assert count == 2


def test_provider_write_gap_is_rolled_back_and_reported_once_as_orphan(tmp_path):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    service, _, plan = _planned_durable_service(db_path, ledger_path)

    # Let the fixture providers write, then fail at the first durable execution record.
    service.gateway.authorize = lambda action, context: None
    original_put = service.store.put

    def fail_execution_record(kind, key, record):
        if kind == "execution":
            raise RuntimeError("execution record unavailable")
        return original_put(kind, key, record)

    service.store.put = fail_execution_record
    with pytest.raises(RuntimeError, match="execution record"):
        service.execute(plan.id, service.world.version, "user")

    assert service.world.version == 1
    assert service.executions == {}
    assert len(list(service.gateway.providers["mock_transfer"].ledger.items())) == 1
    assert SqliteStore(db_path).load().executions == {}

    restarted = _durable_service(db_path, ledger_path)
    orphans = restarted.ledger_orphan_entries()
    assert len(orphans) == 4
    assert all(item["type"] == "ledger.orphan_detected" for item in orphans)
    orphan_audit_count = sum(item["type"] == "ledger.orphan_detected" for item in restarted.audit)

    restarted_again = _durable_service(db_path, ledger_path)
    assert len(restarted_again.ledger_orphan_entries()) == len(orphans)
    assert (
        sum(item["type"] == "ledger.orphan_detected" for item in restarted_again.audit)
        == orphan_audit_count
    )
