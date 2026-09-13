import argparse
import asyncio
import json
import sys

from cascade.demo import demo_world
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import NebiusReasoner, ReasoningError
from cascade.reasoning.service import SemanticService
from cascade.service import CascadeService


async def run(args):
    core = CascadeService(demo_world())
    semantic = SemanticService(core, NebiusReasoner())
    extraction = await semantic.extract(
        NaturalEventRequest(
            event_id="natural_demo",
            expected_version=0,
            text=args.text,
            apply=args.apply,
        )
    )
    result = {"extraction": extraction.model_dump(mode="json")}
    if args.plan and extraction.event_result and extraction.event_result.incident:
        planning = await semantic.assisted_plan(
            extraction.event_result.incident.id,
            core.world.version,
            SearchPolicy(),
        )
        result["recovery"] = planning.model_dump(mode="json")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Live Nemotron extraction and recovery demo")
    parser.add_argument(
        "--text",
        default=(
            "My flight to Nice on September 11, 2026 will now arrive at 19:05 local time "
            "instead of 16:10."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply a high-confidence timing update"
    )
    parser.add_argument(
        "--plan", action="store_true", help="Add strategy and recovery explanations"
    )
    args = parser.parse_args()
    if args.plan and not args.apply:
        parser.error("--plan requires --apply so the incident exists")
    try:
        asyncio.run(run(args))
    except ReasoningError as exc:
        print(f"Reasoning unavailable ({exc.code}): {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
