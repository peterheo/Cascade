import argparse
import json
from datetime import datetime

from cascade.domain.models import (
    Commitment,
    Deadline,
    Dependency,
    Intent,
    Mutation,
    SourceRef,
    World,
)
from cascade.service import CascadeService


def at(time: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-11T{time}:00+02:00")


def demo_world() -> World:
    rows = [
        ("flight", "Flight arrival", "16:10", "16:10", "Reach Nice", 1.0),
        ("transfer", "Airport transfer", "16:45", "17:30", "Reach the hotel", 0.8),
        ("hotel", "Hotel check-in", "18:00", "18:20", "Have accommodation tonight", 1.0),
        ("restaurant", "Special dinner", "19:30", "21:00", "Celebrate together", 0.9),
        ("ticket", "Movie", "21:45", "23:30", "Evening entertainment", 0.5),
    ]
    return World(
        commitments=tuple(
            Commitment(
                id=kind,
                kind=kind,
                title=title,
                intent_id=f"intent_{kind}",
                start_at=at(start),
                end_at=at(end),
                source=SourceRef(source="demo_fixture", external_id=kind),
            )
            for kind, title, start, end, _, _ in rows
        ),
        intents=tuple(
            Intent(id=f"intent_{kind}", description=intent, importance=importance)
            for kind, _, _, _, intent, importance in rows
        ),
        dependencies=tuple(
            Dependency(
                id=f"{parent}_to_{child}",
                from_id=parent,
                to_id=child,
                lag_minutes=lag,
                explanation=explanation,
            )
            for parent, child, lag, explanation in [
                ("flight", "transfer", 35, "Baggage collection needs 35 minutes before pickup."),
                ("transfer", "hotel", 30, "Hotel arrival needs 30 minutes after transfer ends."),
                ("hotel", "restaurant", 30, "Dinner requires 30 minutes of travel after check-in."),
                ("restaurant", "ticket", 30, "Cinema arrival requires 30 minutes after dinner."),
            ]
        ),
        deadlines=(
            Deadline(
                id="hotel_cutoff",
                commitment_id="hotel",
                latest_start=at("18:30"),
                explanation="The original hotel does not offer check-in after 18:30.",
                source=SourceRef(source="demo_fixture", external_id="hotel_policy"),
            ),
        ),
    )


def delay_event(version: int = 0, arrival: str = "19:05") -> Mutation:
    return Mutation(
        event_id=f"demo_flight_delay_v{version}",
        commitment_id="flight",
        expected_version=version,
        new_start_at=at(arrival),
        new_end_at=at(arrival),
        source=SourceRef(source="demo_simulator", external_id="flight_delay"),
    )


def execute_demo(service: CascadeService, incident_id: str, as_json: bool = False) -> None:
    """Plan, approve explicitly, execute through the gateway, and re-verify the world."""
    planning = service.plan(incident_id, service.world.version)
    chosen = min(planning.candidates, key=lambda p: p.additional_cost)
    pending = service.execute(chosen.id, service.world.version, "demo_user")
    request = pending.approval_request
    service.approve(
        request.id,
        "demo_user",
        tuple(item.action_id for item in request.items),
        request.total_amount,
    )
    executed = service.execute(chosen.id, service.world.version, "demo_user")
    if as_json:
        print(executed.model_dump_json(indent=2))
        return
    incident = service.incident(incident_id)
    print(f"CASCADE EXECUTION — incident severity {incident.severity}\n")
    print(f"Chosen plan: additional spend EUR {chosen.additional_cost}; refund EUR {chosen.refund}")
    print(f"Approval {request.id}: {len(request.items)} actions, EUR {request.total_amount}")
    print(f"Held before approval: {pending.status}; side effects {pending.side_effects}\n")
    for step in executed.steps:
        call = step.call.result if step.call else None
        reference = call.external_reference if call else "none"
        verified = "verified" if call and call.verified else "no external effect"
        print(f"  [{step.status}] {step.commitment_id} — {step.resolution} ({verified})")
        print(f"      tier {step.call.action.risk_tier if step.call else 0}; ref {reference}")
    print(f"\nStatus: {executed.status}; world version {executed.world_version_after}")
    print(f"Remaining violations: {len(executed.remaining_violations)}")
    print(f"Incident: {service.incident(incident_id).status}")
    for line in executed.notes:
        print(f"  {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Cascade's deterministic flight-delay demo")
    parser.add_argument("--json", action="store_true", help="Print structured incident evidence")
    parser.add_argument(
        "--plan", action="store_true", help="Compare feasible recovery alternatives"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Approve the cheapest alternative, execute it, and verify the new state",
    )
    args = parser.parse_args()
    service = CascadeService(demo_world())
    result = service.ingest(delay_event())
    if args.execute:
        execute_demo(service, result.incident.id, as_json=args.json)
        return
    if args.plan:
        planning = service.plan(result.incident.id, service.world.version)
        if args.json:
            print(planning.model_dump_json(indent=2))
            return
        print(f"CASCADE RECOVERY — {planning.status} (synthetic providers, EUR)\n")
        for provider in planning.provider_results:
            for stage, reason in provider.exhausted.items():
                print(f"  {provider.commitment_id} / {stage}: {reason}")
        for number, candidate in enumerate(planning.candidates, 1):
            print(
                f"\nOption {number} — additional spend €{candidate.additional_cost}; "
                f"loss €{candidate.loss}; refund €{candidate.refund}"
            )
            for action in candidate.actions:
                print(f"  {action.resolution}: {action.explanation}")
            print("  Hard constraints: passed. Awaiting approval; nothing executed.")
        print(f"\n{planning.tool_calls} provider queries; {planning.expansions} expansions.")
        return
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return
    print("CASCADE — One thing changed. Cascade handles the rest.\n")
    print("Flight arrival: 16:10 → 19:05 (fixture)")
    print(f"Downstream commitments: {', '.join(result.incident.affected_commitment_ids)}")
    print(f"Constraint violations: {len(result.assessment.violations)}\n")
    for violation in result.assessment.violations:
        print(f"  [{violation.severity.upper()}] {violation.constraint_id}")
        print(f"    {violation.explanation}")
        print(f"    Earliest {violation.actual_at:%H:%M}; required {violation.required_at:%H:%M}")
    print("\nRun with --plan to compare recoveries. No provider actions are executed.")


if __name__ == "__main__":
    main()
