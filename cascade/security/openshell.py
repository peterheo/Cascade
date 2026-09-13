from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field

from cascade.domain.models import Record
from cascade.tools.gateway import SandboxDecision, ToolAction
from cascade.tools.permissions import ALL_OPERATIONS, Operation


class SandboxPolicy(Record):
    """Capability allowlist for the execution boundary.

    This is the policy an OpenShell profile would carry: deny by default, allow only
    the providers, operations, endpoints and spend a task actually needs. Enforcement
    here is in-process, so it constrains Cascade's own executor rather than an OS
    sandbox; the `ExecutionSandbox` protocol is the seam a real OpenShell runner
    plugs into without changing the executor.
    """

    name: str = "cascade-default"
    allowed_providers: tuple[str, ...] = ()
    allowed_operations: tuple[Operation, ...] = ALL_OPERATIONS
    allowed_endpoints: tuple[str, ...] = ()
    allow_network: bool = False
    max_amount: Decimal = Field(default=Decimal("1000"), ge=0)


class SandboxDenial(Record):
    at: datetime
    action_id: str
    provider: str
    operation: Operation
    reason: str


class PolicySandbox:
    """Deny-by-default execution boundary; every denial stays visible in the log."""

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        self.denials: list[SandboxDenial] = []

    def grant_provider(self, provider: str) -> None:
        """Widen the profile deliberately. Only a human decision should call this."""
        if provider not in self.policy.allowed_providers:
            self.policy = self.policy.model_copy(
                update={"allowed_providers": (*self.policy.allowed_providers, provider)}
            )

    def authorize(self, action: ToolAction) -> SandboxDecision:
        reason = self._reject(action)
        if reason is None:
            return SandboxDecision(
                allowed=True,
                policy=self.policy.name,
                reason=f"{action.provider}:{action.operation} is inside the profile.",
            )
        self.denials.append(
            SandboxDenial(
                at=datetime.now(UTC),
                action_id=action.id,
                provider=action.provider,
                operation=action.operation,
                reason=reason,
            )
        )
        return SandboxDecision(allowed=False, policy=self.policy.name, reason=reason)

    def _reject(self, action: ToolAction) -> str | None:
        if action.provider not in self.policy.allowed_providers:
            return f"Provider {action.provider} is outside the sandbox profile."
        if action.operation not in self.policy.allowed_operations:
            return f"Operation {action.operation} is not in the sandbox profile."
        if action.amount > self.policy.max_amount:
            return f"Amount {action.amount} exceeds the sandbox spend ceiling."
        return None


def demo_sandbox() -> PolicySandbox:
    """The demo profile: the four mock providers, no outbound network."""
    return PolicySandbox(
        SandboxPolicy(
            allowed_providers=(
                "mock_transfer",
                "mock_hotel",
                "mock_restaurant",
                "mock_ticket",
            ),
            max_amount=Decimal("500"),
        )
    )
