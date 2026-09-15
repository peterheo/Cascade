"""Shared preference routes for the gateway and the isolated simulation."""

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import Field, ValidationError

from cascade.domain.models import Record
from cascade.memory.preferences import Directive, Preference, PreferenceError, preference
from cascade.service import CascadeService


class PreferenceRequest(Record):
    """Explicit preferences only. A learned one is promoted, never authored here."""

    statement: str = Field(min_length=1, max_length=500)
    directive: Directive
    value: str = Field(min_length=1, max_length=200)


class PreferenceStatusRequest(Record):
    status: Literal["ACTIVE", "SUGGESTED", "RETIRED"]
    actor: str = Field(default="user", min_length=1, max_length=100)


def register_preference_routes(app: FastAPI, service: CascadeService) -> None:
    """Register preference endpoints against the supplied service instance."""

    @app.get("/v1/preferences")
    def preferences():
        with service.lock:
            return tuple(service.preferences.values())

    @app.post("/v1/preferences")
    def add_preference(request: PreferenceRequest) -> Preference:
        try:
            return service.add_preference(
                preference(request.statement, request.directive, request.value)
            )
        except (PreferenceError, ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/v1/preferences/{preference_id}")
    def update_preference(preference_id: str, request: PreferenceStatusRequest) -> Preference:
        try:
            return service.set_preference_status(preference_id, request.status, request.actor)
        except KeyError as exc:
            raise HTTPException(404, "unknown preference") from exc
        except (PreferenceError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/v1/preferences/{preference_id}", status_code=204)
    def delete_preference(preference_id: str) -> None:
        try:
            service.delete_preference(preference_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown preference") from exc
