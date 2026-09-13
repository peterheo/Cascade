# Cascade

**One thing changed. Cascade handles the rest.**

Cascade models personal commitments as a dependency graph. When a flight changes,
deterministic code identifies downstream timing failures before an AI proposes recovery.

This repository implements **Phase 1 — deterministic core**, **Phase 2 — recovery search**,
**Phase 3 — Nemotron integration**, **Phase 4 — product surface**, **Phase 5 — approval,
execution and verification**, and **Phase 6 — evaluation** of the supplied
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
uv run cascade-demo --execute
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

Open <http://127.0.0.1:8000/> for the Cascade UI and <http://127.0.0.1:8000/docs> for the
interactive API. The UI walks the whole demo: paste the airline message and let Nemotron
turn it into a typed change you confirm (or use the deterministic simulator when no key
is configured), watch the blast radius light up the dependency graph, compare the
feasible alternatives and their tradeoffs, approve the exact actions and total, then
watch each action execute and verify. It is plain HTML, CSS and ES modules served by the
API — no build step, no bundler, no network dependency. (`apps/web` is the untouched
starter scaffold and is not part of the running system.)

Inject the demo from the command line instead:

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

Executing a plan is a separate, two-step decision:

```sh
curl -X POST http://127.0.0.1:8000/v1/recovery-plans/$PLAN/execute \
  -H 'Content-Type: application/json' -d '{"expected_version": 1}'
curl -X POST http://127.0.0.1:8000/v1/approvals/$REQUEST/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved_action_ids": [...], "acknowledged_amount": "205"}'
```

The first call returns `AWAITING_APPROVAL` with the exact actions, risk tiers and total;
nothing external has happened. Approval must name every action and echo the exact
amount. The second execute call then runs each action through the gateway, verifies it
against the provider record, and re-evaluates the world. `GET /v1/security/sandbox`
shows the capability profile and every denied action.

`GET /v1/stream` emits server-sent notifications; the UI follows it, so a second tab
updates without being touched. Notifications name what changed and carry no state — a
reader fetches it back through the versioned endpoints.

`GET`/`POST`/`PATCH`/`DELETE /v1/preferences` manage explicit preferences, which narrow
every subsequent search. `GET /v1/memory/resolutions` shows what was actually chosen, and
`GET /v1/memory/suggestions` shows what repeated choices imply — suggestions only, until
a person promotes one with `PATCH`.

`GET /v1/skills` lists the versioned recovery templates. Planning matches one from the
incident automatically; `{"skill": "..."}` on the plan request chooses one explicitly, and
an explicit `policy` overrides the template's limits.

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
- Derived risk tiers, a typed tool gateway, and mutating fixture adapters with
  idempotency keys.
- Deny-by-default capability sandbox with a visible denial log.
- Informed approval: every action named, the exact total acknowledged.
- Step-by-step execution with pre-checks, postcondition verification, partial-execution
  commits, and mandatory replanning after any failure.
- Deterministic incident severity, incident resolution, and explicit dismissal.
- A declarative 28-scenario evaluation suite with reproducible fault injection and
  precision/recall, feasibility, security and degradation metrics.
- A zero-build product surface: disruption inbox, stable dashboard, blast-radius graph,
  tradeoff comparison, approval gate, and audit/boundary trail.
- Versioned recovery skills that bound the planner: deterministic trigger matching,
  permitted operators, operator ordering, and search limits.
- Explicit preferences that narrow the search, resolution memory including dismissals,
  and learned suggestions that stay suggestions until a person promotes them.
- Server-sent notifications on `GET /v1/stream`, followed live by the UI.

Projected times are feasibility evidence, **not changed reservations or verified
availability**. Soft constraints are assessed without shifting downstream commitments.
Cycles are rejected in this temporal graph; non-temporal relationships will need a
separate graph layer. All travel times and provider rules here are explicit fixtures.

## Evaluate

```sh
uv run cascade-eval
uv run cascade-eval --tag security
uv run cascade-eval --json
```

Twenty-eight deterministic scenarios vary one axis each — arrival time, search policy,
permission policy, sandbox profile, injected fault, execution mode — and assert what
should happen. The report gives conflict-detection precision and recall, the share of
surfaced plans passing the constraint engine, the share of mutation attempts blocked in
security scenarios, and the share of degradation scenarios that report exhaustion
instead of a guess. Metrics with no supporting scenarios report `None` rather than a
flattering default, and the suite is part of `pytest`.

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
activities, compensation, and globally feasible tradeoffs. Execution tests cover the
approval gate, uninformed and partial approvals, policy and sandbox denial, withdrawn
inventory, unverifiable writes, adapter outages, double-booking, partial execution and
the replan that follows it, forged risk tiers, and the full approve-execute-verify API.
UI tests assert that every endpoint the page calls exists on the API.

## Next milestones

1. PostgreSQL persistence and durable audit storage.
2. Real connectors and an out-of-process OpenShell runner behind the same sandbox seam.
3. A preference and memory surface in the UI for what the API already exposes.

Resource conflicts, authentication, and persistent storage are not implemented yet.
Execution mutates fixture provider ledgers in this process; no real booking, refund or
message is ever sent. The sandbox is enforced in-process,
so it constrains Cascade's executor rather than the operating system. Fixture quality
values are explicit demo assumptions, not learned preferences. Search completeness
refers only to the queried inventory and operators.

Package layout follows the design's domain/graph/constraints split. `cascade/service.py`
is the temporary in-memory orchestration boundary; `apps/api` is the HTTP adapter.
