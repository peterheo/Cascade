import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import STATIC, create_app
from apps.api.simulation import create_app as create_simulation_app

SCRIPT = (STATIC / "app.js").read_text()


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_the_product_surface_is_served_by_the_api(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<title>Cascade</title>" in page.text
    for asset in ("/static/app.js", "/static/styles.css"):
        assert client.get(asset).status_code == 200


def test_the_ui_ships_without_a_build_step_or_a_remote_dependency():
    markup = (STATIC / "index.html").read_text()
    assert "http://" not in markup and "https://" not in markup
    assert not list(Path(STATIC).glob("**/node_modules"))


def segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def matches(route: str, used: str) -> bool:
    left, right = segments(route), segments(used)
    if len(left) != len(right):
        return False
    return all(a.startswith("{") or a == b for a, b in zip(left, right, strict=True))


def test_every_endpoint_the_ui_calls_exists(client):
    """The UI and the API drift apart silently otherwise."""
    used = {
        re.sub(r"\$\{[^}]+\}", "id", path)
        for path in re.findall(r"[\"'`](/v1/[^\"'`\s]*)[\"'`]", SCRIPT)
    }
    used.discard("/v1/auth/")
    assert {"/v1/auth/login", "/v1/auth/logout", "/v1/auth/me"} <= used
    assert len(used) >= 8
    routes = [route.path for route in client.app.routes if hasattr(route, "path")]
    missing = [path for path in used if not any(matches(r, path) for r in routes)]
    assert not missing, missing


def test_the_ui_never_calls_a_mutating_endpoint_by_accident(client):
    """Writes happen only on the paths the approval flow is built around."""
    mutating = set(re.findall(r"method:\s*\"POST\"", SCRIPT))
    assert mutating, "the UI must reach mutations through explicit POSTs"
    assert "DELETE" not in SCRIPT
    assert 'method: "PATCH"' in SCRIPT and "/v1/privacy" in SCRIPT
    # The approval total is echoed back from the server, never recomputed client side.
    assert "acknowledged_amount: state.approval.total_amount" in SCRIPT


def test_the_natural_language_path_never_applies_without_confirmation():
    # The preview call omits `apply`, so the server defaults it to false; the change
    # reaches state only through the separate confirm endpoint.
    assert "apply: true" not in SCRIPT
    assert "/v1/events/text" in SCRIPT
    assert "/confirm" in SCRIPT


def test_a_missing_model_does_not_block_the_deterministic_path():
    assert "The deterministic simulator still works." in SCRIPT
    assert "/v1/reasoning/status" in SCRIPT


def test_privacy_endpoint_exists_in_both_apps():
    for factory in (create_app, create_simulation_app):
        with TestClient(factory()) as app:
            assert app.get("/v1/privacy").status_code == 200


def test_the_ui_can_choose_a_recovery_template():
    assert "/v1/skills" in SCRIPT
    # Auto-matching stays the default: a template is only sent when one is chosen.
    assert "state.skill ? { skill: state.skill } : {}" in SCRIPT
    assert "opt-in" in SCRIPT


def test_the_ui_follows_the_event_stream_without_trusting_it():
    assert 'new EventSource("/v1/stream", { withCredentials: true })' in SCRIPT
    # Notifications only trigger a read; the page never renders from the event body.
    assert "refresh()" in SCRIPT
    assert "event.data" not in SCRIPT


def test_the_ui_reads_times_without_rewriting_them():
    # Converting to the viewer's timezone would misreport the itinerary's own clock.
    assert "toLocaleTimeString" not in SCRIPT
    assert "String(value).slice(11, 16)" in SCRIPT
