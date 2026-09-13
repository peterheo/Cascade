import asyncio
import hashlib
import json
import os
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from cascade.domain.models import Record
from cascade.reasoning.models import ModelCall, ReasoningTask


class ReasoningError(Exception):
    """Public, sanitized failure; provider bodies and credentials are never returned."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class NebiusSettings(Record):
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    base_url: str = "https://api.tokenfactory.nebius.com/v1"
    model: str = "nvidia/nemotron-3-super-120b-a12b"
    planning_model: str = "nvidia/nemotron-3-super-120b-a12b"
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    max_attempts: int = Field(default=2, ge=1, le=3)
    max_tokens: int = Field(default=4096, ge=256, le=8192)

    @model_validator(mode="after")
    def valid_endpoint(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or url.hostname
            not in (
                "api.tokenfactory.nebius.com",
                "api.tokenfactory.us-central1.nebius.com",
            )
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port
        ):
            raise ValueError("use a supported HTTPS Nebius Token Factory endpoint")
        if url.path.rstrip("/") != "/v1":
            raise ValueError("Nebius endpoint must end in /v1")
        if not self.model.strip() or not self.planning_model.strip():
            raise ValueError("model IDs must be nonempty")
        return self

    @classmethod
    def from_env(cls):
        model = os.getenv("NEBIUS_MODEL", cls.model_fields["model"].default)
        return cls(
            api_key=SecretStr(os.getenv("NEBIUS_API_KEY", "").strip()),
            base_url=os.getenv("NEBIUS_BASE_URL", cls.model_fields["base_url"].default),
            model=model,
            planning_model=os.getenv("NEBIUS_PLANNING_MODEL", model),
        )


class ReasoningProvider(Protocol):
    async def structured[T: BaseModel](
        self,
        task: ReasoningTask,
        schema: type[T],
        system: str,
        context: dict,
    ) -> tuple[T, ModelCall]: ...


class NebiusReasoner:
    def __init__(
        self,
        settings: NebiusSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings or NebiusSettings.from_env()
        self.transport = transport
        self.successful_calls = 0

    def status(self) -> dict:
        return {
            "provider": "nebius_token_factory",
            "configured": bool(self.settings.api_key.get_secret_value()),
            "model": self.settings.model,
            "planning_model": self.settings.planning_model,
            "live_verified": self.transport is None and self.successful_calls > 0,
        }

    async def structured[T: BaseModel](
        self,
        task: ReasoningTask,
        schema: type[T],
        system: str,
        context: dict,
    ) -> tuple[T, ModelCall]:
        if not self.settings.api_key.get_secret_value():
            raise ReasoningError("not_configured", "Set NEBIUS_API_KEY to enable live reasoning.")
        model = self.settings.model if task == "extract" else self.settings.planning_model
        schema_json = schema.model_json_schema()
        prompt = json.dumps(context, ensure_ascii=False, sort_keys=True)
        if len(prompt) > 200000:
            raise ReasoningError(
                "context_limit", "Reasoning context exceeds the bounded input limit."
            )
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": system
                    + "\nReturn JSON matching this schema:\n"
                    + json.dumps(schema_json),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema_json,
                },
            },
        }
        started = monotonic()
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.settings.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    for attempt in range(1, self.settings.max_attempts + 1):
                        try:
                            response = await client.post(
                                self.settings.base_url.rstrip("/") + "/chat/completions",
                                headers={
                                    "Authorization": "Bearer "
                                    + self.settings.api_key.get_secret_value()
                                },
                                json=payload,
                            )
                        except httpx.TransportError:
                            if attempt == self.settings.max_attempts:
                                raise ReasoningError("unavailable", "Nebius could not be reached.")
                            await asyncio.sleep(0.2 * attempt)
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            if attempt == self.settings.max_attempts:
                                raise ReasoningError(
                                    "unavailable", "Nebius is temporarily unavailable."
                                )
                            await asyncio.sleep(0.2 * attempt)
                            continue
                        if response.status_code != 200:
                            code = (
                                "authentication"
                                if response.status_code in (401, 403)
                                else "request"
                            )
                            raise ReasoningError(
                                code, f"Nebius rejected the request ({response.status_code})."
                            )
                        try:
                            data = response.json()
                            if not isinstance(data, dict) or not isinstance(
                                data.get("choices"), list
                            ):
                                raise ValueError("invalid response envelope")
                            choice = data["choices"][0]
                            if not isinstance(choice, dict) or not isinstance(
                                choice.get("message"), dict
                            ):
                                raise ValueError("invalid completion envelope")
                            if choice.get("finish_reason") != "stop" or choice["message"].get(
                                "refusal"
                            ):
                                raise ValueError("incomplete or refused response")
                            content = choice["message"]["content"]
                            if not isinstance(content, str) or len(content) > 100000:
                                raise ValueError("invalid content")
                            parsed = schema.model_validate_json(content)
                            usage = data.get("usage") or {}
                            if not isinstance(usage, dict):
                                raise ValueError("invalid usage envelope")
                            trace = ModelCall(
                                id=f"model_{uuid4().hex}",
                                task=task,
                                provider="nebius_token_factory",
                                model=model,
                                attempts=attempt,
                                elapsed_ms=int((monotonic() - started) * 1000),
                                input_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                                prompt_hash=hashlib.sha256(
                                    (system + json.dumps(schema_json, sort_keys=True)).encode()
                                ).hexdigest(),
                                output_hash=hashlib.sha256(content.encode()).hexdigest(),
                                input_tokens=usage.get("prompt_tokens"),
                                output_tokens=usage.get("completion_tokens"),
                            )
                        except (ValueError, TypeError, KeyError, IndexError, ValidationError):
                            raise ReasoningError(
                                "invalid_output", "Model response failed schema validation."
                            ) from None
                        self.successful_calls += 1
                        return parsed, trace
        except TimeoutError:
            raise ReasoningError("timeout", "Nebius reasoning exceeded its time budget.") from None
        raise ReasoningError("unavailable", "No model response was available.")
