"""Opt-in, billable Nebius smoke test. Never runs in normal offline CI."""

import asyncio
import os

import pytest

from cascade.demo import at, demo_world
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import NebiusReasoner
from cascade.reasoning.service import SemanticService
from cascade.service import CascadeService


@pytest.mark.skipif(
    os.getenv("CASCADE_LIVE_TEST") != "1" or not os.getenv("NEBIUS_API_KEY"),
    reason="Set CASCADE_LIVE_TEST=1 and NEBIUS_API_KEY to opt into live inference.",
)
def test_live_nemotron_timing_extraction():
    core = CascadeService(demo_world())
    result = asyncio.run(
        SemanticService(core, NebiusReasoner()).extract(
            NaturalEventRequest(
                event_id="live_smoke",
                expected_version=0,
                text=(
                    "My flight to Nice on September 11, 2026 now arrives at "
                    "19:05 local time instead of 16:10."
                ),
            )
        )
    )
    assert result.extraction.outcome == "UPDATE"
    assert result.mutation.commitment_id == "flight"
    assert result.mutation.new_start_at == at("19:05")
    assert core.world.version == 0  # Smoke test is a preview, never an execution.
