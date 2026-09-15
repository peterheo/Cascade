from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.connectors.calendar import (
    CalendarEvent,
    ICloudCalendarClient,
    ICloudCalendarProvider,
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
    assert store.load().calendar_undos["step-no-header"]["new_etag"] == '"2"'
    assert ledger.get("icloud", "step-no-header")["new_etag"] == '"2"'


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
    with TestClient(create_app()) as client:
        assert client.get("/v1/state").json()["world"]["commitments"] == []
    with TestClient(create_app()) as client:
        assert client.get("/v1/state").json()["world"]["commitments"] == []
