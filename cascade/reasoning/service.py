import asyncio
from uuid import uuid4

from cascade.domain.models import Mutation, SourceRef
from cascade.graph.traversal import descendants
from cascade.planning.models import SearchPolicy
from cascade.reasoning.models import (
    AssistedPlanningResult,
    ExtractedChange,
    ExtractionResult,
    NaturalEventRequest,
    PlanComparison,
    ProposedStrategy,
)
from cascade.reasoning.nebius import ReasoningError, ReasoningProvider
from cascade.reasoning.prompts import COMPARE, EXTRACT, STRATEGY
from cascade.service import CascadeService, ConflictError


class SemanticService:
    def __init__(self, core: CascadeService, provider: ReasoningProvider):
        self.core = core
        self.provider = provider
        self.lock = asyncio.Lock()
        self.requests: dict[str, NaturalEventRequest] = {}
        self.extractions: dict[str, ExtractionResult] = {}
        self.event_to_extraction: dict[str, str] = {}
        self.latest_assisted: AssistedPlanningResult | None = None

    def _version(self, expected: int):
        if self.core.world.version != expected:
            raise ConflictError("world changed; fetch state and retry reasoning")

    async def _call(self, task, schema, prompt, context):
        try:
            parsed, trace = await self.provider.structured(task, schema, prompt, context)
        except ReasoningError as exc:
            with self.core.lock:
                self.core._record_audit(
                    {"type": "reasoning.failed", "task": task, "code": exc.code}
                )
            raise
        with self.core.lock:
            self.core._record_audit(
                {
                    "type": "reasoning.completed",
                    "call": trace.model_dump(mode="json"),
                    "output": parsed.model_dump(mode="json"),
                }
            )
        return parsed, trace

    def _reject(self, message: str):
        with self.core.lock:
            self.core._record_audit({"type": "reasoning.rejected", "reason": message})
        raise ReasoningError("invalid_output", message)

    async def extract(self, request: NaturalEventRequest) -> ExtractionResult:
        # Serialize semantic replay checks without holding the core's thread lock during I/O.
        async with self.lock:
            with self.core.lock:
                if request.event_id in self.requests:
                    if self.requests[request.event_id] != request:
                        raise ConflictError(
                            "event ID already used with different natural-language input"
                        )
                    return self.extractions[self.event_to_extraction[request.event_id]]
                if request.event_id in self.core.events:
                    raise ConflictError("event ID already used by a typed event")
                self._version(request.expected_version)
                snapshot = self.core.world
            extracted, trace = await self._call(
                "extract",
                ExtractedChange,
                EXTRACT,
                {
                    "event_text": request.text,
                    "world": {
                        "version": snapshot.version,
                        "commitments": [c.model_dump(mode="json") for c in snapshot.commitments],
                    },
                },
            )
            mutation = None
            event_result = None
            status = {
                "UPDATE": "PROPOSED",
                "NO_CHANGE": "NO_CHANGE",
                "NEEDS_CLARIFICATION": "NEEDS_CONFIRMATION",
                "UNSUPPORTED": "UNSUPPORTED",
            }[extracted.outcome]
            if extracted.outcome == "UPDATE":
                current = next(
                    (c for c in snapshot.commitments if c.id == extracted.commitment_id), None
                )
                if current is None:
                    self._reject("Extracted commitment does not exist in the supplied state.")
                if (
                    not extracted.evidence_quote.strip()
                    or extracted.evidence_quote not in request.text
                ):
                    self._reject("Extracted evidence must be an exact excerpt of the event text.")
                if (
                    current.start_at == extracted.new_start_at
                    and current.end_at == extracted.new_end_at
                ):
                    status = "NO_CHANGE"
                else:
                    mutation = Mutation(
                        event_id=request.event_id,
                        commitment_id=current.id,
                        expected_version=snapshot.version,
                        new_start_at=extracted.new_start_at,
                        new_end_at=extracted.new_end_at,
                        source=SourceRef(
                            source="nebius_extraction",
                            external_id=trace.id,
                            confidence=extracted.confidence,
                        ),
                    )
                    if extracted.confidence < 0.9:
                        status = "NEEDS_CONFIRMATION"
            with self.core.lock:
                self._version(snapshot.version)
                if request.apply and status == "PROPOSED":
                    event_result = self.core.ingest(mutation)
                    status = "APPLIED"
                result = ExtractionResult(
                    id=f"extraction_{uuid4().hex}",
                    status=status,
                    based_on_version=snapshot.version,
                    extraction=extracted,
                    mutation=mutation,
                    event_result=event_result,
                    model_call=trace,
                )
                self.requests[request.event_id] = request
                self.extractions[result.id] = result
                self.event_to_extraction[request.event_id] = result.id
                self.core._record_audit(
                    {
                        "type": "event.extracted",
                        "event_id": request.event_id,
                        "text": request.text,
                        "result": result.model_dump(mode="json"),
                    }
                )
                return result

    async def confirm(self, extraction_id: str, expected_version: int) -> ExtractionResult:
        async with self.lock:
            with self.core.lock:
                if extraction_id not in self.extractions:
                    raise KeyError(extraction_id)
                result = self.extractions[extraction_id]
                if result.based_on_version != expected_version:
                    raise ConflictError("confirmation version does not match the extraction")
                if result.status == "APPLIED":
                    return result
                self._version(expected_version)
                if result.mutation is None:
                    raise ConflictError("no concrete mutation to confirm; clarify the event text")
                confirmed = result.mutation.model_copy(
                    update={
                        "source": SourceRef(
                            source="explicit_user_confirmation",
                            external_id=extraction_id,
                            confidence=1,
                        )
                    }
                )
                event_result = self.core.ingest(confirmed)
                updated = result.model_copy(
                    update={"status": "APPLIED", "event_result": event_result}
                )
                self.extractions[extraction_id] = updated
                self.core._record_audit(
                    {"type": "extraction.confirmed", "extraction_id": extraction_id}
                )
                return updated

    async def assisted_plan(
        self,
        incident_id: str,
        expected_version: int,
        policy: SearchPolicy,
        skill_name: str | None = None,
    ) -> AssistedPlanningResult:
        async with self.lock:
            with self.core.lock:
                self._version(expected_version)
                incident = next((i for i in self.core.incidents if i.id == incident_id), None)
                if incident is None:
                    raise KeyError(incident_id)
                snapshot = self.core.world
                if not set(policy.required_intent_ids) <= {i.id for i in snapshot.intents}:
                    raise ValueError("required intent does not exist")
            affected = descendants(snapshot, incident.trigger_commitment_id)
            strategy = comparison = None
            warnings = []
            traces = []
            try:
                proposal, trace = await self._call(
                    "strategy",
                    ProposedStrategy,
                    STRATEGY,
                    {
                        "incident": incident.model_dump(mode="json"),
                        "commitments": [c.model_dump(mode="json") for c in snapshot.commitments],
                        "intents": [i.model_dump(mode="json") for i in snapshot.intents],
                        "policy": policy.model_dump(mode="json"),
                        "affected_ids": affected,
                        "allowed_resolutions": [
                            "PRESERVED",
                            "RESCHEDULED",
                            "SUBSTITUTED",
                            "COMPENSATED",
                            "ABANDONED",
                        ],
                    },
                )
                traces.append(trace)
                ids = [s.commitment_id for s in proposal.suggestions]
                if len(set(ids)) != len(ids) or not set(ids) <= set(affected):
                    self._reject("Strategy references duplicate or unaffected commitment IDs.")
                if any(len(set(s.priorities)) != len(s.priorities) for s in proposal.suggestions):
                    self._reject("Strategy contains duplicate operator priorities.")
                strategy = proposal
            except ReasoningError as exc:
                warnings.append(
                    f"Semantic strategy unavailable ({exc.code}); using deterministic search."
                )
            with self.core.lock:
                self._version(expected_version)
                planning = self.core.plan(
                    incident_id,
                    expected_version,
                    policy,
                    {s.commitment_id: s.priorities for s in strategy.suggestions}
                    if strategy
                    else None,
                    skill_name,
                )
            if planning.candidates:
                try:
                    proposal, trace = await self._call(
                        "compare",
                        PlanComparison,
                        COMPARE,
                        {
                            "candidates": [p.model_dump(mode="json") for p in planning.candidates],
                            "policy": policy.model_dump(mode="json"),
                            "notes": planning.notes,
                        },
                    )
                    traces.append(trace)
                    valid_ids = {p.id for p in planning.candidates}
                    ids = [p.plan_id for p in proposal.plans]
                    if len(ids) != len(set(ids)) or set(ids) != valid_ids:
                        self._reject("Comparison must cover each feasible candidate exactly once.")
                    if (
                        proposal.recommended_plan_id is not None
                        and proposal.recommended_plan_id not in valid_ids
                    ):
                        self._reject("Recommendation does not reference a feasible candidate.")
                    comparison = proposal
                except ReasoningError as exc:
                    warnings.append(
                        f"Semantic comparison unavailable ({exc.code}); retain plan facts."
                    )
            with self.core.lock:
                self._version(expected_version)
                result = AssistedPlanningResult(
                    planning=planning,
                    strategy=strategy,
                    comparison=comparison,
                    model_calls=tuple(traces),
                    warnings=tuple(warnings),
                )
                self.latest_assisted = result
                self.core._record_audit(
                    {"type": "recovery.reasoned", "result": result.model_dump(mode="json")}
                )
            return result
