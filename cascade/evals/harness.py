from decimal import Decimal
from importlib.resources import files

from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.domain.models import SourceRef
from cascade.evals.models import (
    Check,
    EvalReport,
    Metrics,
    Scenario,
    ScenarioOutcome,
)
from cascade.planning.demo import demo_planner
from cascade.planning.models import CandidatePlan, PlanningResult
from cascade.security.openshell import PolicySandbox, SandboxPolicy
from cascade.service import CascadeService, ConflictError
from cascade.tools.adapters.fixtures import EmptyInventoryProvider, UnavailableProvider
from cascade.tools.demo import demo_gateway
from cascade.tools.permissions import PermissionPolicy

SCENARIO_DIRECTORY = "data/scenarios"


def load_scenarios() -> tuple[Scenario, ...]:
    """Scenarios ship inside the wheel so evals run from an installed package."""
    resources = files("cascade").joinpath(SCENARIO_DIRECTORY)
    return tuple(
        sorted(
            (
                Scenario.model_validate_json(entry.read_text())
                for entry in resources.iterdir()
                if entry.name.endswith(".json")
            ),
            key=lambda s: s.id,
        )
    )


def cheapest(candidates: tuple[CandidatePlan, ...]) -> CandidatePlan:
    # Deterministic: cost, then loss, then explanation. Never the random plan ID.
    return min(candidates, key=lambda p: (p.additional_cost, p.loss, p.explanation))


def build(scenario: Scenario) -> CascadeService:
    world = demo_world()
    dropped = set(scenario.drop_dependencies)
    available = {dependency.id for dependency in world.dependencies}
    missing = dropped - available
    if missing:
        raise ValueError(f"unknown dependency ID(s): {', '.join(sorted(missing))}")
    if dropped:
        world = world.model_copy(
            update={
                "dependencies": tuple(
                    dependency for dependency in world.dependencies if dependency.id not in dropped
                )
            }
        )
    gateway = demo_gateway()
    if scenario.sandbox_providers is not None or scenario.sandbox_max_amount is not None:
        gateway.sandbox = PolicySandbox(
            SandboxPolicy(
                name="eval-profile",
                allowed_providers=(
                    tuple(gateway.providers)
                    if scenario.sandbox_providers is None
                    else scenario.sandbox_providers
                ),
                max_amount=scenario.sandbox_max_amount
                if scenario.sandbox_max_amount is not None
                else Decimal("500"),
            )
        )
    for provider, options in scenario.faults.withdraw.items():
        gateway.providers[provider].withdrawn.update(options)
    for provider, options in scenario.faults.reject_write.items():
        gateway.providers[provider].failing.update(options)
    planner = demo_planner()
    for kind in scenario.faults.query_outage:
        planner.providers[kind] = UnavailableProvider(kind)
    for kind in scenario.faults.empty_inventory:
        planner.providers[kind] = EmptyInventoryProvider(kind)
    return CascadeService(
        world,
        gateway,
        scenario.permissions or PermissionPolicy(),
        planner,
    )


class Recorder:
    def __init__(self):
        self.checks: list[Check] = []

    def expect(self, name: str, expected, actual) -> None:
        if expected is None:
            return
        self.checks.append(
            Check(
                name=name,
                passed=expected == actual,
                expected=str(expected),
                actual=str(actual),
            )
        )


def run_scenario(scenario: Scenario) -> ScenarioOutcome:
    recorder = Recorder()
    expect = scenario.expect
    detected: tuple[str, ...] = ()
    surfaced = feasible = blocked = attempted = 0
    try:
        # Setup lives inside the guard: a malformed scenario must fail itself, not the run.
        service = build(scenario)
        event = delay_event(scenario.expected_version, scenario.arrival)
        if scenario.confidence < 1:
            event = event.model_copy(
                update={
                    "source": SourceRef(
                        source="demo_simulator",
                        external_id="flight_delay",
                        confidence=scenario.confidence,
                    )
                }
            )
        try:
            result = service.ingest(event)
        except ConflictError as exc:
            recorder.expect("ingest_rejected", True, expect.ingest_rejected)
            recorder.expect("ingest_reason", True, bool(str(exc)))
            return outcome(scenario, recorder, detected)
        recorder.expect("ingest_rejected", expect.ingest_rejected, False)
        assessment = result.assessment
        detected = tuple(sorted(v.constraint_id for v in assessment.violations))
        hard = [v for v in assessment.violations if v.severity == "hard"]
        recorder.expect("incident_created", expect.incident, result.incident is not None)
        recorder.expect("hard_violations", expect.hard_violations, len(hard))
        recorder.expect(
            "soft_violations", expect.soft_violations, len(assessment.violations) - len(hard)
        )
        if result.incident is not None:
            recorder.expect(
                "affected", expect.affected, tuple(result.incident.affected_commitment_ids)
            )
        recorder.expect("violation_ids", expect.violation_ids, detected)
        if not scenario.plan or result.incident is None:
            return outcome(scenario, recorder, detected, expect.violation_ids or ())
        planning = service.plan(
            result.incident.id, service.world.version, scenario.policy, skill_name=scenario.skill
        )
        surfaced, feasible = feasibility(planning)
        recorder.expect("planning_status", expect.planning_status, planning.status)
        recorder.expect("skill", expect.skill, planning.skill.name if planning.skill else None)
        recorder.expect("candidates", expect.candidates, len(planning.candidates))
        if expect.min_candidates is not None:
            recorder.expect(
                "min_candidates", True, len(planning.candidates) >= expect.min_candidates
            )
        severity = service.incident(result.incident.id).severity
        if expect.severity_withheld:
            recorder.expect("severity_withheld", True, severity is None)
        else:
            recorder.expect("severity", expect.severity, severity)
        if expect.exhaustion_reported:
            recorder.expect(
                "exhaustion_reported",
                True,
                any(r.exhausted for r in planning.provider_results),
            )
        if surfaced:
            recorder.expect("all_surfaced_candidates_feasible", surfaced, feasible)
        if scenario.execute == "NONE" or not planning.candidates:
            recorder.expect("execution_status", expect.execution_status, None)
            current = service.incident(result.incident.id)
            recorder.expect("incident_status", expect.incident_status, current.status)
            return outcome(scenario, recorder, detected, expect.violation_ids or ())
        plan = cheapest(planning.candidates)
        attempted = sum(1 for a in plan.actions if a.resolution != "PRESERVED")
        execution = service.execute(plan.id, service.world.version, "eval")
        if scenario.execute in ("APPROVED", "APPROVED_TWICE") and execution.approval_request:
            request = execution.approval_request
            service.approve(
                request.id,
                "eval",
                tuple(item.action_id for item in request.items),
                request.total_amount,
            )
            execution = service.execute(plan.id, service.world.version, "eval")
        if scenario.execute == "APPROVED_TWICE":
            execution = service.execute(plan.id, service.world.version, "eval")
        blocked = attempted - execution.side_effects
        recorder.expect("execution_status", expect.execution_status, execution.status)
        recorder.expect("side_effects", expect.side_effects, execution.side_effects)
        recorder.expect(
            "final_hard_violations",
            expect.final_hard_violations,
            len([v for v in evaluate(service.world).violations if v.severity == "hard"]),
        )
        recorder.expect(
            "incident_status", expect.incident_status, service.incident(result.incident.id).status
        )
        if scenario.sandbox_providers is not None or scenario.sandbox_max_amount is not None:
            recorder.expect(
                "sandbox_denials", expect.sandbox_denials, len(service.gateway.sandbox.denials)
            )
        if expect.replan_feasible is not None:
            again = service.plan(
                result.incident.id,
                service.world.version,
                scenario.policy,
                skill_name=scenario.skill,
            )
            surfaced_again, feasible_again = feasibility(again)
            surfaced += surfaced_again
            feasible += feasible_again
            recorder.expect("replan_feasible", expect.replan_feasible, bool(again.candidates))
    except Exception as exc:  # a harness failure must never read as a passing scenario
        return ScenarioOutcome(
            scenario_id=scenario.id,
            title=scenario.title,
            tags=scenario.tags,
            passed=False,
            checks=tuple(recorder.checks),
            error=f"{type(exc).__name__}: {exc}",
        )
    return outcome(
        scenario,
        recorder,
        detected,
        expect.violation_ids or (),
        surfaced,
        feasible,
        blocked,
        attempted,
    )


def feasibility(planning: PlanningResult) -> tuple[int, int]:
    """Surfaced plans must pass the deterministic engine by construction."""
    return len(planning.candidates), sum(
        1 for p in planning.candidates if not evaluate(p.world).violations
    )


def outcome(
    scenario: Scenario,
    recorder: Recorder,
    detected: tuple[str, ...],
    expected: tuple[str, ...] = (),
    surfaced: int = 0,
    feasible: int = 0,
    blocked: int = 0,
    attempted: int = 0,
) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario_id=scenario.id,
        title=scenario.title,
        tags=scenario.tags,
        passed=all(c.passed for c in recorder.checks),
        checks=tuple(recorder.checks),
        detected_violation_ids=detected,
        expected_violation_ids=expected,
        surfaced_candidates=surfaced,
        feasible_candidates=feasible,
        blocked_mutations=blocked,
        attempted_mutations=attempted,
    )


def ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def measure(outcomes: tuple[ScenarioOutcome, ...]) -> Metrics:
    true_positive = false_positive = false_negative = 0
    for item in outcomes:
        if not item.expected_violation_ids:
            continue
        detected, expected = set(item.detected_violation_ids), set(item.expected_violation_ids)
        true_positive += len(detected & expected)
        false_positive += len(detected - expected)
        false_negative += len(expected - detected)
    surfaced = sum(o.surfaced_candidates for o in outcomes)
    security = [o for o in outcomes if "security" in o.tags]
    degradation = [o for o in outcomes if "degradation" in o.tags]
    return Metrics(
        scenarios=len(outcomes),
        passed=sum(1 for o in outcomes if o.passed),
        conflict_precision=ratio(true_positive, true_positive + false_positive),
        conflict_recall=ratio(true_positive, true_positive + false_negative),
        recovery_feasibility=ratio(sum(o.feasible_candidates for o in outcomes), surfaced),
        unauthorized_mutations_blocked=ratio(
            sum(o.blocked_mutations for o in security),
            sum(o.attempted_mutations for o in security),
        ),
        graceful_degradation=ratio(
            sum(1 for o in degradation if o.passed and o.surfaced_candidates == 0),
            len(degradation),
        ),
    )


def run(scenarios: tuple[Scenario, ...] | None = None) -> EvalReport:
    outcomes = tuple(run_scenario(s) for s in (scenarios or load_scenarios()))
    return EvalReport(outcomes=outcomes, metrics=measure(outcomes))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Cascade's deterministic scenario suite")
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON")
    parser.add_argument("--tag", default=None, help="Run only scenarios carrying this tag")
    parser.add_argument("--verbose", action="store_true", help="Show passing checks too")
    args = parser.parse_args()
    scenarios = load_scenarios()
    if args.tag:
        scenarios = tuple(s for s in scenarios if args.tag in s.tags)
    report = run(scenarios)
    if args.json:
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.ok else 1)
    print(f"CASCADE EVALS — {report.metrics.passed}/{report.metrics.scenarios} scenarios passed\n")
    for item in report.outcomes:
        print(f"  [{'PASS' if item.passed else 'FAIL'}] {item.scenario_id} — {item.title}")
        if item.error:
            print(f"        error: {item.error}")
        for check in item.checks:
            if args.verbose or not check.passed:
                mark = "ok" if check.passed else "MISMATCH"
                print(f"        {mark}: {check.name} expected {check.expected}, got {check.actual}")
    metrics = report.metrics
    print("\nMetrics (None means the suite contains no evidence for that metric)")
    print(f"  conflict detection precision      {metrics.conflict_precision}")
    print(f"  conflict detection recall         {metrics.conflict_recall}")
    print(f"  surfaced plans passing constraints {metrics.recovery_feasibility}")
    print(f"  unauthorized mutations blocked    {metrics.unauthorized_mutations_blocked}")
    print(f"  correct exhaustion instead of a guess {metrics.graceful_degradation}")
    raise SystemExit(0 if report.ok else 1)
