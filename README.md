# Cascade

**One thing changed. Cascade handles the rest.**

Cascade models personal commitments as a dependency graph. When a flight changes,
deterministic code identifies downstream timing failures before an AI proposes recovery.

This repository implements **Phase 1 — deterministic core**, **Phase 2 — recovery search**,
**Phase 3 — Nemotron integration**, **Phase 4 — product surface**, **Phase 5 — approval,
execution and verification**, and **Phase 6 — evaluation** of the supplied
[architecture specification](docs/architecture.md). It is a local, single-user demo,
with synthetic data. The gateway can persist its state to SQLite when `CASCADE_DB` is
set; without it, the default is in-memory state.

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
API — no build step, no bundler, no network dependency. `apps/web` is the React workspace,
live at https://app.cascade.150.136.6.100.nip.io, and it talks to the simulation API.

Inject the demo from the command line instead:

```sh
curl -X POST http://127.0.0.1:8000/v1/demo/scenarios/flight_delay/inject
curl http://127.0.0.1:8000/v1/state
curl http://127.0.0.1:8000/v1/audit
curl -X POST http://127.0.0.1:8000/v1/incidents/inc_demo_flight_delay_v0/plan \
  -H 'Content-Type: application/json' -d '{"expected_version": 1}'
```

Repeated demo injection is idempotent. With the default in-memory store, restart the server
to reset. `POST /v1/events`
accepts typed mutations with an event ID, expected world version, timezone-aware
start/end timestamps, and source provenance. Conflicting IDs, stale versions, and
low-confidence inputs return 409; invalid data returns 422. The confidence threshold
is a demo admission rule, not authentication or authorization. Bind locally and use
one worker for the single-process demo.

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

Set `CASCADE_DB` to persist the gateway's world, events, incidents, preferences, resolution
memory, audit log, searches, approvals, and execution history across restarts. Set
`CASCADE_LEDGER_DB` to persist the gateway provider ledger in a separate SQLite file;
`GET /v1/ledger/orphans` reports durable writes that have no matching execution record.
Set `CASCADE_SIMULATION_DB` to persist the React workspace's preferences and resolution
memory; its demo itinerary, world, incidents, plans, approvals, executions, and provider
ledger remain resettable and in memory. The Oracle unit stores these databases at
`/var/lib/cascade/gateway.db`, `/var/lib/cascade/ledger.db`, and
`/var/lib/cascade/simulation.db`.

`GET /v1/skills` lists the versioned recovery templates. Planning matches one from the
incident automatically; `{"skill": "..."}` on the plan request chooses one explicitly, and
an explicit `policy` overrides the template's limits.

`GET /v1/privacy` reports the process-local reasoning controls. `PATCH /v1/privacy` can
pause live Nemotron calls while keeping deterministic planning available. Event extraction
audits keep a SHA-256 digest of the submitted text by default; set `persist_event_text` only
when retaining the raw text is appropriate. Reasoning audits include a context manifest with
the task, allowlisted fields, entity IDs, byte count, and input hash, never the rendered prompt.

### Authentication

Set `CASCADE_AUTH_REQUIRED=1` to require sign-in. The owner password and session signing key
come from `CASCADE_OWNER_PASSWORD_HASH` and `CASCADE_SESSION_SECRET`; an optional
`CASCADE_DEMO_PASSWORD_HASH` enables a read-only demo role. Password hashes are generated with
`uv run cascade-hash-password`. The gateway (`/v1`) is owner-only because it can receive
personal data; the simulation app (`/simulation/v1`) is available to both roles, with privacy
changes reserved for the owner. Auth routes remain available at `/v1/auth/login`,
`/v1/auth/logout`, and `/v1/auth/me`.

When `CASCADE_ICLOUD_USER` and `CASCADE_ICLOUD_APP_PASSWORD` are set, the gateway polls the
iCloud IMAP folder named by `CASCADE_ICLOUD_MAIL_FOLDER` (default `Cascade`) every
`CASCADE_MAIL_POLL_SECONDS` seconds (default 90, clamped to 60–3600). New messages are sent
through extraction with confirmation still required; `GET /v1/connectors/mail/status` reports
the watcher state. Mail credentials are never logged or persisted, and message text is retained
only when `persist_event_text` is enabled in the process-local privacy settings.

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
- Deterministic incident severity, withheld for inconclusive searches, incident resolution,
  and explicit dismissal.
- A projected-time overlap check that flags physically overlapping commitments as a hard
  violation even without a dependency link or with only a soft link.
- Read-back after a provider write raises, using the provider record to distinguish a landed
  write, a write that did not land, and an external state that needs reconciliation.
- A declarative 29-scenario evaluation suite with reproducible fault injection and
  precision/recall, feasibility, security and degradation metrics.
- A zero-build product surface: disruption inbox, stable dashboard, blast-radius graph,
  tradeoff comparison, approval gate, and audit/boundary trail.
- An interactive React Flow dependency graph with live server-sent refresh in the React
  workspace.
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

Twenty-nine deterministic scenarios vary one axis each — arrival time, search policy,
permission policy, sandbox profile, injected fault, execution mode — and assert what
should happen. The report gives conflict-detection precision and recall, the share of
surfaced plans passing the constraint engine, the share of mutation attempts blocked in
security scenarios, and the share of degradation scenarios that report exhaustion
instead of a guess.

| Measure | Result |
| --- | ---: |
| Scenarios | 29 |
| Passed | 29 |
| Conflict detection precision | 1.0 |
| Conflict detection recall | 1.0 |
| Surfaced plans passing constraints | 1.0 |
| Unauthorized mutations blocked | 1.0 |
| Correct exhaustion instead of a guess | 1.0 |

Metrics with no supporting scenarios report `None` rather than a flattering default, and
the suite is part of `pytest`. These are fixture-suite results, not field measurements.

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

1. Real connectors and an out-of-process OpenShell runner behind the same sandbox seam.

Package layout follows the design's domain/graph/constraints split. `cascade/service.py`
is the orchestration boundary and `cascade/persistence/store.py` supplies the optional
durable store; `apps/api` is the HTTP adapter.

## Deploy

The live Oracle deployment is at https://cascade.150.136.6.100.nip.io. Update it with
`ssh opc@<host> 'bash -s' < deploy/oracle/update.sh`. Secrets live in
`/etc/cascade/cascade.env` on the box; the install and update scripts are in
`deploy/oracle/`.

The live React workspace is at https://app.cascade.150.136.6.100.nip.io. Build and ship
it from the repository root with `deploy/oracle/deploy-web.sh`.

## Known limitations

Resource conflicts are not implemented yet. When `CASCADE_AUTH_REQUIRED=1`, the gateway and
simulation require a signed session cookie. The owner password hash is supplied through
`CASCADE_OWNER_PASSWORD_HASH`; an optional read-only demo account uses
`CASCADE_DEMO_PASSWORD_HASH`, and sessions are signed with `CASCADE_SESSION_SECRET`.
Plans, searches, approvals,
executions, and fixture provider ledgers remain process-local in the React simulation; no real
booking, refund or message is ever sent. The gateway sandbox is enforced in-process,
so it constrains Cascade's executor rather than the operating system. In the React
workspace, security denials are read from the gateway's sandbox, and simulation
executions don't pass through that sandbox, so the security callout stays empty there.
Authentication is disabled when `CASCADE_AUTH_REQUIRED` is unset, preserving local development
and existing tests. The deployment sets it to `1`; missing owner credentials or a session secret
fail closed at startup. `PATCH /v1/privacy` is owner-only when authentication is enabled.
Fixture quality
values are explicit demo assumptions, not learned preferences. Search completeness
refers only to the queried inventory and operators.

The constraint engine's elapsed-time arithmetic uses wall-clock time zone offsets, so a
chain crossing a DST transition can be judged feasible when it is not. The demo date has
no transition.

Applying a corrective event does not re-check open incidents, and reconciliation closes an
incident when its original violations clear without considering new ones.

When the candidate frontier is capped, branches are ranked by additional cost alone, so
intent-preserving branches may be dropped first.

When an option budget truncates the search and a provider is also down, the planner status
reports `BUDGET_EXHAUSTED`, hiding the outage.

Two commitments under one intent overwrite each other's quality score rather than taking
the minimum. Demo intents are one-to-one.

Whether a mid-plan violation can still be repaired is judged only from its last affected
commitment.

The gateway approval endpoint enforces the echoed total amount but fills the approved action
set from the server's own proposal.

The overall Nebius timeout equals the per-request timeout, so a hung request consumes the
whole budget and the second attempt rarely runs. Degradation to the deterministic plan
still works.

A natural-language event applied at confidence ≥ 0.9 can be steered by injected text into a
timing change. It is bounded to a typed, versioned, audited time change, with no money or
booking.

The overlap check adds no travel time between unlinked commitments. Buffers exist only on
dependency edges.

The dependency graph re-levels only when the set of commitments changes, not when edges
change under the same set.

The hosted replay fixture has no model comparison or sandbox denials, so the Recommended
badge and the security callout render only against a live backend.

## Stepwise simulation workspace

The Phase 4 React workspace remains available alongside the API-served product UI.
Run the API as above, then run `npm ci` and `npm run dev -- --host 127.0.0.1` in
`apps/web` and open http://localhost:3000. It offers plan comparison, exact approval,
step verification, cancellation, and an audit trail.

Local dev (`npm run dev`) is live and uses `/simulation/v1`, an isolated fixture workspace with its own
world, plans, and approvals. Simulation cannot mutate the main `/v1` gateway state
or satisfy its approval requirements. The main UI at http://127.0.0.1:8000 retains
the remote branch's tool gateway, permission tiers, skills, memory, and notifications.
Both interfaces use the shared deterministic planner and Nebius integration.

The hosted build is replay by default when `NEXT_PUBLIC_CASCADE_MODE` is unset.
`npm run build:export` sets `NEXT_PUBLIC_CASCADE_MODE=live` for the Oracle deployment,
so the deployed React workspace uses the live simulation API. The hosted replay build
uses synthetic data and has no live backend.
Run `uv run python scripts/export_web_demo.py` to regenerate it after fixture changes.
Validate the React workspace with `npm run typecheck`, `npm run lint`, `npm test`,
and `npm run build` from `apps/web`.

## License

Licensed under the Apache License, Version 2.0 — see `LICENSE`.
