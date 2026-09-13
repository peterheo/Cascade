import json
from importlib.resources import files

from cascade.security.openshell import demo_sandbox
from cascade.tools.adapters.booking import FixtureBookingProvider
from cascade.tools.gateway import ToolGateway

KINDS = ("transfer", "hotel", "restaurant", "ticket")


def demo_fixture() -> dict:
    return json.loads(files("cascade").joinpath("data/flight_delay_black.json").read_text())


def demo_gateway(fixture: dict | None = None) -> ToolGateway:
    """Mutating adapters for the same inventory the planner searched, behind the sandbox."""
    fixture = fixture or demo_fixture()
    providers = {f"mock_{kind}": FixtureBookingProvider(kind, fixture) for kind in KINDS}
    return ToolGateway(providers, demo_sandbox())
