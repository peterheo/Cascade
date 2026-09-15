from datetime import datetime
from decimal import Decimal

from cascade.tools.adapters.fixtures import FixtureProvider
from cascade.tools.gateway import ToolAction, ToolResult
from cascade.tools.ledger import LedgerBackend, ProviderLedgerView


class FixtureBookingProvider(FixtureProvider):
    """Deterministic mutating adapter over the same fixture inventory the planner read.

    The ledger is this process's stand-in for a vendor record. `apply` is keyed by
    idempotency key so a retry cannot double-book, and `verify` reads that record
    back rather than trusting the write's own claim.
    """

    def __init__(self, kind: str, fixture: dict, ledger: LedgerBackend | None = None):
        self.kind = kind
        self.fixture = fixture
        self.ledger: dict[str, dict] = (
            ProviderLedgerView(ledger, self.name) if ledger is not None else {}
        )
        self.ledger_backend = ledger
        # Explicit fault injection for the demo and evals; nothing here is random.
        self.withdrawn: set[str] = set()
        self.failing: set[str] = set()

    @property
    def name(self) -> str:
        return f"mock_{self.kind}"

    def _row(self, option_id: str | None) -> dict | None:
        if option_id is None:
            return None
        rows = self.fixture["providers"][self.kind]["options"]
        return next((r for r in rows if r["id"] == option_id), None)

    def _inventory(self, action: ToolAction) -> tuple[datetime | None, datetime | None, Decimal]:
        row = self._row(action.option_id)
        if row is None:
            return None, None, Decimal("0")
        replacement = row.get("replacement") or {}
        start = replacement.get("start_at")
        end = replacement.get("end_at")
        return (
            datetime.fromisoformat(start) if start else None,
            datetime.fromisoformat(end) if end else None,
            Decimal(str(row.get("refund", "0"))),
        )

    def check(self, action: ToolAction) -> ToolResult:
        """Re-read live inventory immediately before a write; quotes go stale."""
        start, end, refund = self._inventory(action)
        withdrawn = action.option_id in self.withdrawn
        expected = action.postcondition
        if expected.commitment_present:
            available = not withdrawn and start == expected.start_at and end == expected.end_at
            detail = (
                "Inventory still matches the quoted option."
                if available
                else "Quoted inventory is no longer available at this time."
            )
        else:
            available = not withdrawn and refund >= expected.min_refund
            detail = (
                "Cancellation terms still match the quote."
                if available
                else "Cancellation terms changed since the quote."
            )
        return ToolResult(
            success=available,
            provider=self.name,
            operation=action.operation,
            raw_result_ref=f"fixture:{self.fixture['id']}:{self.kind}:{action.option_id}",
            detail=detail,
            observed_start_at=start if available else None,
            observed_end_at=end if available else None,
            refund_amount=refund if available else Decimal("0"),
        )

    def apply(self, action: ToolAction) -> ToolResult:
        recorded = self.ledger.get(action.idempotency_key)
        if recorded is not None:
            return ToolResult(
                success=True,
                provider=self.name,
                operation=action.operation,
                external_reference=recorded["reference"],
                side_effect=False,
                raw_result_ref=f"ledger:{self.kind}:{action.idempotency_key}",
                detail="Idempotent replay; the existing confirmation was reused.",
            )
        if action.option_id in self.failing or action.operation in self.failing:
            return ToolResult(
                success=False,
                provider=self.name,
                operation=action.operation,
                raw_result_ref=f"fixture:{self.kind}:{action.option_id}",
                detail="Provider rejected the write; no confirmation was issued.",
            )
        start, end, refund = self._inventory(action)
        if action.option_id in self.withdrawn:
            return ToolResult(
                success=False,
                provider=self.name,
                operation=action.operation,
                raw_result_ref=f"fixture:{self.kind}:{action.option_id}",
                detail="Inventory was withdrawn before the write landed.",
            )
        reference = f"{self.kind}-{action.idempotency_key}"
        self.ledger[action.idempotency_key] = {
            "reference": reference,
            "operation": action.operation,
            "start_at": start if action.postcondition.commitment_present else None,
            "end_at": end if action.postcondition.commitment_present else None,
            "refund": refund if action.operation == "refund" else Decimal("0"),
        }
        return ToolResult(
            success=True,
            provider=self.name,
            operation=action.operation,
            external_reference=reference,
            side_effect=True,
            raw_result_ref=f"ledger:{self.kind}:{action.idempotency_key}",
            detail=f"{action.operation} confirmed by the fixture provider.",
        )

    def verify(self, action: ToolAction) -> ToolResult:
        """Read the provider's own record back. The write's claim is not the proof."""
        recorded = self.ledger.get(action.idempotency_key)
        if recorded is None:
            return ToolResult(
                success=False,
                provider=self.name,
                operation=action.operation,
                raw_result_ref=f"ledger:{self.kind}:{action.idempotency_key}",
                detail="No provider record exists for this idempotency key.",
            )
        return ToolResult(
            success=True,
            provider=self.name,
            operation=action.operation,
            external_reference=recorded["reference"],
            raw_result_ref=f"ledger:{self.kind}:{action.idempotency_key}",
            detail="Provider record read back after the write.",
            observed_start_at=recorded["start_at"],
            observed_end_at=recorded["end_at"],
            refund_amount=recorded["refund"],
        )
