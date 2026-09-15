import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.connectors.calendar import (
    CalendarError,
    CalendarEvent,
    CalendarSnapshot,
    CalendarWatcher,
    ICloudCalendarClient,
    ICloudCalendarProvider,
    _validate_discovered_url,
)
from cascade.demo import delay_event, demo_world
from cascade.domain.models import Commitment, SourceRef, World
from cascade.persistence import MemoryStore, SqliteStore
from cascade.service import CascadeService, ConflictError
from cascade.tools.gateway import (
    ExecutionContext,
    PostCondition,
    ToolGateway,
    build_action,
)
from cascade.tools.ledger import SqliteLedger

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1@example.test
SUMMARY:Doctor
DTSTART:20260915T120000Z
DTEND:20260915T130000Z
END:VEVENT
END:VCALENDAR
"""


def calendar_action(*, key="calendar-step", start_hour=14, end_hour=15):
    return build_action(
        plan_id="plan",
        commitment_id="ical_one",
        provider="icloud",
        operation="reschedule",
        idempotency_key=key,
        postcondition=PostCondition(
            commitment_present=True,
            start_at=datetime(2026, 9, 15, start_hour, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, end_hour, tzinfo=UTC),
        ),
        description="Move meeting",
    )


def test_caldav_discovery_imports_timed_events_and_counts_skips():
    responses = {
        "https://caldav.icloud.com/": (
            '<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>'
            "<current-user-principal><href>/principal</href></current-user-principal>"
            "</prop></propstat></response></multistatus>"
        ),
        "https://caldav.icloud.com/principal": (
            '<multistatus xmlns="DAV:"><response><href>/principal</href><propstat><prop>'
            '<calendar-home-set xmlns="urn:ietf:params:xml:ns:caldav"><href>/home/</href>'
            "</calendar-home-set></prop></propstat></response></multistatus>"
        ),
        "https://caldav.icloud.com/home/": (
            '<multistatus xmlns="DAV:"><response><href>/home/cascade/</href><propstat><prop>'
            "<displayname>Cascade Trip</displayname><resourcetype/>"
            "</prop></propstat></response></multistatus>"
        ),
    }
    all_day = ICS.replace("DTSTART:20260915T120000Z", "DTSTART;VALUE=DATE:20260915").replace(
        "DTEND:20260915T130000Z", "DTEND;VALUE=DATE:20260916"
    )
    recurring = ICS.replace("END:VEVENT", "RRULE:FREQ=WEEKLY\nEND:VEVENT")

    def handler(request):
        if request.method == "PROPFIND":
            return httpx.Response(207, text=responses[str(request.url)], request=request)
        body = (
            '<multistatus xmlns="DAV:">'
            f'<response><href>/home/cascade/one.ics</href><propstat><prop><getetag>"1"</getetag><calendar-data>{ICS}</calendar-data></prop></propstat></response>'
            f'<response><href>/home/cascade/day.ics</href><propstat><prop><getetag>"2"</getetag><calendar-data>{all_day}</calendar-data></prop></propstat></response>'
            f'<response><href>/home/cascade/recur.ics</href><propstat><prop><getetag>"3"</getetag><calendar-data>{recurring}</calendar-data></prop></propstat></response>'
            "</multistatus>"
        )
        return httpx.Response(207, text=body, request=request)

    client = ICloudCalendarClient(
        "user@example.test",
        "app-password",
        base_url="https://caldav.icloud.com/",
        transport=httpx.MockTransport(handler),
    )
    snapshot = client.list_events()
    assert snapshot.found and snapshot.imported == 1
    assert snapshot.skipped_all_day == 1
    assert snapshot.skipped_recurring == 1
    event = snapshot.events[0]
    assert event.commitment.id.startswith("ical_")
    assert set(event.link) == {"calendar_href", "href", "event_uid", "etag"}


def test_discovered_icloud_urls_allow_only_https_default_port_and_443():
    normalized = _validate_discovered_url("https://p01-caldav.icloud.com:443/principal")
    assert normalized == "https://p01-caldav.icloud.com/principal"
    with pytest.raises(CalendarError):
        _validate_discovered_url("https://p01-caldav.icloud.com:8443/principal")
    with pytest.raises(CalendarError):
        _validate_discovered_url("http://evil.test/principal")


def test_discovery_refuses_evil_principal_before_following_it():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == "https://caldav.icloud.com/":
            return httpx.Response(
                207,
                text=(
                    '<multistatus xmlns="DAV:"><response><propstat><prop>'
                    "<current-user-principal><href>http://evil.test/</href></current-user-principal>"
                    "</prop></propstat></response></multistatus>"
                ),
                request=request,
            )
        raise AssertionError("unsafe host was contacted")

    client = ICloudCalendarClient(
        "u",
        "p",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CalendarError, match="unsafe URL"):
        client.list_events()
    assert requested == ["https://caldav.icloud.com/"]


def test_calendar_sync_is_idempotent_and_personal_mode_blocks_demo(monkeypatch):
    service = CascadeService(World(commitments=(), intents=(), dependencies=(), deadlines=()))
    commitment = Commitment(
        id="ical_one",
        kind="meeting",
        title="Meeting",
        intent_id="intent_ical_one",
        start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
        source=SourceRef(source="icloud_calendar", external_id="one"),
    )
    event = CalendarEvent(
        commitment,
        {
            "calendar_href": "https://caldav.test/home/cascade/",
            "href": "https://caldav.test/home/cascade/one.ics",
            "event_uid": "one",
            "etag": '"1"',
        },
        metadata={"STATUS": "CONFIRMED", "LOCATION": "secret", "DESCRIPTION": "private"},
    )
    assert service.sync_calendar((event,), persist_event_text=False)["changed"] is True
    assert service.calendar_metadata["ical_one"] == {"STATUS": "CONFIRMED"}
    version = service.world.version
    assert service.sync_calendar((event,))["changed"] is False
    assert service.world.version == version
    monkeypatch.setenv("CASCADE_GATEWAY_WORLD", "empty")
    with TestClient(create_app()) as client:
        assert client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 409


def test_calendar_provider_uses_if_match_and_supports_undo():
    state = {"body": ICS, "etag": '"1"'}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        assert request.headers["if-match"] == state["etag"]
        state["body"] = request.content.decode()
        state["etag"] = '"2"'
        return httpx.Response(204, headers={"ETag": state["etag"]}, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    provider = ICloudCalendarProvider(client, MemoryStore(), links)
    action = build_action(
        plan_id="plan",
        commitment_id="ical_one",
        provider="icloud",
        operation="reschedule",
        idempotency_key="step-1",
        postcondition=PostCondition(
            commitment_present=True,
            start_at=datetime(2026, 9, 15, 14, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 15, tzinfo=UTC),
        ),
        description="Move meeting",
    )
    context = ExecutionContext(
        plan_id="plan", world_version=0, actor="owner", approved_action_ids=(action.id,)
    )
    blocked = ToolGateway({"icloud": provider}).execute(
        action,
        ExecutionContext(plan_id="plan", world_version=0, actor="owner"),
    )
    assert blocked.decision == "REQUIRES_APPROVAL"
    result = ToolGateway({"icloud": provider}).execute(action, context).result
    assert result and result.success and result.verified
    assert provider.undo("step-1")["status"] == "UNDONE"


def test_calendar_provider_refuses_conflict_and_unknown_timezone_before_put():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=ICS, headers={"ETag": '"1"'}, request=request)
        return httpx.Response(412, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    provider = ICloudCalendarProvider(client, MemoryStore(), links)
    action = build_action(
        plan_id="plan",
        commitment_id="ical_one",
        provider="icloud",
        operation="reschedule",
        idempotency_key="conflict",
        postcondition=PostCondition(
            commitment_present=True,
            start_at=datetime(2026, 9, 15, 14, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 15, tzinfo=UTC),
        ),
        description="Move meeting",
    )
    result = provider.apply(action)
    assert not result.success and not result.side_effect
    assert calls == ["GET", "PUT"]

    unknown = ICS.replace(
        "DTSTART:20260915T120000Z", "DTSTART;TZID=Romance Standard Time:20260915T120000"
    ).replace("DTEND:20260915T130000Z", "DTEND;TZID=Romance Standard Time:20260915T130000")
    client.get = lambda _href: (unknown, '"1"')
    result = provider.apply(action.model_copy(update={"idempotency_key": "unknown-tz"}))
    assert not result.success and not result.side_effect and result.detail == "unsupported_timezone"


def test_calendar_duration_write_keeps_equal_duration_and_replaces_changed_duration():
    duration_ics = ICS.replace("DTEND:20260915T130000Z\n", "DURATION:PT1H\n")
    state = {"body": duration_ics, "etag": '"1"'}
    puts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        puts.append(request.content.decode())
        state["body"] = request.content.decode()
        state["etag"] = f'"{len(puts) + 1}"'
        return httpx.Response(204, headers={"ETag": state["etag"]}, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    provider = ICloudCalendarProvider(client, MemoryStore(), links)
    assert provider.apply(calendar_action(key="duration-equal")).success
    assert "DURATION:PT1H" in puts[0] and "DTEND" not in puts[0]

    state["body"] = duration_ics
    state["etag"] = '"3"'
    changed = calendar_action(key="duration-changed", end_hour=16)
    assert provider.apply(changed).success
    assert "DURATION" not in puts[1] and "DTEND:20260915T160000Z" in puts[1]


def test_calendar_known_iana_tzid_round_trips_without_utc_wall_time_corruption():
    tz_ics = ICS.replace(
        "DTSTART:20260915T120000Z\nDTEND:20260915T130000Z",
        "DTSTART;TZID=America/New_York:20260915T120000\n"
        "DTEND;TZID=America/New_York:20260915T130000",
    )
    state = {"body": tz_ics, "etag": '"1"'}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        state["body"] = request.content.decode()
        state["etag"] = '"2"'
        return httpx.Response(204, headers={"ETag": state["etag"]}, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    provider = ICloudCalendarProvider(client, MemoryStore(), links)
    action = calendar_action(key="iana", start_hour=14, end_hour=15)
    assert provider.apply(action).success
    assert "TZID=America/New_York" in state["body"]
    observed = provider.verify(action)
    assert observed.success
    assert observed.observed_start_at.tzinfo == ZoneInfo("America/New_York")


def test_calendar_provider_refreshes_etag_when_put_has_no_etag(tmp_path):
    state = {"body": ICS, "etag": '"1"'}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        state["body"] = request.content.decode()
        state["etag"] = '"2"'
        return httpx.Response(204, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    store = SqliteStore(tmp_path / "calendar.db")
    ledger = SqliteLedger(tmp_path / "ledger.db")
    provider = ICloudCalendarProvider(client, store, links, ledger)
    action = build_action(
        plan_id="plan",
        commitment_id="ical_one",
        provider="icloud",
        operation="reschedule",
        idempotency_key="step-no-header",
        postcondition=PostCondition(
            commitment_present=True,
            start_at=datetime(2026, 9, 15, 14, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 15, tzinfo=UTC),
        ),
        description="Move meeting",
    )
    context = ExecutionContext(
        plan_id="plan", world_version=0, actor="owner", approved_action_ids=(action.id,)
    )
    result = ToolGateway({"icloud": provider}).execute(action, context).result
    assert result and result.success
    assert links["ical_one"]["etag"] == '"2"'
    loaded = store.load()
    assert loaded is not None
    assert loaded.calendar_links["ical_one"]["etag"] == '"2"'
    assert loaded.calendar_undos["step-no-header"]["new_etag"] == '"2"'
    assert ledger.get("icloud", "step-no-header")["new_etag"] == '"2"'


def test_calendar_412_closes_intent_and_allows_retry_with_same_key(tmp_path):
    state = {"body": ICS, "etag": '"1"', "put_calls": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        state["put_calls"] += 1
        if state["put_calls"] == 1:
            return httpx.Response(412, request=request)
        state["body"] = request.content.decode()
        state["etag"] = '"2"'
        return httpx.Response(204, headers={"ETag": state["etag"]}, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    ledger = SqliteLedger(tmp_path / "ledger.db")
    provider = ICloudCalendarProvider(client, SqliteStore(tmp_path / "state.db"), links, ledger)
    action = calendar_action(key="retry-conflict")
    first = provider.apply(action)
    assert first.success is False and first.side_effect is False
    refused = ledger.get("icloud", action.idempotency_key)
    assert refused and refused["state"] == "refused" and refused["pending"] is False
    second = provider.apply(action)
    assert second.success is True
    assert state["put_calls"] == 2


def test_refused_calendar_write_is_not_an_orphan_after_restart(tmp_path):
    state_store = SqliteStore(tmp_path / "state.db")
    ledger = SqliteLedger(tmp_path / "ledger.db")
    ledger.replace(
        "icloud",
        "refused",
        {
            "reference": "https://caldav.test/home/one.ics",
            "pending": False,
            "state": "refused",
            "put_started": False,
        },
    )
    client = ICloudCalendarClient("u", "p", base_url="https://caldav.test/home/")
    client.calendar_href = "https://caldav.test/home/"
    provider = ICloudCalendarProvider(client, state_store, {}, ledger)
    service = CascadeService(
        World(commitments=(), intents=(), dependencies=(), deadlines=()),
        gateway=ToolGateway({"icloud": provider}),
        store=state_store,
    )
    assert service.ledger_orphan_entries() == ()


def test_calendar_sync_discards_snapshot_after_world_change():
    service = CascadeService(World(commitments=(), intents=(), dependencies=(), deadlines=()))
    commitment = Commitment(
        id="ical_race",
        kind="meeting",
        title="Race",
        intent_id="intent_ical_race",
        start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
        source=SourceRef(source="icloud_calendar", external_id="race"),
    )
    event = CalendarEvent(
        commitment,
        {
            "calendar_href": "https://caldav.test/home/cascade/",
            "href": "https://caldav.test/home/cascade/race.ics",
            "event_uid": "race",
            "etag": '"1"',
        },
    )
    service.sync_calendar((event,))
    version = service.world.version
    result = service.sync_calendar((event,), sync_started_version=version - 1)
    assert result["stale"] is True
    assert service.world.version == version


def test_calendar_sync_reordering_events_does_not_bump_version():
    service = CascadeService(World(commitments=(), intents=(), dependencies=(), deadlines=()))
    first = CalendarEvent(
        Commitment(
            id="ical_a",
            kind="meeting",
            title="A",
            intent_id="intent_ical_a",
            start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
            source=SourceRef(source="icloud_calendar", external_id="a"),
        ),
        {
            "calendar_href": "https://caldav.test/home/",
            "href": "https://caldav.test/home/a.ics",
            "event_uid": "a",
            "etag": '"1"',
        },
    )
    second = replace(
        first,
        commitment=first.commitment.model_copy(
            update={
                "id": "ical_b",
                "intent_id": "intent_ical_b",
                "title": "B",
                "source": SourceRef(source="icloud_calendar", external_id="b"),
            }
        ),
        link={**first.link, "href": "https://caldav.test/home/b.ics", "event_uid": "b"},
    )
    assert service.sync_calendar((first, second))["changed"]
    version = service.world.version
    result = service.sync_calendar((second, first))
    assert result["changed"] is False
    assert service.world.version == version


def test_calendar_watcher_counts_discarded_snapshots():
    service = CascadeService(World(commitments=(), intents=(), dependencies=(), deadlines=()))

    class SnapshotClient:
        def list_events(self):
            return CalendarSnapshot(found=True)

    service.sync_calendar = lambda *args, **kwargs: {"stale": True}
    watcher = CalendarWatcher(SnapshotClient(), service, poll_seconds=60)
    assert watcher.sync_once()["discarded_snapshots"] == 1


def test_calendar_undo_api_requires_verified_executed_step(monkeypatch):
    monkeypatch.setenv("CASCADE_ICLOUD_USER", "u@example.test")
    monkeypatch.setenv("CASCADE_ICLOUD_APP_PASSWORD", "app-password")
    app = create_app()
    service = app.state.service
    action = SimpleNamespace(provider="icloud", idempotency_key="undo-api")
    call = SimpleNamespace(action=action)
    service.executions["exec-failed"] = SimpleNamespace(
        steps=(SimpleNamespace(id="step", status="FAILED", call=call),)
    )
    service.executions["exec-unverified"] = SimpleNamespace(
        steps=(SimpleNamespace(id="step", status="EXECUTED", call=call),)
    )
    with TestClient(app) as client:
        assert client.post("/v1/calendar/undo/exec-failed/step").status_code == 409
        provider = app.state.service.gateway.providers["icloud"]
        provider.undo = lambda _key: (_ for _ in ()).throw(
            CalendarError("nothing verified to undo")
        )
        assert client.post("/v1/calendar/undo/exec-unverified/step").status_code == 409


def test_calendar_undo_api_returns_verified_undo(monkeypatch):
    monkeypatch.setenv("CASCADE_ICLOUD_USER", "u@example.test")
    monkeypatch.setenv("CASCADE_ICLOUD_APP_PASSWORD", "app-password")
    app = create_app()
    service = app.state.service
    action = SimpleNamespace(provider="icloud", idempotency_key="undo-api")
    call = SimpleNamespace(action=action)
    service.executions["exec"] = SimpleNamespace(
        steps=(SimpleNamespace(id="step", status="EXECUTED", call=call),)
    )
    provider = app.state.service.gateway.providers["icloud"]
    outcome = {"status": "UNDONE", "href": "https://caldav.test/home/one.ics"}
    provider.undo = lambda _key: outcome
    service.record_calendar_undo = lambda *_args: None
    with TestClient(app) as client:
        response = client.post("/v1/calendar/undo/exec/step")
    assert response.status_code == 200
    assert response.json() == outcome


def test_calendar_undo_api_real_execute_and_provider_uses_if_match(tmp_path, monkeypatch):
    monkeypatch.setenv("CASCADE_DB", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("CASCADE_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("CASCADE_ICLOUD_USER", "u@example.test")
    monkeypatch.setenv("CASCADE_ICLOUD_APP_PASSWORD", "app-password")
    app = create_app()
    watcher = app.state.calendar_watcher
    mail_watcher = app.state.mail_watcher
    if watcher is not None:
        watcher._next_poll = time.monotonic() + 3600
    if mail_watcher is not None:
        mail_watcher._next_poll = time.monotonic() + 3600
    service = app.state.service
    service.gateway.sandbox = None
    incident = service.ingest(delay_event()).incident
    planning = service.plan(incident.id, service.world.version)
    plan = planning.candidates[0]
    option = next(item for item in plan.actions if item.resolution != "PRESERVED")
    current = next(item for item in service.world.commitments if item.id == option.commitment_id)
    href = "https://caldav.test/home/event.ics"
    service.calendar_links[current.id] = {
        "calendar_href": "https://caldav.test/home/",
        "href": href,
        "event_uid": "event-1@example.test",
        "etag": '"1"',
    }
    body = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        "UID:event-1@example.test\nSUMMARY:Calendar item\n"
        f"DTSTART;TZID=Europe/Paris:{current.start_at.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND;TZID=Europe/Paris:{current.end_at.strftime('%Y%m%dT%H%M%S')}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    state = {"body": body, "etag": '"1"'}
    puts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        assert request.headers["if-match"] == state["etag"]
        puts.append((request.content.decode(), request.headers["if-match"]))
        state["body"] = request.content.decode()
        state["etag"] = f'"{len(puts) + 1}"'
        return httpx.Response(204, headers={"ETag": state["etag"]}, request=request)

    provider = service.gateway.providers["icloud"]
    provider.client.calendar_href = "https://caldav.test/home/"
    provider.client.transport = httpx.MockTransport(handler)
    with TestClient(app) as client:
        version = service.world.version
        pending = client.post(
            f"/v1/recovery-plans/{plan.id}/execute", json={"expected_version": version}
        ).json()
        assert pending["status"] == "AWAITING_APPROVAL"
        request = pending["approval_request"]
        approved = client.post(
            f"/v1/approvals/{request['id']}/approve",
            json={
                "approved_action_ids": [item["action_id"] for item in request["items"]],
                "acknowledged_amount": request["total_amount"],
            },
        )
        assert approved.status_code == 200
        executed = client.post(
            f"/v1/recovery-plans/{plan.id}/execute", json={"expected_version": version}
        ).json()
        step = next(
            item
            for item in executed["steps"]
            if item["call"] and item["call"]["action"]["provider"] == "icloud"
        )
        assert step["status"] == "EXECUTED"
        undone = client.post(f"/v1/calendar/undo/{executed['id']}/{step['id']}")
        assert undone.status_code == 200
    assert len(puts) >= 2
    assert puts[-1][1] == '"2"'


def test_calendar_sync_stales_approval_via_api(monkeypatch):
    app = create_app()
    service = app.state.service
    with TestClient(app) as client:
        incident = client.post("/v1/demo/scenarios/flight_delay/inject").json()["incident"]
        planned = client.post(
            f"/v1/incidents/{incident['id']}/plan", json={"expected_version": 1}
        ).json()
        pending = client.post(
            f"/v1/recovery-plans/{planned['candidates'][0]['id']}/execute",
            json={"expected_version": 1},
        ).json()
        commitment = Commitment(
            id="ical_api_stale",
            kind="meeting",
            title="Imported",
            intent_id="intent_ical_api_stale",
            start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
            source=SourceRef(source="icloud_calendar", external_id="api-stale"),
        )
        service.sync_calendar(
            (
                CalendarEvent(
                    commitment,
                    {
                        "calendar_href": "https://caldav.test/home/",
                        "href": "https://caldav.test/home/api-stale.ics",
                        "event_uid": "api-stale",
                        "etag": '"1"',
                    },
                ),
            )
        )
        response = client.post(
            f"/v1/approvals/{pending['approval_request']['id']}/approve",
            json={
                "approved_action_ids": [
                    item["action_id"] for item in pending["approval_request"]["items"]
                ],
                "acknowledged_amount": pending["approval_request"]["total_amount"],
            },
        )
    assert response.status_code == 409


def test_calendar_link_and_undo_rows_reload_on_service_restart(tmp_path):
    path = tmp_path / "calendar.db"
    event = CalendarEvent(
        Commitment(
            id="ical_restart",
            kind="meeting",
            title="Restart",
            intent_id="intent_ical_restart",
            start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
            end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
            source=SourceRef(source="icloud_calendar", external_id="restart"),
        ),
        {
            "calendar_href": "https://caldav.test/home/",
            "href": "https://caldav.test/home/restart.ics",
            "event_uid": "restart",
            "etag": '"7"',
        },
    )
    service_a = CascadeService(
        World(commitments=(), intents=(), dependencies=(), deadlines=()),
        store=SqliteStore(path),
    )
    service_a.sync_calendar((event,))
    undo = {
        "href": event.link["href"],
        "prior_ics": ICS,
        "prior_etag": '"7"',
        "new_etag": '"8"',
        "commitment_id": event.commitment.id,
    }
    service_a.store.put("calendar_undo", "restart-undo", undo)
    service_b = CascadeService(
        World(commitments=(), intents=(), dependencies=(), deadlines=()),
        store=SqliteStore(path),
    )
    assert service_b.calendar_links[event.commitment.id] == event.link
    assert service_b.calendar_undos["restart-undo"] == undo


def test_calendar_orphan_is_reported_by_api_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "gateway.db"
    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setenv("CASCADE_DB", str(db_path))
    monkeypatch.setenv("CASCADE_LEDGER_DB", str(ledger_path))
    monkeypatch.setenv("CASCADE_GATEWAY_WORLD", "empty")
    monkeypatch.setenv("CASCADE_ICLOUD_USER", "u@example.test")
    monkeypatch.setenv("CASCADE_ICLOUD_APP_PASSWORD", "app-password")
    SqliteLedger(ledger_path).replace(
        "icloud",
        "orphan-after-restart",
        {
            "reference": "https://caldav.test/home/orphan.ics",
            "pending": True,
            "put_started": True,
            "state": "pending",
        },
    )
    first_app = create_app()
    if first_app.state.calendar_watcher is not None:
        first_app.state.calendar_watcher._next_poll = time.monotonic() + 3600
    if first_app.state.mail_watcher is not None:
        first_app.state.mail_watcher._next_poll = time.monotonic() + 3600
    with TestClient(create_app()) as client:
        orphans = client.get("/v1/ledger/orphans").json()
    assert any(item["idempotency_key"] == "orphan-after-restart" for item in orphans)


def test_calendar_write_after_put_readback_failure_is_reconcilable(tmp_path):
    state = {"body": ICS, "etag": '"1"', "gets": 0}

    def handler(request):
        if request.method == "GET":
            state["gets"] += 1
            if state["gets"] > 1:
                return httpx.Response(503, request=request)
            return httpx.Response(
                200, text=state["body"], headers={"ETag": state["etag"]}, request=request
            )
        state["body"] = request.content.decode()
        state["etag"] = '"2"'
        return httpx.Response(204, request=request)

    client = ICloudCalendarClient(
        "u", "p", base_url="https://caldav.test/home/", transport=httpx.MockTransport(handler)
    )
    client.calendar_href = "https://caldav.test/home/"
    links = {
        "ical_one": {
            "calendar_href": client.calendar_href,
            "href": "https://caldav.test/home/one.ics",
            "event_uid": "event-1@example.test",
            "etag": '"1"',
        }
    }
    ledger = SqliteLedger(tmp_path / "ledger.db")
    provider = ICloudCalendarProvider(client, SqliteStore(tmp_path / "state.db"), links, ledger)
    action = calendar_action(key="post-put-failure")
    call = ToolGateway({"icloud": provider}).execute(
        action,
        ExecutionContext(
            plan_id="plan", world_version=0, actor="owner", approved_action_ids=(action.id,)
        ),
    )
    assert call.result and not call.result.success and call.result.side_effect
    record = ledger.get("icloud", "post-put-failure")
    assert record and record["put_started"] is True and record["pending"] is True


def test_calendar_sync_makes_existing_approval_stale():
    service = CascadeService(demo_world())
    incident = service.ingest(delay_event()).incident
    assert incident is not None
    planning = service.plan(incident.id, service.world.version)
    pending = service.execute(planning.candidates[0].id, service.world.version, "owner")
    assert pending.approval_request is not None
    commitment = Commitment(
        id="ical_stale",
        kind="meeting",
        title="Imported",
        intent_id="intent_ical_stale",
        start_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        end_at=datetime(2026, 9, 15, 13, tzinfo=UTC),
        source=SourceRef(source="icloud_calendar", external_id="stale"),
    )
    event = CalendarEvent(
        commitment,
        {
            "calendar_href": "https://caldav.test/home/cascade/",
            "href": "https://caldav.test/home/cascade/stale.ics",
            "event_uid": "stale",
            "etag": '"1"',
        },
    )
    service.sync_calendar((event,))
    with pytest.raises(ConflictError):
        service.approve(
            pending.approval_request.id,
            "owner",
            tuple(item.action_id for item in pending.approval_request.items),
            pending.approval_request.total_amount,
        )


def test_empty_mode_clears_persisted_demo_records(tmp_path, monkeypatch):
    db_path = tmp_path / "gateway.db"
    monkeypatch.setenv("CASCADE_DB", str(db_path))
    monkeypatch.delenv("CASCADE_GATEWAY_WORLD", raising=False)
    with TestClient(create_app()) as client:
        assert client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 200
    monkeypatch.setenv("CASCADE_GATEWAY_WORLD", "empty")
    for _ in range(2):
        with TestClient(create_app()) as client:
            assert client.get("/v1/state").json()["world"]["commitments"] == []
        loaded = SqliteStore(db_path).load()
        assert loaded is not None
        assert loaded.events == {}
        assert loaded.incidents == []
        assert loaded.approvals == {}
        assert loaded.executions == {}
        connection = sqlite3.connect(db_path)
        try:
            records = connection.execute(
                "SELECT kind, COUNT(*) FROM records GROUP BY kind"
            ).fetchall()
            resolution_rows = connection.execute(
                "SELECT COUNT(*) FROM log WHERE stream = 'resolution'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert {kind for kind, _ in records}.isdisjoint(
            {"event", "incident", "plan", "approval", "execution"}
        )
        assert resolution_rows == 0
