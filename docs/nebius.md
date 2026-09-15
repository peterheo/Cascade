# Phase 3 — Nemotron through Nebius Token Factory

Live inference was verified on September 13, 2026 against
`nvidia/nemotron-3-super-120b-a12b`: the opt-in extraction smoke test passed, followed
by a complete extraction → mutation → strategy → deterministic recovery → comparison
run. All five candidate plans passed hard constraints, with no reasoning fallback
warnings in the successful run. No provider actions were executed.

One earlier extraction response failed validation and was rejected without applying
it. A later diagnostic request and the complete path passed; the cause of the
intermittent invalid response was not established. This verifies the integration,
not error-free model behavior. Offline tests use explicit HTTP mock transports and
never claim to be Nemotron inference.

## Configure locally

Copy `.env.example` to `.env`, enter your Token Factory key there, and run:

```sh
uv run --env-file .env cascade-reason
uv run --env-file .env cascade-reason --apply --plan
```

The first command previews a typed extraction. The second applies a high-confidence
timing update to an isolated in-memory demo, queries the fixture providers, and asks
Nemotron for recovery-stage priorities and explanations. No real bookings execute.
Add `--text 'your timing update'` to supply a different event. There is no regex or
hardcoded semantic fallback: extraction requires a successful model response.

To serve the API with your configuration:

```sh
uv run --env-file .env uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

The app reads environment variables at startup. `uv --env-file` loads the local
file; Cascade does not search parent directories or shell histories for credentials.
Restart after changing configuration. `.env` and other `.env.*` files are ignored
by Git, except the credential-free example.

Default model: `nvidia/nemotron-3-super-120b-a12b`. `NEBIUS_MODEL` selects extraction;
`NEBIUS_PLANNING_MODEL` optionally selects strategy/comparison. Both default to
Super. Ultra is not assumed available; select a model your Token Factory project
actually exposes. The global `/v1` endpoint and documented US regional endpoint
are supported. HTTP redirects are disabled.

These defaults follow the [Nebius Nemotron cookbook](https://github.com/nebius/token-factory-cookbook/blob/main/models/nemotron/nemotron3-super-120B.md)
and the [Token Factory structured-output documentation](https://docs.tokenfactory.nebius.com/ai-models-inference/json).
Model and regional structured-output compatibility still require the live test.

## API workflow

`GET /v1/reasoning/status` reports configuration, whether this process has
completed a real, schema-valid model response, and whether live inference is enabled.
Configured is not the same as verified. `GET /v1/privacy` and `PATCH /v1/privacy` expose
process-local controls for pausing live inference and opting into raw event-text retention.

Preview a natural-language event:

```sh
curl -X POST http://127.0.0.1:8000/v1/events/text \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"flight_notice","expected_version":0,"text":"My flight to Nice on September 11, 2026 now arrives at 19:05 local time instead of 16:10."}'
```

The result includes the matched commitment, proposed timestamps, confidence, exact
supporting excerpt, and model-call metadata. `apply` defaults to false. Set it true
to admit an update at confidence ≥0.9 after deterministic validation. Lower confidence
always returns NEEDS_CONFIRMATION without modifying state.

Confirm a concrete proposal using its returned extraction ID:

```sh
curl -X POST http://127.0.0.1:8000/v1/extractions/EXTRACTION_ID/confirm \
  -H 'Content-Type: application/json' -d '{"expected_version":0}'
```

Confirmation is explicit user input in this local demo. It records separate source
provenance rather than rewriting the model's confidence. Ambiguous events without
a concrete mutation cannot be confirmed; clarify and submit a new event ID.
`GET /v1/extractions/{id}` includes a stale-state indicator.

Use the incident ID returned by the applied event to request assisted planning:

```sh
curl -X POST http://127.0.0.1:8000/v1/incidents/INCIDENT_ID/plan/assisted \
  -H 'Content-Type: application/json' -d '{"expected_version":1}'
```

The response keeps `planning` (deterministic facts), `strategy` (validated search
priorities), and `comparison` (advisory model prose) separate. A recommendation
must reference an existing feasible plan. Every candidate must appear exactly once
in the comparison. The model cannot change costs, refunds, feasibility, permissions,
or the set of available provider options. Prose remains fallible and is not used as
machine authority; the structured plan remains the source of factual values.

## Failure behavior and bounds

- Missing credentials: extraction returns 503 with `not_configured`.
- Invalid JSON, unknown IDs, malformed intervals, refusals, or truncated output:
  reject the response without mutating commitments.
- Unknown/multiple/non-timing changes: typed UNSUPPORTED or NEEDS_CLARIFICATION
  outcomes; never guess a cancellation into a time update.
- Identical event replay: reuse the extraction and applied result without another
  model call. Reusing an ID with different input returns 409.
- State changes during inference or before confirmation: reject stale proposals.
- Strategy/comparison errors: return deterministic recovery candidates with explicit
  warnings. A missing model does not disable `/plan` or `cascade-demo`.

Each call defaults to a 30-second total deadline, at most two HTTP attempts, 4,096
output tokens, and a bounded input. Only transport failures, 429, and 5xx responses
retry. Invalid output and authentication errors do not retry. An assisted plan uses
at most two logical model calls; a full extraction-plus-plan path uses three.
Future live testing may require tuning the token budget for a model's reasoning mode.

Audit entries retain structured outputs, task/model IDs, timing, attempts, token
usage when supplied, and hashes of context, prompt/schema, and output. Credentials
and provider error bodies are never included. Event text is represented by a SHA-256
digest in the local audit by default; `persist_event_text` is an explicit process-local
opt-in. Gateway audit entries persist across restarts when `CASCADE_DB` is set; an
in-memory deployment resets them on restart.

## Verification

Offline contract and integration tests:

```sh
uv run pytest -q
```

Explicitly opt into the billable, read-only live extraction smoke test:

```sh
CASCADE_LIVE_TEST=1 uv run --env-file .env pytest tests/test_reasoning_live.py -q
```

Normal tests skip it. After that passes, run `cascade-reason --apply --plan` to
verify strategy/comparison against real Nemotron responses too. These are the
repeatable acceptance checks for the spec's Phase 3 runtime success condition.
