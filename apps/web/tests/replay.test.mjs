import assert from 'node:assert/strict';
import test from 'node:test';
import { api, isLive } from '../lib/cascade-api.ts';

test('an unset mode uses replay', () => {
  const mode = process.env.NEXT_PUBLIC_CASCADE_MODE;
  delete process.env.NEXT_PUBLIC_CASCADE_MODE;
  try {
    assert.equal(isLive(), false);
  } finally {
    if (mode === undefined) delete process.env.NEXT_PUBLIC_CASCADE_MODE;
    else process.env.NEXT_PUBLIC_CASCADE_MODE = mode;
  }
});

test('all five replay plans require approval and finish without hard conflicts', async () => {
  await api('/v1/demo/reset', {});
  await api('/v1/demo/scenarios/flight_delay/inject', {});
  let workspace = await api('/v1/workspace');
  const incident = workspace.incidents[0].id;
  const planning = await api(`/v1/incidents/${incident}/plan`, { expected_version: 1 });
  assert.equal(planning.candidates.length, 5);
  for (const plan of planning.candidates) {
    await api('/v1/demo/reset', {});
    await api('/v1/demo/scenarios/flight_delay/inject', {});
    await api(`/v1/incidents/${incident}/plan`, { expected_version: 1 });
    await assert.rejects(api(`/v1/recovery-plans/${plan.id}/execute`, { expected_version: 1 }), /Approve/);
    const approval = await api(`/v1/incidents/${incident}/approve`, { plan_id: plan.id, expected_version: 1 });
    let execution = await api(`/v1/recovery-plans/${plan.id}/execute`, { approval_id: approval.id, expected_version: 1 });
    for (let step = 0; execution.status === 'RUNNING' && step < 20; step++) {
      execution = await api(`/v1/executions/${execution.id}/advance`, { expected_step: execution.next_step });
    }
    assert.equal(execution.status, 'SUCCEEDED');
    workspace = await api('/v1/workspace');
    assert.equal(workspace.incidents[0].status, 'RESOLVED');
    assert.equal(workspace.state.assessment.violations.filter(v => v.severity === 'hard').length, 0);
  }
});
