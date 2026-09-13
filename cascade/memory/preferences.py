from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from cascade.domain.models import Record, World
from cascade.planning.models import SELECTABLE_RESOLUTIONS, SearchPolicy

Directive = Literal["require_intent", "limit_spend", "forbid_operator"]
PreferenceSource = Literal["explicit_user", "learned"]
PreferenceStatus = Literal["ACTIVE", "SUGGESTED", "RETIRED"]


class Preference(Record):
    """A preference only counts if it narrows something the deterministic layer reads."""

    id: str
    statement: str = Field(min_length=1, max_length=500)
    directive: Directive
    value: str = Field(min_length=1, max_length=200)
    source: PreferenceSource = "explicit_user"
    status: PreferenceStatus = "ACTIVE"
    confidence: float = Field(default=1, ge=0, le=1)
    evidence_count: int = Field(default=0, ge=0)
    evidence: tuple[str, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def learned_preferences_are_never_policy_by_default(self) -> Self:
        if self.source == "learned" and self.status == "SUGGESTED" and self.evidence_count == 0:
            raise ValueError("a learned preference must carry the evidence it came from")
        if self.source == "explicit_user" and self.confidence != 1:
            raise ValueError("an explicit preference is not a guess")
        if self.directive == "forbid_operator" and self.value not in SELECTABLE_RESOLUTIONS:
            raise ValueError("only a provider operator can be forbidden")
        if self.directive == "limit_spend":
            try:
                if Decimal(self.value) < 0:
                    raise ValueError("a spending limit cannot be negative")
            except InvalidOperation:
                raise ValueError("a spending limit must be a decimal amount") from None
        return self


def preference(
    statement: str,
    directive: Directive,
    value: str,
    *,
    source: PreferenceSource = "explicit_user",
    status: PreferenceStatus | None = None,
    confidence: float = 1,
    evidence: tuple[str, ...] = (),
) -> Preference:
    now = datetime.now(UTC)
    return Preference(
        id=f"pref_{uuid4().hex}",
        statement=statement,
        directive=directive,
        value=value,
        source=source,
        # A learned preference is a suggestion until a person promotes it, never policy.
        status=status or ("ACTIVE" if source == "explicit_user" else "SUGGESTED"),
        confidence=confidence,
        evidence_count=len(evidence),
        evidence=evidence,
        created_at=now,
        updated_at=now,
    )


class PreferenceError(ValueError):
    """A preference cannot be stored or applied as requested."""


def promote(current: Preference, status: PreferenceStatus, actor: str) -> Preference:
    if current.source == "learned" and status == "ACTIVE" and actor != "user":
        raise PreferenceError("only a person can turn a learned preference into policy")
    return current.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})


def narrow(policy: SearchPolicy, preferences: tuple[Preference, ...], world: World) -> SearchPolicy:
    """Apply active preferences as restrictions on top of whatever policy is in effect.

    Preferences only ever narrow: they add required intents, lower the spending
    ceiling, and remove operators. That makes their authority unambiguous without
    letting a stored preference quietly widen a caller's explicit limits.
    """
    active = [p for p in preferences if p.status == "ACTIVE"]
    required = list(policy.required_intent_ids)
    allowed = list(policy.allowed_resolutions)
    ceiling = policy.max_additional_cost
    known = {i.id for i in world.intents}
    for item in active:
        if item.directive == "require_intent":
            if item.value not in known:
                raise PreferenceError(f"preference {item.id} names an unknown intent")
            if item.value not in required:
                required.append(item.value)
        elif item.directive == "limit_spend":
            ceiling = min(ceiling, Decimal(item.value))
        elif item.value in allowed:
            allowed.remove(item.value)
    if not allowed:
        raise PreferenceError("active preferences forbid every recovery operator")
    return policy.model_copy(
        update={
            "required_intent_ids": tuple(required),
            "allowed_resolutions": tuple(allowed),
            "max_additional_cost": ceiling,
        }
    )
