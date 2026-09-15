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
from cascade.tools.gateway import ToolGateway, ToolResult
from cascade.tools.ledger import ProviderLedgerView, SqliteLedger


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
    assert service_b.ledger_orphan_entries() == ()
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


def test_calendar_write_gap_after_put_survives_service_rollback_and_restart(tmp_path):
    class CalendarFixtureProvider:
        kind = "icloud"

        def __init__(self, ledger):
            self.ledger = ProviderLedgerView(ledger, "icloud")

        def check(self, action):
            return ToolResult(
                success=True,
                provider="icloud",
                operation=action.operation,
                raw_result_ref="calendar-fixture:check",
                detail="calendar fixture available",
                observed_start_at=action.postcondition.start_at,
                observed_end_at=action.postcondition.end_at,
            )

        def apply(self, action):
            self.ledger[action.idempotency_key] = {
                "reference": f"calendar:{action.idempotency_key}",
                "operation": action.operation,
                "start_at": action.postcondition.start_at,
                "end_at": action.postcondition.end_at,
                "refund": 0,
                "pending": True,
                "put_started": True,
            }
            return ToolResult(
                success=True,
                provider="icloud",
                operation=action.operation,
                external_reference=f"calendar:{action.idempotency_key}",
                side_effect=True,
                raw_result_ref="calendar-fixture:apply",
                detail="calendar fixture write landed",
            )

        def verify(self, action):
            record = self.ledger[action.idempotency_key]
            return ToolResult(
                success=True,
                provider="icloud",
                operation=action.operation,
                external_reference=record["reference"],
                raw_result_ref="calendar-fixture:verify",
                detail="calendar fixture read back",
                observed_start_at=record["start_at"],
                observed_end_at=record["end_at"],
            )

    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(ledger_path)
    gateway = demo_gateway(ledger_path=ledger_path)
    gateway.sandbox = None
    gateway.providers["icloud"] = CalendarFixtureProvider(ledger)
    service = CascadeService(demo_world(), gateway=gateway, store=SqliteStore(db_path))
    incident = service.ingest(delay_event()).incident
    planning = service.plan(incident.id, service.world.version)
    plan = planning.candidates[0]
    linked = next(option for option in plan.actions if option.resolution != "PRESERVED")
    service.calendar_links[linked.commitment_id] = {
        "calendar_href": "https://caldav.test/home/",
        "href": "https://caldav.test/home/event.ics",
        "event_uid": "event-1",
        "etag": '"1"',
    }
    pending = service.execute(plan.id, service.world.version, "owner")
    assert pending.approval_request is not None
    service.approve(
        pending.approval_request.id,
        "owner",
        tuple(item.action_id for item in pending.approval_request.items),
        pending.approval_request.total_amount,
    )
    original_put = service.store.put

    def fail_execution_record(kind, key, record):
        if kind == "execution":
            raise RuntimeError("execution record unavailable")
        return original_put(kind, key, record)

    service.store.put = fail_execution_record
    with pytest.raises(RuntimeError, match="execution record"):
        service.execute(plan.id, service.world.version, "owner")
    assert len(service.executions) == 1
    assert next(iter(service.executions.values())).status == "AWAITING_APPROVAL"

    restarted = CascadeService(
        demo_world(),
        gateway=ToolGateway({"icloud": CalendarFixtureProvider(SqliteLedger(ledger_path))}),
        store=SqliteStore(db_path),
    )
    assert any(item["provider"] == "icloud" for item in restarted.ledger_orphan_entries())


def _approved_durable_execution(db_path, ledger_path):
    service, incident, plan = _planned_durable_service(db_path, ledger_path)
    pending = service.execute(plan.id, service.world.version, "user")
    request = pending.approval_request
    assert request is not None
    service.approve(
        request.id,
        "user",
        tuple(item.action_id for item in request.items),
        request.total_amount,
    )
    return service, incident, plan


@pytest.mark.parametrize("failure_point", ("resolution", "audit"))
def test_approval_flow_provider_gap_restores_memory_and_reports_orphans(tmp_path, failure_point):
    db_path = tmp_path / f"gateway-{failure_point}.db"
    ledger_path = tmp_path / f"ledger-{failure_point}.db"
    service, _, plan = _approved_durable_execution(db_path, ledger_path)
    before_world = service.world
    before_executions = service.executions.copy()
    before_approvals = service.approvals.copy()

    if failure_point == "resolution":
        service._record_resolution = lambda record: (_ for _ in ()).throw(
            RuntimeError("resolution record unavailable")
        )
        expected_message = "resolution record"
    else:
        service._record_audit = lambda body: (_ for _ in ()).throw(
            RuntimeError("audit record unavailable")
        )
        expected_message = "audit record"

    with pytest.raises(RuntimeError, match=expected_message):
        service.execute(plan.id, service.world.version, "user")

    assert service.world == before_world
    assert service.executions == before_executions
    assert service.approvals == before_approvals
    assert len(list(service.gateway.providers["mock_transfer"].ledger.items())) == 1
    persisted = SqliteStore(db_path).load()
    assert persisted is not None
    assert persisted.world == before_world
    assert persisted.executions == before_executions

    restarted = _durable_service(db_path, ledger_path)
    assert len(restarted.ledger_orphan_entries()) == 4
    assert sum(item["type"] == "ledger.orphan_detected" for item in restarted.audit) == 4

    restarted_again = _durable_service(db_path, ledger_path)
    assert len(restarted_again.ledger_orphan_entries()) == 4
    assert sum(item["type"] == "ledger.orphan_detected" for item in restarted_again.audit) == 4
