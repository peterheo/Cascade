# Implementation decisions

The original architecture is preserved in `architecture.md` without modifications.

- Python 3.12, Pydantic v2, NetworkX, and FastAPI form the first executable slice.
- Identifiers are readable strings in the fixture/domain baseline. Multi-user identity
  and database-backed identifiers belong with the persistence milestone.
- Only temporal BEFORE/TRAVEL_TO/REQUIRES edges are implemented. Every such edge
  carries an explicit lag and retains provenance, confidence, and an explanation.
- Hard edges constrain earliest possible starts. Durations are preserved while
  projecting downstream times. Original scheduled times remain authoritative.
- Deadline violations use projected starts. Bounds are inclusive: equality is valid.
- Every positive increase in a soft violation currently counts as material; a
  preference-aware materiality policy is deferred.
- The event service evaluates the entire small world. This prevents an optimization
  from silently skipping cross-branch constraints when the model grows.
- New/worsening violations create incidents; repeated identical payloads return the
  original response. Reusing an event ID with different data is rejected.
- A lock serializes event transactions and state/audit snapshots in one process.
  This is deliberately temporary until a transactional PostgreSQL repository exists.
- No severity color is assigned before recovery search establishes recoverability.
- There are no external effects or LLM calls in Phase 1.

## Phase 2 — recovery search

The reproducible provider inventory lives in `cascade/data/flight_delay_black.json`
and ships in the Python wheel. Four typed adapters expose the same read-only query
contract. Quotes retain fixture IDs, query windows, check times, and expiry. An
unsupported fixture date is an error, not evidence that real inventory is empty.

The planner visits affected descendants in topological order, querying each provider
once per search. Each branch attempts preservation followed by provider-backed
options. Intermediate checks reject conflicts at processed targets; unresolved
downstream constraints remain eligible for later repair. A full-world feasibility
pass is mandatory before surfacing a candidate. Plans do not mutate live state.

All candidates retain per-intent quality dimensions plus additional spend, original
loss, refund, and soft delay. Pareto dominance compares these separately. There is
no scalar winner and no inferred monetary authority. Refunds do not increase the
additional-spend allowance. Fixture quality 0.55 for quick dinner is an exposed demo
assumption describing degraded celebration intent, not a measured user preference.

Provider-owned deadlines now carry provenance. Substitution may explicitly replace
those deadlines; it cannot erase a user-owned cutoff. Dropping an optional activity
removes its duration but connects incoming and outgoing travel edges with summed
buffers, conservatively retaining the route through that location. Hard REQUIRES
prerequisites and hard user deadlines cannot be erased by abandonment. This MVP
does not optimize alternate routes or infer travel between new locations; fixture
substitutes are assumed to fit the existing route and travel-time bounds.

The policy exposes required intents (transfer and accommodation in the demo),
spending, tool calls, options, depth, expansions, frontier size, and elapsed time.
Time checks are cooperative between local operations. Future network adapters must
enforce I/O timeouts inside their query implementations. Frontier truncation is
reported as incomplete rather than proving optimality or global unavailability.

Result meanings:

- COMPLETE: all configured fixture branches searched, with feasible alternatives.
- PARTIAL: feasible alternatives exist, but a budget or unknown provider result limited search.
- BLOCKED: provider failure or missing/expired evidence prevents a feasible result.
- BUDGET_EXHAUSTED: search stopped without a complete feasible candidate.
- NO_FEASIBLE_PLAN: no combination in the checked inventory satisfies this policy.

Exhaustion evidence is scoped to each provider and its search window. It never
claims that every real-world recovery has been exhausted. Phase 2 has no side
effects, approval endpoint, execution gate, or post-booking verification. Candidate
status AWAITING_APPROVAL describes the next product step only.

The plan/replan API requires an expected world version. Saved plans include the
version used for search and are marked stale on retrieval after an event. Audit
entries include provider evidence, limits, rejection counts, and candidate worlds.
Detailed per-branch execution traces and persistent storage remain future work.

## Phase 3 — semantic reasoning

`cascade/reasoning` contains the async Token Factory transport, typed extraction,
strategy and comparison schemas, prompt boundaries, and semantic orchestration.
`httpx` is now a runtime dependency. No separate agent framework or OpenAI SDK is
needed for the narrow chat-completions contract.

Natural event text is never passed directly to the deterministic mutation layer.
Pydantic validation, known-ID matching, source excerpt checks, confidence admission,
and version checks sit between inference and application. Preview is the default.
Explicit confirmation has independent provenance. Concurrent natural-event replay
is serialized without holding the core lock across model I/O.

Strategy suggestions reorder only existing provider-backed recovery stages; preserve
remains first. The complete configured frontier is retained under the same budgets.
When a budget truncates search, semantic ordering may change which branches are
explored, and the existing incomplete-search status remains authoritative.

Comparisons must cover the deterministic candidate IDs exactly once and recommend
only one of those IDs or null. The prose is advisory, separately returned from the
candidate facts; structural validation does not prove semantic truthfulness.
Missing/failed reasoning leaves the deterministic planner available with visible
warnings. Model output never establishes external inventory or grants authority.

Runtime configuration, failure contracts, and live verification results are
documented in [nebius.md](nebius.md).

API testing follows the [FastAPI testing guide](https://fastapi.tiangolo.com/tutorial/testing/).
Models use [Pydantic validation](https://docs.pydantic.dev/latest/concepts/validators/)
to reject invalid intervals, unknown references, and unexpected fields.
