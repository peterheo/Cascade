import type { Workspace } from './cascade-types';

export type SandboxDenial = {
  at: string;
  action_id: string;
  provider: string;
  operation: string;
  reason: string;
};
export type SandboxState = {
  enforced: boolean;
  policy: {
    name: string;
    allowed_providers: string[];
    allowed_operations: string[];
    allowed_endpoints: string[];
    allow_network: boolean;
    max_amount: string | number;
  } | null;
  denials: SandboxDenial[];
};
export type PreferenceDirective =
  | 'require_intent'
  | 'limit_spend'
  | 'forbid_operator';
export type Preference = {
  id: string;
  statement: string;
  directive: PreferenceDirective;
  value: string;
  source: 'explicit_user' | 'learned';
  status: 'ACTIVE' | 'SUGGESTED' | 'RETIRED';
  confidence?: number;
  evidence_count?: number;
  created_at?: string;
  updated_at?: string;
};
export type CreatePreference = Pick<
  Preference,
  'statement' | 'directive' | 'value'
>;

const DEMO_PREFERENCES: Preference[] = [
  {
    id: 'demo-pref-dinner',
    statement: 'Protect the celebration dinner when travel changes.',
    directive: 'require_intent',
    value: 'intent_restaurant',
    source: 'explicit_user',
    status: 'ACTIVE',
  },
  {
    id: 'demo-pref-hotel',
    statement: 'Protect hotel accommodations over entertainment.',
    directive: 'require_intent',
    value: 'intent_hotel',
    source: 'explicit_user',
    status: 'ACTIVE',
  },
  {
    id: 'demo-pref-spend',
    statement: 'Keep additional recovery spend under €210.',
    directive: 'limit_spend',
    value: '210',
    source: 'explicit_user',
    status: 'ACTIVE',
  },
];

async function localRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/cascade-api' + path, {
    ...init,
    headers: { 'Content-Type': 'application/json' },
  });
  const payload = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  if (!response.ok)
    throw new Error(
      typeof payload?.detail === 'string'
        ? payload.detail
        : 'The preference could not be saved. Try again.',
    );
  return payload as T;
}

export async function listPreferences(): Promise<Preference[]> {
  if (!isLive()) return structuredClone(DEMO_PREFERENCES);
  return localRequest<Preference[]>('/simulation/v1/preferences');
}

export async function createPreference(
  input: CreatePreference,
): Promise<Preference> {
  if (!isLive())
    throw new Error('Preferences can only be changed in a live workspace.');
  return localRequest<Preference>('/simulation/v1/preferences', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function deletePreference(id: string): Promise<void> {
  if (!isLive())
    throw new Error('Preferences can only be changed in a live workspace.');
  await localRequest<null>(`/simulation/v1/preferences/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}
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
export function isLive() {
  return process.env.NEXT_PUBLIC_CASCADE_MODE === 'live';
}

const STREAM_EVENTS = [
  'state.changed',
  'incident.created',
  'action.completed',
  'incident.resolved',
] as const;

/**
 * Listen to the workspace's state notifications. Notifications are only a hint
 * to refetch; the workspace response remains the source of truth.
 */
export function subscribeToWorkspaceEvents(
  onChange: () => void | Promise<void>,
): () => void {
  if (
    !isLive() ||
    typeof window === 'undefined' ||
    typeof window.EventSource === 'undefined'
  )
    return () => {};

  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let retryMs = 1000;

  const connect = () => {
    if (stopped) return;
    source = new window.EventSource('/cascade-api/simulation/v1/stream');
    const handleChange = () => {
      retryMs = 1000;
      void Promise.resolve(onChange()).catch(() => {});
    };
    for (const eventName of STREAM_EVENTS)
      source.addEventListener(eventName, handleChange);
    source.onerror = () => {
      source?.close();
      source = null;
      if (stopped) return;
      reconnectTimer = setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 10000);
    };
  };

  connect();
  return () => {
    stopped = true;
    source?.close();
    source = null;
    if (reconnectTimer) clearTimeout(reconnectTimer);
  };
}

export async function securitySandbox(): Promise<SandboxState | null> {
  if (!isLive()) return null;
  const response = await fetch('/cascade-api/v1/security/sandbox');
  const payload = (await response
    .json()
    .catch(() => null)) as SandboxState | null;
  if (!response.ok)
    throw new Error('The security boundary could not be loaded.');
  return payload;
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  if (!isLive()) return structuredClone(previewRequest(path, body)) as T;
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
