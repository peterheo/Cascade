"""Small, process-local session authentication for the demo applications."""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Final

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import Field

from cascade.domain.models import Record

COOKIE_NAME: Final = "cascade_session"
SESSION_SECONDS: Final = 12 * 60 * 60
FAILURE_WINDOW_SECONDS: Final = 60
FAILURE_LIMIT: Final = 5


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, n: int = 16_384, r: int = 8, p: int = 1) -> str:
    """Return a portable scrypt password hash."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if n < 2 or n & (n - 1) or r < 1 or p < 1:
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class Session:
    role: str
    exp: int


class LoginRequest(Record):
    password: str = Field(min_length=1, max_length=10_000)


class AuthManager:
    """Auth configuration and request dependency shared by one app instance."""

    def __init__(self, scope: str):
        self.scope = scope
        self.required = os.environ.get("CASCADE_AUTH_REQUIRED") == "1"
        self.owner_hash = os.environ.get("CASCADE_OWNER_PASSWORD_HASH")
        self.demo_hash = os.environ.get("CASCADE_DEMO_PASSWORD_HASH")
        secret = os.environ.get("CASCADE_SESSION_SECRET")
        if self.required and (not self.owner_hash or not secret or len(secret.encode()) < 32):
            raise RuntimeError(
                "CASCADE_AUTH_REQUIRED=1 requires CASCADE_OWNER_PASSWORD_HASH and "
                "a CASCADE_SESSION_SECRET of at least 32 bytes"
            )
        self.secret = (secret or "cascade-local-development-secret").encode("utf-8")
        self._failures: dict[str, tuple[float, int]] = {}

    def _client_key(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _record_failure(self, request: Request) -> bool:
        now = time.monotonic()
        key = self._client_key(request)
        started, count = self._failures.get(key, (now, 0))
        if now - started >= FAILURE_WINDOW_SECONDS:
            started, count = now, 0
        count += 1
        self._failures[key] = (started, count)
        return count > FAILURE_LIMIT

    def _clear_failures(self, request: Request) -> None:
        self._failures.pop(self._client_key(request), None)

    def _token(self, role: str, exp: int) -> str:
        payload = _b64encode(json.dumps({"role": role, "exp": exp}, separators=(",", ":")).encode())
        signature = hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest()
        return f"{payload}.{_b64encode(signature)}"

    def _session(self, token: str | None) -> Session | None:
        if not token:
            return None
        try:
            payload_text, signature_text = token.split(".", 1)
            expected = hmac.new(self.secret, payload_text.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(signature_text)):
                return None
            payload = json.loads(_b64decode(payload_text))
            role, exp = payload["role"], int(payload["exp"])
            if role not in {"owner", "demo"} or exp <= int(time.time()):
                return None
            return Session(role=role, exp=exp)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def current(self, request: Request) -> Session | None:
        if not self.required:
            return Session(role="owner", exp=2**63 - 1)
        return self._session(request.cookies.get(COOKIE_NAME))

    def _public(self, request: Request) -> bool:
        path = self._route_path(request)
        if (
            path == "/health"
            or path.startswith("/static/")
            or path in {"/", "/docs", "/openapi.json", "/redoc"}
        ):
            return True
        if path.startswith("/v1/auth/"):
            return True
        return not path.startswith("/v1/")

    def _route_path(self, request: Request) -> str:
        path = request.url.path
        # Starlette keeps the mount prefix on ``Request.url.path`` for a
        # mounted sub-application, even though route matching uses the stripped
        # path. Normalize it before applying the same policy to both apps.
        if self.scope == "simulation" and path.startswith("/simulation/"):
            return path[len("/simulation") :]
        return path

    def _allowed(self, request: Request, session: Session) -> bool:
        if self.scope == "gateway":
            return request.method in {"GET", "HEAD", "OPTIONS"} or session.role == "owner"
        # The simulation is the judge-facing demo surface. Both roles can use it;
        # only the process-wide privacy switch remains owner-only.
        if request.method == "PATCH" and self._route_path(request) == "/v1/privacy":
            return session.role == "owner"
        return True

    async def dependency(self, request: Request) -> Session | None:
        if not self.required or self._public(request):
            return self.current(request)
        session = self.current(request)
        if session is None:
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Session"}
            )
        if not self._allowed(request, session):
            raise HTTPException(403, "This action requires the owner role")
        return session

    def login(self, request: Request, response: Response, password: str) -> str:
        if self._record_failure(request):
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
        role = None
        if verify_password(password, self.owner_hash):
            role = "owner"
        elif verify_password(password, self.demo_hash):
            role = "demo"
        if role is None:
            raise HTTPException(401, "Invalid password")
        self._clear_failures(request)
        response.set_cookie(
            COOKIE_NAME,
            self._token(role, int(time.time()) + SESSION_SECONDS),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return role

    def logout(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="strict", path="/")

    def register_routes(self, app: FastAPI) -> None:
        @app.post("/v1/auth/login")
        def login(request: Request, response: Response, body: LoginRequest):
            role = self.login(request, response, body.password)
            return {"role": role}

        @app.post("/v1/auth/logout")
        def logout(response: Response):
            self.logout(response)
            return {"ok": True}

        @app.get("/v1/auth/me")
        def me(request: Request):
            session = self.current(request)
            if session is None:
                raise HTTPException(401, "Authentication required")
            return {"authenticated": True, "role": session.role, "expires_at": session.exp}


def auth_dependency(manager: AuthManager):
    """Return a FastAPI dependency while keeping manager state app-local."""
    return Depends(manager.dependency)


def hash_password_cli() -> None:
    print(hash_password(getpass.getpass("Password: ")))
