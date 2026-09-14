import json

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.demo import delay_event, demo_world
from cascade.observability.events import MAX_QUEUED, MAX_SUBSCRIBERS, EventStream
from cascade.service import CascadeService
from cascade.tools.demo import demo_gateway


@pytest.fixture
def listening():
    service = CascadeService(demo_world(), demo_gateway())
    return service, service.stream.subscribe()


def types(queue):
    return [notification.type for notification in queue]


def test_an_event_reaches_every_subscriber():
    stream = EventStream()
    first, second = stream.subscribe(), stream.subscribe()
    stream.publish("state.changed", 3, reason="test")
    assert types(first) == types(second) == ["state.changed"]
    assert first[0].world_version == 3
    assert first[0].detail == {"reason": "test"}
    stream.unsubscribe(first)
    stream.publish("state.changed", 4)
    assert len(first) == 1 and len(second) == 2


def test_unsubscribe_removes_the_exact_idle_queue():
    stream = EventStream()
    first, second = stream.subscribe(), stream.subscribe()
    stream.unsubscribe(second)
    stream.publish("state.changed", 3, reason="identity")
    assert types(first) == ["state.changed"]
    assert not second


def test_a_slow_reader_loses_the_oldest_not_the_newest():
    stream = EventStream()
    queue = stream.subscribe()
    for _ in range(MAX_QUEUED + 5):
        stream.publish("state.changed", 0)
    assert len(queue) == MAX_QUEUED
    # Identifiers are monotonic, so a reader can tell it missed something.
    assert queue[-1].id > queue[0].id
    assert queue[-1].id == MAX_QUEUED + 5


def test_subscribers_are_capped():
    stream = EventStream()
    assert all(stream.subscribe() is not None for _ in range(MAX_SUBSCRIBERS))
    assert stream.subscribe() is None


def test_the_wire_format_is_server_sent_events():
    stream = EventStream()
    queue = stream.subscribe()
    stream.publish("incident.created", 1, incident_id="inc_1")
    frame = queue[0].encode()
    assert frame.startswith("id: 1\nevent: incident.created\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["detail"]["incident_id"] == "inc_1"
    assert payload["world_version"] == 1


def test_the_lifecycle_is_announced_in_order(listening):
    service, queue = listening
    incident = service.ingest(delay_event()).incident
    planning = service.plan(incident.id, service.world.version)
    plan = min(planning.candidates, key=lambda p: p.additional_cost)
    request = service.execute(plan.id, service.world.version, "user").approval_request
    service.approve(
        request.id, "user", tuple(i.action_id for i in request.items), request.total_amount
    )
    service.execute(plan.id, service.world.version, "user")
    announced = types(queue)
    assert announced[:3] == ["state.changed", "incident.created", "recovery.plan.created"]
    assert "approval.required" in announced
    assert announced.count("approval.decided") == 1
    assert "action.completed" in announced
    assert "incident.resolved" in announced
    assert announced[-1] == "state.changed"


def test_events_describe_change_without_carrying_state(listening):
    service, queue = listening
    service.ingest(delay_event())
    # A reader still has to fetch state through the versioned endpoints.
    for notification in queue:
        assert "world" not in notification.detail
        assert "commitments" not in notification.detail
        assert notification.world_version == service.world.version


def test_the_endpoint_refuses_to_oversubscribe():
    app = create_app()
    with TestClient(app) as client:
        service = app.state.service
        held = [service.stream.subscribe() for _ in range(MAX_SUBSCRIBERS)]
        assert all(queue is not None for queue in held)
        assert client.get("/v1/stream").status_code == 503
        for queue in held:
            service.stream.unsubscribe(queue)
