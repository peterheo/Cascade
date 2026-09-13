import pytest

from cascade.evals.harness import load_scenarios, measure, run, run_scenario
from cascade.evals.models import Metrics


@pytest.fixture(scope="module")
def report():
    return run()


def test_every_scenario_in_the_suite_passes(report):
    failures = [
        f"{o.scenario_id}: {o.error or [c.name for c in o.checks if not c.passed]}"
        for o in report.outcomes
        if not o.passed
    ]
    assert not failures, failures
    assert report.metrics.scenarios >= 25


def test_the_suite_covers_every_metric(report):
    metrics = report.metrics
    assert metrics.conflict_precision == 1
    assert metrics.conflict_recall == 1
    # Surfaced plans pass the deterministic engine by construction, not by luck.
    assert metrics.recovery_feasibility == 1
    assert metrics.unauthorized_mutations_blocked == 1
    assert metrics.graceful_degradation == 1
    assert all(
        value is not None
        for name, value in metrics
        if name not in ("scenarios", "passed")
    )


def test_the_suite_exercises_each_category():
    tags = {tag for scenario in load_scenarios() for tag in scenario.tags}
    assert {"detection", "propagation", "recovery", "degradation", "security", "execution"} <= tags


def test_scenario_ids_are_unique_and_deterministic():
    scenarios = load_scenarios()
    assert len({s.id for s in scenarios}) == len(scenarios)
    once = run(scenarios[:4])
    twice = run(scenarios[:4])
    assert [o.passed for o in once.outcomes] == [o.passed for o in twice.outcomes]
    assert [c.actual for o in once.outcomes for c in o.checks] == [
        c.actual for o in twice.outcomes for c in o.checks
    ]


def test_a_wrong_expectation_is_reported_as_a_failure():
    """The harness has to be able to fail, or its green report proves nothing."""
    scenario = next(s for s in load_scenarios() if s.id == "05_primary_flight_delay")
    broken = scenario.model_copy(
        update={"expect": scenario.expect.model_copy(update={"hard_violations": 99})}
    )
    outcome = run_scenario(broken)
    assert not outcome.passed
    assert any(c.name == "hard_violations" and not c.passed for c in outcome.checks)
    assert measure((outcome,)).passed == 0


def test_a_harness_error_never_reads_as_a_pass():
    scenario = next(s for s in load_scenarios() if s.id == "05_primary_flight_delay")
    outcome = run_scenario(
        scenario.model_copy(
            update={"faults": scenario.faults.model_copy(update={"withdraw": {"absent": ("x",)}})}
        )
    )
    assert isinstance(measure((outcome,)), Metrics)
    assert not outcome.passed
    assert outcome.error and "KeyError" in outcome.error
