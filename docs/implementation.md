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

## Phase 5 — approval, execution, and verification

`cascade/tools/permissions.py` assigns the design's risk tier from the operation and
the committed amount. A tier is never model-assigned and never caller-supplied:
`ToolAction` recomputes both its read/write mode and its tier and rejects any value
that disagrees, so an API caller cannot present a purchase as a tier-1 draft.

`cascade/tools/gateway.py` is the only path to an external effect. It authorizes in a
fixed order — product policy, adapter availability, sandbox boundary, then approval —
so a user is never asked to approve an action the boundary would refuse anyway. Writes
are followed by an independent read: `apply` reports what it did, `verify` reads the
provider's own record back, and the call is successful only when the observed state
matches the action's postcondition. An adapter exception becomes unknown state, never
success and never proof of unavailability.

`cascade/tools/adapters/booking.py` mutates the same fixture inventory the planner
searched. Writes are keyed by an idempotency key, so a retry reuses the existing
confirmation instead of double-booking. Withdrawn inventory and write failures are
explicit switches for the demo and evals rather than random faults.

`cascade/security/openshell.py` is a deny-by-default capability profile: allowed
providers, operations, endpoints, and a spend ceiling, with every denial retained in
a log the API exposes. Enforcement is in-process, so it constrains Cascade's own
executor rather than the operating system; the `ExecutionSandbox` protocol is the seam
a real OpenShell runner plugs into. The sandbox ceiling is independent of user
approval: an approved action can still be refused by the boundary.

`cascade/security/approvals.py` requires informed consent. A grant must name every
requested action and echo the exact total; a partial list or a mismatched amount is
rejected. Approvals are bound to a plan and to the world version they were built on.

`cascade/execution/executor.py` authorizes every write before performing any of them,
so a plan is never half-authorized. Each step then re-checks live availability, writes,
verifies, applies the option to the world, bumps the version, and re-evaluates. A
failure stops the run: confirmed side effects stay committed as authoritative state and
the result is PARTIAL with `replan_required`, because the remainder of a plan is not
assumed to still hold. COMPLETED additionally requires that the resulting world equals
the planned world and carries no hard violation; the model validator enforces that a
completed execution cannot be reported without verification.

Incidents now close. `severity` is assigned from what search actually found — GREEN for
a cost-free same-provider recovery, YELLOW when every intent survives with degradation,
RED when no feasible plan keeps every intent, BLACK when nothing feasible was found —
and incidents resolve when their violations disappear, whatever removed them, or when a
user dismisses them explicitly.

## Phase 6 — scenario suite and evaluation

`cascade/evals` runs a declarative suite rather than more prose. Each scenario in
`cascade/data/scenarios/` varies the same demo itinerary along one axis — arrival time,
search policy, permission policy, sandbox profile, injected fault, execution mode — and
states what should happen. Scenario files ship inside the wheel for the same reason the
provider fixtures do, so `cascade-eval` works from an installed package; the design's
proposed top-level `scenarios/` and `evals/` directories are collapsed into the package
for that reason.

Fault injection is explicit and reproducible. `query_outage` makes a read adapter fail;
`empty_inventory` makes it answer with nothing; `withdraw` removes inventory between
the quote and the write; `reject_write` refuses the write. Nothing is random, and the
suite distinguishes the four outcomes those faults produce: BLOCKED, NO_FEASIBLE_PLAN,
a pre-check abort with no side effect, and a partial execution.

Metrics follow the design's evaluation section: conflict-detection precision and recall
against enumerated violation IDs, the share of surfaced plans that pass the constraint
engine, the share of mutation attempts blocked in security scenarios, and the share of
degradation scenarios that report exhaustion instead of surfacing a plan. A metric with
no supporting scenarios reports `None` rather than a flattering default.

A green report only means something if the harness can go red, so scenario setup runs
inside the per-scenario guard — a malformed scenario fails itself rather than the run —
and the tests assert that a deliberately wrong expectation and a broken scenario both
come back as failures.

## Phase 4 — product surface

The UI is plain HTML, CSS and ES modules under `apps/api/static`, served by the API
itself at `/`. This is a deliberate deviation from the design's Next.js/React Flow
recommendation. The demo is a single-process local system; a separate build, dev server
and proxy would add two failure modes and a toolchain to every run without changing what
the product has to show. `apps/web` is the untouched starter scaffold that came with the
repository and is not part of the running system.

One page carries the five surfaces the design calls for: the stable-state dashboard, the
incident view with the blast radius on the dependency graph, the recovery comparison, the
approval gate, and the audit and boundary trail. The graph lays commitments out by
longest-path depth, so it renders any DAG rather than the demo's chain, and it stops
colouring the blast radius once the incident closes.

The disruption inbox completes the chain the design's success criteria describe: a
message arrives in natural language, Nemotron returns a typed change with an exact source
quote and a confidence, and the change reaches state only when the user confirms it. The
preview never carries `apply`, so the server's default keeps it a proposal. When no key is
configured the endpoint answers 503 and the page says so and keeps the deterministic
simulator available, because a missing model must not block the deterministic path.

The UI holds no authority of its own. It reads the world version from the server and
echoes it back on every write, it sends back the approval's own action IDs and total
rather than recomputing them, and it renders each commitment's own wall clock instead of
converting to the viewer's timezone — the constraint engine reasoned about the
itinerary's local times, and a converted display would misreport them. Tests assert that
every `/v1` path the script calls exists on the API, so the two cannot drift apart
silently.

## Reusable recovery skills

A skill is a versioned recovery template in `cascade/data/skills/`, not a prompt. It
states which commitment kinds its trigger fires on, how severe the incident must be,
which downstream kinds it knows how to repair, which recovery operators it permits, the
order to try them in, and its search limits. Everything the trigger reads — the trigger
commitment's kind, the count of hard violations, the worst delay — comes off the incident
deterministically.

Permitting operators is a real constraint, so `SearchPolicy` carries
`allowed_resolutions` and the planner drops provider options outside that set, counting
them as `operator_not_allowed` rejections. Preservation is never listed anywhere: it is
the base case the planner constructs itself, not a provider operator, and both the policy
and the skill schema reject any attempt to restrict it.

Selection is deterministic — the most specific trigger wins, then the highest version,
then the name — and a skill may be marked `auto_select: false`. `accommodation_first_recovery`
is opt-in for a reason worth stating: withholding abandonment can turn a recoverable day
into an unrecoverable one, so narrowing the operator set has to be the user's choice
rather than a default the system makes for them.

Precedence is explicit. An explicit policy from the caller outranks the template's
limits, and explicit operator priorities — including Nemotron's suggested ordering —
outrank its ordering. The applied template's name and version are recorded on the
planning result, so the audit trail shows which template bounded a given search.
