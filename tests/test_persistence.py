import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.api.simulation import create_app as create_simulation_app
from cascade.demo import delay_event, demo_world
from cascade.memory.preferences import preference
from cascade.memory.resolutions import record_dismissal
from cascade.persistence import MemoryStore, SqliteStore
from cascade.service import CascadeService, ConflictError


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
