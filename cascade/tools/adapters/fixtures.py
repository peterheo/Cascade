from datetime import UTC, datetime, timedelta
from typing import Protocol

from cascade.domain.models import Commitment, World
from cascade.planning.models import ProviderResult, RecoveryOption


class RecoveryProvider(Protocol):
    """Read-only query contract. Real adapters must enforce their own I/O timeout."""

    def query(self, commitment: Commitment, world: World) -> ProviderResult: ...


class FixtureProvider:
    kind: str

    def __init__(self, fixture: dict):
        self.fixture = fixture

    def query(self, commitment: Commitment, world: World) -> ProviderResult:
        now = datetime.now(UTC)
        if commitment.kind != self.kind:
            raise ValueError("provider kind mismatch")
        same_date = commitment.start_at.date().isoformat() == self.fixture["date"]
        data = self.fixture["providers"][self.kind]
        options = []
        if same_date:
            for row in data["options"]:
                replacement = row.get("replacement")
                values = {k: v for k, v in row.items() if k != "replacement"}
                if replacement:
                    values["replacement"] = {
                        **commitment.model_dump(),
                        **replacement,
                        "source": {"source": "mock_provider", "external_id": row["id"]},
                    }
                options.append(
                    RecoveryOption.model_validate(
                        {
                            **values,
                            "commitment_id": commitment.id,
                            "evidence": f"fixture:{self.fixture['id']}:{self.kind}:{row['id']}",
                        }
                    )
                )
        return ProviderResult(
            provider=f"mock_{self.kind}",
            commitment_id=commitment.id,
            status=("AVAILABLE" if options else "UNAVAILABLE") if same_date else "ERROR",
            options=tuple(options),
            exhausted=data["exhausted"] if same_date else {},
            evidence=(
                f"fixture:{self.fixture['id']}:{self.kind}; "
                f"search window {commitment.start_at.date()} (synthetic inventory)"
            ),
            checked_at=now,
            expires_at=now + timedelta(minutes=5),
        )


class TransferAdapter(FixtureProvider):
    kind = "transfer"


class HotelAdapter(FixtureProvider):
    kind = "hotel"


class RestaurantAdapter(FixtureProvider):
    kind = "restaurant"


class TicketAdapter(FixtureProvider):
    kind = "ticket"


class UnavailableProvider:
    """A provider that cannot answer. Its silence is unknown state, not empty inventory."""

    def __init__(self, kind: str, detail: str = "simulated provider outage"):
        self.kind = kind
        self.detail = detail

    def query(self, commitment: Commitment, world: World) -> ProviderResult:
        raise TimeoutError(self.detail)


class EmptyInventoryProvider:
    """A provider that answers, and the answer is that it has nothing for this search."""

    def __init__(self, kind: str, exhausted: dict[str, str] | None = None):
        self.kind = kind
        self.exhausted = exhausted or {
            "SUBSTITUTE": "Fixture inventory checked: no options in this search window."
        }

    def query(self, commitment: Commitment, world: World) -> ProviderResult:
        now = datetime.now(UTC)
        return ProviderResult(
            provider=f"empty_{self.kind}",
            commitment_id=commitment.id,
            status="UNAVAILABLE",
            evidence=f"empty-inventory:{self.kind}:{commitment.start_at.date()}",
            exhausted=self.exhausted,
            checked_at=now,
            expires_at=now + timedelta(minutes=5),
        )
