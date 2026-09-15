"""Process-local privacy controls shared by the gateway and simulation APIs."""

from fastapi import FastAPI

from cascade.domain.models import Record
from cascade.reasoning.models import PrivacySettings
from cascade.reasoning.service import SemanticService


class PrivacyPatch(Record):
    live_inference: bool | None = None
    persist_event_text: bool | None = None


def register_privacy_routes(app: FastAPI, semantic: SemanticService) -> None:
    @app.get("/v1/privacy")
    def privacy() -> PrivacySettings:
        return semantic.privacy

    @app.patch("/v1/privacy")
    def update_privacy(request: PrivacyPatch) -> PrivacySettings:
        updates = request.model_dump(exclude_none=True)
        semantic.privacy = semantic.privacy.model_copy(update=updates)
        return semantic.privacy
