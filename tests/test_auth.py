import time

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.security.auth import hash_password


def configure_auth(monkeypatch, *, demo=True):
    monkeypatch.setenv("CASCADE_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CASCADE_OWNER_PASSWORD_HASH", hash_password("owner-password"))
    monkeypatch.setenv("CASCADE_SESSION_SECRET", "s" * 32)
    if demo:
        monkeypatch.setenv("CASCADE_DEMO_PASSWORD_HASH", hash_password("demo-password"))
    else:
        monkeypatch.delenv("CASCADE_DEMO_PASSWORD_HASH", raising=False)


def client(monkeypatch, *, demo=True):
    configure_auth(monkeypatch, demo=demo)
    return TestClient(create_app(), base_url="https://testserver")


def test_auth_required_fails_closed_without_owner_or_secret(monkeypatch):
    monkeypatch.setenv("CASCADE_AUTH_REQUIRED", "1")
    monkeypatch.delenv("CASCADE_OWNER_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("CASCADE_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CASCADE_AUTH_REQUIRED"):
        create_app()


def test_login_success_bad_password_and_rate_limit(monkeypatch):
    with client(monkeypatch) as test_client:
        assert test_client.post("/v1/auth/login", json={"password": "owner-password"}).json() == {
            "role": "owner"
        }
    with client(monkeypatch) as test_client:
        for _ in range(5):
            assert test_client.post("/v1/auth/login", json={"password": "wrong"}).status_code == 401
        assert test_client.post("/v1/auth/login", json={"password": "wrong"}).status_code == 429


def test_cookie_tamper_and_expiry_are_rejected(monkeypatch):
    with client(monkeypatch) as test_client:
        test_client.post("/v1/auth/login", json={"password": "owner-password"})
        token = test_client.cookies.get("cascade_session")
    assert token
    with client(monkeypatch) as tampered:
        tampered.headers["Cookie"] = f"cascade_session={token[:-1]}x"
        assert tampered.get("/v1/state").status_code == 401
    with client(monkeypatch) as expired:
        token = expired.app.state.auth._token("owner", int(time.time()) - 1)
        expired.headers["Cookie"] = f"cascade_session={token}"
        assert expired.get("/v1/state").status_code == 401


def test_demo_is_read_only_on_gateway_but_can_use_simulation(monkeypatch):
    with client(monkeypatch) as test_client:
        assert test_client.post("/v1/auth/login", json={"password": "demo-password"}).json() == {
            "role": "demo"
        }
        assert test_client.get("/v1/state").status_code == 200
        assert test_client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 403
        assert test_client.get("/simulation/v1/workspace").status_code == 200
        assert (
            test_client.post("/simulation/v1/demo/scenarios/flight_delay/inject").status_code == 200
        )
        assert (
            test_client.patch("/simulation/v1/privacy", json={"live_inference": False}).status_code
            == 403
        )


def test_owner_has_full_access_and_auth_disabled_mode_is_unchanged(monkeypatch):
    with client(monkeypatch) as test_client:
        test_client.post("/v1/auth/login", json={"password": "owner-password"})
        assert test_client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 200
    monkeypatch.delenv("CASCADE_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("CASCADE_OWNER_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("CASCADE_SESSION_SECRET", raising=False)
    with TestClient(create_app()) as test_client:
        assert test_client.get("/v1/state").status_code == 200
        assert test_client.post("/v1/demo/scenarios/flight_delay/inject").status_code == 200
