"""Export synthetic API-verified replay data for the private, backend-free preview."""

import json
from pathlib import Path

from cascade.constraints.engine import evaluate
from cascade.demo import delay_event, demo_world
from cascade.execution.service import ExecutionService
from cascade.service import CascadeService


def snapshot(core, executor=None):
    return {
        "state": {
            "world": core.world.model_dump(mode="json"),
            "assessment": evaluate(core.world).model_dump(mode="json"),
        },
        "incidents": [i.model_dump(mode="json") for i in core.incidents],
        "planning": core.latest_planning.model_dump(mode="json") if core.latest_planning else None,
        "assisted": None,
        "approvals": [a.model_dump(mode="json") for a in executor.approvals.values()]
        if executor
        else [],
        "executions": [e.model_dump(mode="json") for e in executor.executions.values()]
        if executor
        else [],
        "audit": list(core.audit),
        "reasoning": {"configured": False, "live_verified": False},
    }


core = CascadeService(demo_world())
initial = snapshot(core)
event = core.ingest(delay_event())
disrupted = snapshot(core)
result = core.plan(event.incident.id, 1)
planned = snapshot(core)
paths = {}
for plan in result.candidates:
    branch = CascadeService(core.world)
    branch.incidents = list(core.incidents)
    branch.plans = dict(core.plans)
    branch.plan_incidents = dict(core.plan_incidents)
    branch.latest_planning = result
    branch.audit = list(core.audit)
    executor = ExecutionService(branch)
    approval = executor.decide(event.incident.id, plan.id, 1, True)
    approved = snapshot(branch, executor)
    execution = executor.start(plan.id, approval.id, 1)
    started = snapshot(branch, executor)
    steps = []
    while execution.status == "RUNNING":
        execution = executor.advance(execution.id, execution.next_step)
        assert execution.status in ("RUNNING", "SUCCEEDED")
        steps.append(snapshot(branch, executor))
    assert execution.status == "SUCCEEDED"
    paths[plan.id] = {
        "approval": approval.model_dump(mode="json"),
        "approved": approved,
        "started": started,
        "steps": steps,
    }
Path("apps/web/lib/preview-data.json").write_text(
    json.dumps(
        {
            "initial": initial,
            "disrupted": disrupted,
            "planned": planned,
            "event": event.model_dump(mode="json"),
            "paths": paths,
        },
        separators=(",", ":"),
    )
    + "\n"
)
print("Exported five verified synthetic recovery replays.")
