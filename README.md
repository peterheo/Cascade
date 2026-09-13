# Cascade

**One thing changed. Cascade handles the rest.**

Cascade models personal commitments as a dependency graph. When a flight changes,
deterministic code identifies downstream timing failures before an AI proposes recovery.

This repository implements **Phase 1 — deterministic core**, **Phase 2 — recovery search**,
and the **Phase 3 — Nemotron integration** of the supplied
[architecture specification](docs/architecture.md). It is a local, single-user demo,
with synthetic data and in-memory state that resets on restart.

Phase 3 live extraction, strategy generation, and comparison have been verified
against Nebius Nemotron. See the [Nebius setup guide](docs/nebius.md) for configuration
and the opt-in live smoke test.

## Run

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync --locked
uv run cascade-demo
uv run cascade-demo --json
uv run cascade-demo --plan
uv run cascade-demo --plan --json
```

The seeded itinerary has five commitments: flight → transfer → hotel → dinner → movie.
Changing arrival from 16:10 to 19:05 produces four downstream timing violations and
one hotel cutoff violation. The initial itinerary has no violations.

With `--plan`, four synthetic providers return inventory and exhaustion evidence.
The bounded planner returns five Pareto alternatives, including a special dinner
without the movie (€300 additional spend) and a quick dinner with a later screening
(€245). Lower-cost plans explicitly compensate or abandon optional goals. Hotel
and transfer intents are required by the visible demo policy. All alternatives pass
the full deterministic constraint engine; no bookings or refunds are executed.

## API

```sh
uv run uvicorn apps.api.main:app --reload --host 127.0.0.1
```

Open <http://127.0.0.1:8000/docs> for the interactive API. Inject the demo:

```sh
curl -X POST http://127.0.0.1:8000/v1/demo/scenarios/flight_delay/inject
curl http://127.0.0.1:8000/v1/state
curl http://127.0.0.1:8000/v1/audit
curl -X POST http://127.0.0.1:8000/v1/incidents/inc_demo_flight_delay_v0/plan \
  -H 'Content-Type: application/json' -d '{"expected_version": 1}'
```

Repeated demo injection is idempotent. Restart the server to reset. `POST /v1/events`
accepts typed mutations with an event ID, expected world version, timezone-aware
start/end timestamps, and source provenance. Conflicting IDs, stale versions, and
low-confidence inputs return 409; invalid data returns 422. The confidence threshold
is a demo admission rule, not authentication or authorization. Bind locally and use
one worker: storage and deduplication are process-local.

`POST /v1/incidents/{id}/replan` reruns against the requested current version.
`GET /v1/recovery-plans/{id}` returns a saved candidate and marks it stale after a
world change. Planning appends audit evidence while leaving commitments untouched.
The plan request optionally accepts `policy`, including `max_additional_cost`,
`required_intent_ids`, and tool, expansion, depth, time, and frontier limits.

## Implemented

- Separate typed commitments and intents; sourced dependency edges and deadlines.
- DAG validation and ordered descendant traversal with NetworkX.
- Earliest-time projection through hard precedence/travel edges, with fixed durations.
- Hard and soft violations with actual/required timestamps and delay magnitude.
- Atomic event application, version checks, duplicate detection, and incident evidence.
- State, commitments, incident, event, audit, and scenario API routes.
- CLI demo, automated tests, dependency lockfile, and CI checks.
- Typed preserve/reschedule/substitute/compensate/abandon operators.
- Four read-only fixture adapters: transfer, hotel, restaurant, and tickets.
- Candidate world branching, global feasibility checks, and Pareto pruning.
- Provider exhaustion, outage/expiry handling, bounded search, and spending limits.
- Read-only plan/replan APIs and explicit proposed-action outcomes.
- Nebius structured-output client with bounded retries, timeouts, and model routing.
- Natural-language extraction, preview/confirmation, and stale-state protection.
- Model-suggested recovery priorities and advisory comparisons of feasible plans.

Projected times are feasibility evidence, **not changed reservations or verified
availability**. Soft constraints are assessed without shifting downstream commitments.
Cycles are rejected in this temporal graph; non-temporal relationships will need a
separate graph layer. All travel times and provider rules here are explicit fixtures.

## Validate

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The suite covers baseline feasibility, multi-level and multi-parent propagation,
delay thresholds, timezone equivalence, hard/soft constraints, cycle/reference
validation, atomic rejection, event replay, API errors, and application isolation.
Recovery tests additionally cover budgets, provider outages versus known empty
inventory, expired quotes, immutable user rules, conservative travel through skipped
activities, compensation, and globally feasible tradeoffs.

## Next milestones

1. Next.js graph and recovery comparison UI, approvals, execution, and verification (Phase 4).
2. PostgreSQL persistence and incident lifecycle reconciliation; historical incidents
   currently remain open even if a later event improves the world.
3. Real connectors, OpenShell boundary, and the broader scenario evaluation suite.

Resource conflicts, general user policy, recovery severity classification, provider
execution, authentication, and persistent audit storage are not implemented yet.
`AWAITING_APPROVAL` is a candidate status; the execution/authorization layer is a
later milestone. Fixture quality values are explicit demo assumptions, not learned
preferences. Search completeness refers only to the queried inventory and operators.

Package layout follows the design's domain/graph/constraints split. `cascade/service.py`
is the temporary in-memory orchestration boundary; `apps/api` is the HTTP adapter.
