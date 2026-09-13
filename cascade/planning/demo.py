import json
from importlib.resources import files

from cascade.planning.planner import RecoveryPlanner
from cascade.tools.adapters.fixtures import (
    HotelAdapter,
    RestaurantAdapter,
    TicketAdapter,
    TransferAdapter,
)


def demo_planner() -> RecoveryPlanner:
    fixture = json.loads(files("cascade").joinpath("data/flight_delay_black.json").read_text())
    return RecoveryPlanner(
        {
            adapter.kind: adapter(fixture)
            for adapter in (
                TransferAdapter,
                HotelAdapter,
                RestaurantAdapter,
                TicketAdapter,
            )
        }
    )
