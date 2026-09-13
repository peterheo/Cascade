import type { Workspace } from './cascade-types';
import data from './preview-data.json' with { type: 'json' };
const replay = data as unknown as {
  initial: Workspace;
  disrupted: Workspace;
  planned: Workspace;
  event: unknown;
  paths: Record<
    string,
    {
      approval: unknown;
      approved: Workspace;
      started: Workspace;
      steps: Workspace[];
    }
  >;
};
let preview = structuredClone(replay.initial);
export function isLocal() {
  return (
    typeof window !== 'undefined' &&
    ['localhost', '127.0.0.1'].includes(window.location.hostname)
  );
}
export async function api<T>(path: string, body?: unknown): Promise<T> {
  if (!isLocal()) return structuredClone(previewRequest(path, body)) as T;
  const response = await fetch('/cascade-api/simulation' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  if (!response.ok)
    throw new Error(
      typeof payload?.detail === 'string'
        ? payload.detail
        : 'The workspace could not complete this request. Refresh and try again.',
    );
  return payload as T;
}
function previewRequest(path: string, body: unknown) {
  const b = (body || {}) as {
    expected_version?: number;
    plan_id?: string;
    approval_id?: string;
    expected_step?: number;
  };
  if (path === '/v1/workspace') return preview;
  if (path === '/v1/demo/reset') {
    preview = structuredClone(replay.initial);
    return preview.state;
  }
  if (path === '/v1/demo/scenarios/flight_delay/inject') {
    if (preview.state.world.version === 0)
      preview = structuredClone(replay.disrupted);
    return replay.event;
  }
  if (path.endsWith('/plan') || path.endsWith('/replan')) {
    if (preview.state.world.version !== 1)
      throw new Error('Reset this demo before comparing a fresh recovery.');
    preview = structuredClone(replay.planned);
    return preview.planning;
  }
  if (path.endsWith('/approve') || path.endsWith('/reject')) {
    if (
      b.expected_version !== preview.state.world.version ||
      b.expected_version !== 1
    )
      throw new Error('This plan is stale. Reset and compare again.');
    const branch = replay.paths[b.plan_id || ''];
    if (!branch) throw new Error('Choose an available plan.');
    if (path.endsWith('/approve')) {
      preview = structuredClone(branch.approved);
      return branch.approval;
    }
    const rejected = { ...branch.approved.approvals[0], status: 'REJECTED' };
    preview.approvals = [
      ...preview.approvals.filter((a) => a.plan_id !== b.plan_id),
      rejected,
    ];
    preview.audit.push({ type: 'plan.rejected' });
    return rejected;
  }
  if (path.endsWith('/execute')) {
    const id = path.split('/')[3];
    const branch = replay.paths[id];
    if (
      !branch ||
      !preview.approvals.some(
        (a) =>
          a.plan_id === id && a.id === b.approval_id && a.status === 'APPROVED',
      )
    )
      throw new Error('Approve this plan before execution.');
    preview = structuredClone(branch.started);
    return preview.executions[0];
  }
  if (path.endsWith('/advance')) {
    const execution = preview.executions[0];
    if (!execution || execution.id !== path.split('/')[3])
      throw new Error('Unknown execution.');
    if (execution.status !== 'RUNNING') return execution;
    if (b.expected_step !== execution.next_step)
      throw new Error('Execution step changed.');
    preview = structuredClone(
      replay.paths[execution.plan_id].steps[execution.next_step],
    );
    return preview.executions[0];
  }
  if (path.endsWith('/cancel')) {
    const execution = preview.executions[0];
    if (!execution) throw new Error('No active execution.');
    execution.status = 'CANCELLED';
    execution.message =
      'Remaining demo actions cancelled. Completed changes are retained.';
    preview.audit.push({ type: 'execution.cancelled' });
    return execution;
  }
  throw new Error(
    'This action needs the connected local workspace. The hosted preview uses a verified demo replay.',
  );
}
