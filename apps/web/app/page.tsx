'use client';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import Link from 'next/link';
import {
  Activity,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  GitBranch,
  Hotel,
  LoaderCircle,
  MessageSquareText,
  PlaneLanding,
  Play,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  Ticket,
  TramFront,
  Utensils,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';
import { Progress } from '@/components/ui/progress';
import { api, isLocal } from '@/lib/cascade-api';
import type {
  Workspace,
  Plan,
  Execution,
  Approval,
  Extraction,
  Audit,
} from '@/lib/cascade-types';
import replay from '@/lib/preview-data.json';

const EMPTY_PLANS: Plan[] = [];

const icons: Record<string, typeof PlaneLanding> = {
  flight: PlaneLanding,
  transfer: TramFront,
  hotel: Hotel,
  restaurant: Utensils,
  ticket: Ticket,
};
const names: Record<string, string> = {
  flight: 'Flight',
  transfer: 'Transfer',
  hotel: 'Hotel',
  restaurant: 'Dinner',
  ticket: 'Movie',
};
const time = (v: string) =>
  new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Paris',
  }).format(new Date(v));
const euro = (v: string | number) =>
  new Intl.NumberFormat('en-IE', {
    style: 'currency',
    currency: 'EUR',
    maximumFractionDigits: 0,
  }).format(Number(v));
const clean = (s: string) =>
  s
    .replace(/\s*\(fixture\)/g, '')
    .replace(/fixture provider/g, 'demo provider');
function planTitle(p: Plan) {
  const d = p.intent_quality.intent_restaurant || 0,
    m = p.intent_quality.intent_ticket || 0;
  return d === 1
    ? 'Keep the celebration'
    : d > 0 && m > 0
      ? 'Dinner and a movie'
      : d > 0
        ? 'A quieter evening'
        : m > 0
          ? 'Keep the entertainment'
          : 'Focus on arrival';
}
function planSubtitle(p: Plan) {
  const d = p.intent_quality.intent_restaurant || 0,
    m = p.intent_quality.intent_ticket || 0;
  return d === 1
    ? 'Make room for a special dinner. Let the movie go.'
    : d > 0 && m > 0
      ? 'A simpler dinner makes time for a later screening.'
      : d > 0
        ? 'A quick dinner, with more room to settle in.'
        : m > 0
          ? 'Recover the dinner deposit and catch another screening.'
          : 'Protect transport and accommodation. Recover what you can.';
}
const activityTitles: Record<string, string> = {
  'state.assessed': 'Change detected and checked',
  'recovery.planned': 'Feasible recoveries compared',
  'plan.approved': 'You approved a recovery',
  'plan.rejected': 'You declined a recovery',
  'execution.started': 'Simulation started',
  'action.started': 'Applying an approved change',
  'action.verified': 'Change verified',
  'action.failed': 'Action failed',
  'execution.blocked': 'Execution paused for review',
  'execution.cancelled': 'Remaining changes cancelled',
  'incident.resolved': 'Your evening is back on track',
  'reasoning.completed': 'Nemotron returned structured reasoning',
  'reasoning.failed': 'Reasoning unavailable',
  'reasoning.rejected': 'Invalid model output rejected',
  'event.extracted': 'Event interpreted',
  'extraction.confirmed': 'You confirmed a timing update',
  'recovery.reasoned': 'Recovery tradeoffs explained',
  'demo.reset': 'Demo reset',
};
function activityDetail(a: Audit) {
  const o = a.outcome as { explanation?: string } | undefined;
  return o?.explanation
    ? clean(o.explanation)
    : a.type === 'recovery.planned'
      ? 'Candidate worlds passed the timing and travel checks.'
      : a.type === 'plan.approved'
        ? 'Approval is bound to this plan and its exact spending amount.'
        : a.type === 'incident.resolved'
          ? 'No hard timing conflicts remain.'
          : a.type === 'state.assessed'
            ? 'The updated state was checked across downstream dependencies.'
            : '';
}

export default function Home() {
  const [workspace, setWorkspace] = useState<Workspace>(
      replay.initial as unknown as Workspace,
    ),
    [connected, setConnected] = useState(false),
    [busy, setBusy] = useState(''),
    [error, setError] = useState(''),
    [selected, setSelected] = useState('flight'),
    [tab, setTab] = useState('overview'),
    [review, setReview] = useState<Plan | null>(null),
    [preview, setPreview] = useState<Plan | null>(null),
    [eventOpen, setEventOpen] = useState(false),
    [eventText, setEventText] = useState(
      'My flight to Nice on September 11, 2026 now arrives at 19:05 local time instead of 16:10.',
    ),
    [extraction, setExtraction] = useState<Extraction | null>(null);
  const local = useSyncExternalStore(
    () => () => {},
    isLocal,
    () => false,
  );
  const cancelRef = useRef(false);
  const state = workspace.state,
    conflicts = state.assessment.violations,
    disrupted = conflicts.length > 0;
  const execution = workspace.executions.at(-1),
    running = execution?.status === 'RUNNING',
    recovered = execution?.status === 'SUCCEEDED';
  const incident = [...workspace.incidents]
    .reverse()
    .find((i) => i.status === 'OPEN');
  const planning = workspace.planning,
    plans = planning?.candidates ?? EMPTY_PLANS;
  const comparison =
    workspace.assisted?.planning.id === planning?.id
      ? workspace.assisted?.comparison
      : null;
  const shownWorld = preview?.world || state.world;
  const picked =
    shownWorld.commitments.find((c) => c.id === selected) ||
    shownWorld.commitments[0];
  const currentViolation = conflicts.filter((v) =>
    v.affected_commitment_ids.includes(selected),
  );
  const affected = new Set(incident?.affected_commitment_ids || []);
  async function refresh() {
    const result = await api<Workspace>('/v1/workspace');
    setWorkspace(result);
    setConnected(true);
    return result;
  }
  useEffect(() => {
    let active = true;
    void api<Workspace>('/v1/workspace')
      .then((result) => {
        if (active) {
          setWorkspace(result);
          setConnected(true);
        }
      })
      .catch(() => {
        if (active)
          setError(
            'Your workspace is unavailable. Start the Cascade API, then reconnect.',
          );
      });
    return () => {
      active = false;
    };
  }, []);
  async function perform(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError('');
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
      await refresh().catch(() => {});
    } finally {
      setBusy('');
    }
  }
  async function planCurrent(ai = false) {
    const current = await refresh();
    const active = [...current.incidents]
      .reverse()
      .find((i) => i.status === 'OPEN');
    if (!active) throw new Error('There is no open incident to plan.');
    await api(`/v1/incidents/${active.id}/plan${ai ? '/assisted' : ''}`, {
      expected_version: current.state.world.version,
    });
    setPreview(null);
    await refresh();
  }
  async function inject() {
    await perform('Finding recoveries', async () => {
      await api('/v1/demo/scenarios/flight_delay/inject', {});
      await planCurrent();
    });
  }
  async function reset() {
    await perform('Resetting demo', async () => {
      await api('/v1/demo/reset', {});
      setPreview(null);
      setReview(null);
      setExtraction(null);
      setSelected('flight');
      await refresh();
    });
  }
  async function drive(start: Execution) {
    let current = start;
    cancelRef.current = false;
    setBusy('Simulating approved changes');
    try {
      while (current.status === 'RUNNING' && !cancelRef.current) {
        current = await api<Execution>(`/v1/executions/${current.id}/advance`, {
          expected_step: current.next_step,
        });
        await refresh();
        if (current.status === 'RUNNING')
          await new Promise((r) => setTimeout(r, 350));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      await refresh().catch(() => {});
      setBusy('');
    }
  }
  async function approve() {
    if (!review || !incident) return;
    const chosen = review;
    await perform('Approving recovery', async () => {
      const approval = await api<Approval>(
        `/v1/incidents/${incident.id}/approve`,
        { plan_id: chosen.id, expected_version: state.world.version },
      );
      const started = await api<Execution>(
        `/v1/recovery-plans/${chosen.id}/execute`,
        { approval_id: approval.id, expected_version: state.world.version },
      );
      setReview(null);
      setPreview(null);
      await refresh();
      await drive(started);
    });
  }
  async function reject() {
    if (!review || !incident) return;
    await perform('Declining recovery', async () => {
      await api(`/v1/incidents/${incident.id}/reject`, {
        plan_id: review.id,
        expected_version: state.world.version,
      });
      setReview(null);
      await refresh();
    });
  }
  async function cancel() {
    if (!execution) return;
    cancelRef.current = true;
    await api(`/v1/executions/${execution.id}/cancel`, {});
    await refresh();
  }
  async function interpret() {
    await perform('Interpreting your update', async () => {
      const result = await api<Extraction>('/v1/events/text', {
        event_id: crypto.randomUUID(),
        expected_version: state.world.version,
        text: eventText,
        apply: false,
      });
      setExtraction(result);
      await refresh();
    });
  }
  async function confirmEvent() {
    if (!extraction) return;
    await perform('Checking consequences', async () => {
      const result = await api<Extraction>(
        `/v1/extractions/${extraction.id}/confirm`,
        { expected_version: extraction.based_on_version },
      );
      setExtraction(result);
      setEventOpen(false);
      await refresh();
      if (result.event_result?.incident) await planCurrent();
    });
  }
  const actionRefs = useRef({ workspace, plans, setReview });
  useEffect(() => {
    actionRefs.current = { workspace, plans, setReview };
  }, [workspace, plans]);
  useEffect(() => {
    const context = (
      document as Document & {
        modelContext?: {
          registerTool: (
            tool: unknown,
            options: { signal: AbortSignal },
          ) => void | Promise<void>;
        };
      }
    ).modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    const register = (tool: unknown) => {
      try {
        void Promise.resolve(
          context.registerTool(tool, { signal: lifecycle.signal }),
        ).catch(() => {});
      } catch {}
    };
    register({
      name: 'cascade_read_workspace',
      description:
        'Read current commitment and recovery state. Does not change anything.',
      inputSchema: {
        type: 'object',
        properties: {},
        additionalProperties: false,
      },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      execute: () => ({
        version: actionRefs.current.workspace.state.world.version,
        conflicts:
          actionRefs.current.workspace.state.assessment.violations.length,
        plans: actionRefs.current.plans.map((p) => ({
          id: p.id,
          title: planTitle(p),
          additional_cost: p.additional_cost,
        })),
      }),
    });
    register({
      name: 'cascade_review_plan',
      description:
        'Open a recovery plan for human review. Does not approve or execute it.',
      inputSchema: {
        type: 'object',
        properties: { plan_id: { type: 'string' } },
        required: ['plan_id'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      execute: (input: unknown) => {
        const id = (input as { plan_id?: unknown })?.plan_id;
        if (typeof id !== 'string') throw new Error('plan_id is required');
        const plan = actionRefs.current.plans.find((p) => p.id === id);
        if (!plan) throw new Error('Unknown plan');
        actionRefs.current.setReview(plan);
        return { status: 'review_opened', plan_id: id, approved: false };
      },
    });
    return () => lifecycle.abort();
  }, []);
  return (
    <div className="workspace">
      <header className="topbar">
        <Link href="/" className="wordmark" aria-label="Cascade home">
          <GitBranch aria-hidden="true" />
          cascade<span className="beta">PERSONAL AI</span>
        </Link>
        <div className="top-status">
          <i className={`status-dot ${connected ? 'green' : ''}`} />
          {local
            ? workspace.reasoning?.configured
              ? 'Nemotron connected'
              : 'Local workspace'
            : 'Interactive demo'}
          <span className="avatar">CH</span>
        </div>
      </header>
      <main>
        <div className="breadcrumbs">
          YOUR WORLD
          <ChevronRight size={14} />
          NICE, FRANCE<span>11 SEP 2026</span>
        </div>
        <div className="page-heading">
          <div>
            <p className="eyebrow">FRIDAY’S ITINERARY</p>
            <h1>
              {recovered ? (
                'Your evening, recovered.'
              ) : (
                <>
                  One evening. <span>Everything connected.</span>
                </>
              )}
            </h1>
          </div>
          <div className="heading-actions">
            {local && (
              <Button
                variant="outline"
                className="secondary-action"
                disabled={!!busy || running}
                onClick={() => {
                  setExtraction(null);
                  setEventOpen(true);
                }}
              >
                <MessageSquareText />
                Add an update
              </Button>
            )}
            {!disrupted && !recovered ? (
              <Button
                className="primary-action"
                disabled={!connected || !!busy}
                onClick={inject}
              >
                {busy ? <LoaderCircle className="spin" /> : <Play />}
                {busy || 'Simulate flight delay'}
              </Button>
            ) : (
              <Button
                variant="outline"
                className="secondary-action"
                disabled={!!busy || running}
                onClick={reset}
              >
                <RotateCcw />
                Reset demo
              </Button>
            )}
          </div>
        </div>
        <output className={`health-banner ${disrupted ? 'alert' : ''}`}>
          <div className="health-icon">
            {disrupted ? <Activity /> : <ShieldCheck />}
          </div>
          <div>
            <strong>
              {recovered
                ? 'A feasible evening. Your choice, carried through.'
                : disrupted
                  ? `${conflicts.length} timing conflicts need attention.`
                  : 'Your world is stable.'}
            </strong>
            <p>
              {recovered
                ? 'Approved changes are verified in the simulated provider state.'
                : disrupted
                  ? 'Cascade has traced the consequences. Choose what matters most from here.'
                  : 'Your plans fit together. Cascade is ready when reality changes.'}
            </p>
          </div>
          <span className="banner-count">
            {disrupted ? `${affected.size} affected` : 'All clear'}
            {!disrupted && <Check size={16} />}
          </span>
        </output>
        {!local && (
          <p className="preview-notice">
            Verified demo replay · explore the full recovery flow with sample
            data. Live event interpretation runs in the connected local
            workspace.
          </p>
        )}
        {error && (
          <div role="alert" className="error-banner">
            {error}
            <Button
              variant="ghost"
              onClick={() =>
                perform('Reconnecting', async () => {
                  await refresh();
                })
              }
            >
              Reconnect
            </Button>
          </div>
        )}
        <Tabs value={tab} onValueChange={(v) => setTab(String(v))}>
          <div className="section-toolbar">
            <TabsList variant="line">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="audit">
                Activity{' '}
                <span className="tiny-count">{workspace.audit.length}</span>
              </TabsTrigger>
            </TabsList>
            <span className="meta">
              {state.world.commitments.length} commitments ·{' '}
              {state.world.dependencies.length} connections
            </span>
          </div>
          <TabsContent value="overview">
            <div className="overview-grid">
              <section className="graph-panel">
                <div className="panel-heading">
                  <div>
                    <GitBranch size={18} />
                    <h2>
                      {preview ? 'Recovery preview' : 'The ripple effect'}
                    </h2>
                  </div>
                  {preview ? (
                    <Button variant="ghost" onClick={() => setPreview(null)}>
                      Back to current
                      <X size={14} />
                    </Button>
                  ) : (
                    <span className="legend">
                      <i />
                      {disrupted ? 'Affected' : 'Connected'}
                    </span>
                  )}
                </div>
                <div className="graph-canvas">
                  <div className="graph-track">
                    {shownWorld.commitments.map((c, i) => {
                      const Icon = icons[c.kind] || CircleDot;
                      const atRisk =
                        !preview &&
                        conflicts.some((v) =>
                          v.affected_commitment_ids.includes(c.id),
                        );
                      const edge = shownWorld.dependencies.find(
                        (d) =>
                          d.from_id === c.id &&
                          d.to_id === shownWorld.commitments[i + 1]?.id,
                      );
                      return (
                        <div className="graph-stop" key={c.id}>
                          <button
                            onClick={() => setSelected(c.id)}
                            className={`commitment-node ${selected === c.id ? 'selected' : ''} ${atRisk ? 'affected' : ''}`}
                            aria-pressed={selected === c.id}
                          >
                            <span className="node-icon">
                              <Icon size={22} />
                            </span>
                            <span className="node-time">
                              {time(c.start_at)}
                            </span>
                            <strong>{names[c.kind] || c.title}</strong>
                            <span className="node-state">
                              {preview
                                ? 'Proposed'
                                : atRisk
                                  ? 'At risk'
                                  : recovered
                                    ? 'Verified'
                                    : 'On track'}
                            </span>
                          </button>
                          {i < shownWorld.commitments.length - 1 && (
                            <span
                              className={`connector ${edge ? '' : 'no-edge'}`}
                              title={edge?.explanation}
                            >
                              {edge && <ArrowRight size={15} />}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <div className="graph-footnote">
                    <CircleDot size={14} />
                    {preview
                      ? 'Hypothetical itinerary. Nothing changes until you approve.'
                      : 'A change travels through these dependencies. Select a commitment to inspect it.'}
                  </div>
                </div>
              </section>
              <aside className="detail-panel">
                <p className="eyebrow">
                  {preview ? 'PROPOSED COMMITMENT' : 'COMMITMENT DETAILS'}
                </p>
                <h2>{clean(picked?.title || 'Itinerary')}</h2>
                <p className="detail-time">
                  {picked ? time(picked.start_at) : '—'}
                  <span>Nice local time</span>
                </p>
                <div className="divider" />
                <h3>
                  {disrupted && !preview ? 'What is at risk' : 'Why it matters'}
                </h3>
                <p>
                  {disrupted && !preview
                    ? currentViolation[0]?.explanation ||
                      'No direct timing violation.'
                    : shownWorld.intents.find((i) => i.id === picked?.intent_id)
                        ?.description}
                </p>
                {disrupted && !preview && currentViolation[0] && (
                  <p className="required-time">
                    Earliest possible: {time(currentViolation[0].actual_at)}
                  </p>
                )}
                <div className="confidence-note">
                  <ShieldCheck size={18} />
                  Checked with explicit timing rules
                </div>
              </aside>
            </div>
            {execution && (
              <section
                className={`execution-panel ${execution.status === 'SUCCEEDED' ? 'success' : ''}`}
                aria-live="polite"
              >
                <div className="execution-heading">
                  <div>
                    {execution.status === 'SUCCEEDED' ? (
                      <CheckCircle2 />
                    ) : (
                      <Activity />
                    )}
                    <h2>
                      {execution.status === 'SUCCEEDED'
                        ? 'Recovery complete'
                        : execution.status === 'RUNNING'
                          ? 'Putting your evening back together'
                          : `Execution ${execution.status.toLowerCase()}`}
                    </h2>
                  </div>
                  <span>
                    {
                      execution.outcomes.filter((o) => o.status === 'VERIFIED')
                        .length
                    }{' '}
                    / {execution.total_steps} verified
                  </span>
                </div>
                <Progress
                  value={(execution.next_step / execution.total_steps) * 100}
                />
                <div className="execution-steps">
                  {execution.outcomes.map((o) => (
                    <span key={o.action_id}>
                      {o.status === 'VERIFIED' ? (
                        <Check size={15} />
                      ) : (
                        <X size={15} />
                      )}{' '}
                      {names[o.commitment_id] || o.commitment_id}
                    </span>
                  ))}
                </div>
                <p>{execution.message}</p>
                {running && (
                  <div className="execution-controls">
                    <Button
                      variant="outline"
                      onClick={() => cancel().catch((e) => setError(e.message))}
                    >
                      Cancel remaining
                    </Button>
                    {!busy && (
                      <Button onClick={() => drive(execution)}>
                        Continue simulation
                      </Button>
                    )}
                  </div>
                )}
              </section>
            )}
            {disrupted && !running && (
              <div className="recovery-heading">
                <div>
                  <p className="eyebrow">FEASIBLE RECOVERIES</p>
                  <h2>Keep what matters. See what changes.</h2>
                  <p>
                    Each option protects transport and accommodation. The
                    tradeoff is your evening.
                  </p>
                </div>
                <div className="heading-actions">
                  <Button
                    variant="outline"
                    disabled={!!busy}
                    onClick={() =>
                      perform('Comparing recoveries', () => planCurrent())
                    }
                  >
                    {busy ? <LoaderCircle className="spin" /> : <RotateCcw />}
                    {plans.length ? 'Replan' : 'Find recoveries'}
                  </Button>
                  {local && workspace.reasoning?.configured && (
                    <Button
                      variant="outline"
                      disabled={!!busy}
                      onClick={() =>
                        perform('Nemotron is comparing tradeoffs', () =>
                          planCurrent(true),
                        )
                      }
                    >
                      <Sparkles />
                      Explain tradeoffs
                    </Button>
                  )}
                </div>
              </div>
            )}
            {!!busy && !running && (
              <output className="working-status">
                <LoaderCircle className="spin" size={16} />
                {busy}…
              </output>
            )}
            {workspace.assisted?.warnings?.map((w) => (
              <p className="warning-note" key={w}>
                {w}
              </p>
            ))}
            {disrupted && !running && plans.length > 0 && (
              <div className="plans-grid">
                {plans.map((p, i) => {
                  const rejected = workspace.approvals.some(
                    (a) => a.plan_id === p.id && a.status === 'REJECTED',
                  );
                  const stale = p.based_on_version !== state.world.version;
                  const explanation = comparison?.plans.find(
                    (e) => e.plan_id === p.id,
                  );
                  return (
                    <article
                      className={`plan-card ${preview?.id === p.id ? 'previewing' : ''} ${rejected ? 'declined' : ''}`}
                      key={p.id}
                    >
                      <div className="plan-number">
                        OPTION {String(i + 1).padStart(2, '0')}
                        <span>
                          <ShieldCheck size={13} />
                          Feasible
                        </span>
                      </div>
                      <h3>{planTitle(p)}</h3>
                      <p className="plan-description">{planSubtitle(p)}</p>
                      <div className="plan-cost">
                        {euro(p.additional_cost)}
                        <span>additional spend</span>
                      </div>
                      <div className="plan-finance">
                        <span>{euro(p.loss)} lost</span>
                        <span>{euro(p.refund)} proposed refund</span>
                      </div>
                      <ul className="plan-actions">
                        {p.actions.map((a) => (
                          <li key={a.id}>
                            <span
                              className={
                                a.intent_quality === 0 ? 'lost' : 'kept'
                              }
                            >
                              {a.intent_quality === 0 ? (
                                <X size={15} />
                              ) : (
                                <Check size={15} />
                              )}
                            </span>
                            <span>
                              <strong>
                                {names[a.commitment_id] || a.commitment_id}
                              </strong>
                              <small>
                                {a.resolution === 'ABANDONED'
                                  ? 'Let go'
                                  : a.resolution === 'COMPENSATED'
                                    ? 'Deposit refund proposed'
                                    : a.intent_quality < 1
                                      ? 'Simpler alternative'
                                      : a.resolution === 'PRESERVED'
                                        ? 'Keep original'
                                        : 'Substitute confirmed in demo'}
                              </small>
                            </span>
                          </li>
                        ))}
                      </ul>
                      {explanation && (
                        <details className="ai-explanation">
                          <summary>
                            <Sparkles size={13} />
                            Nemotron’s comparison
                          </summary>
                          <p>{explanation.explanation}</p>
                          <p>{explanation.tradeoff}</p>
                        </details>
                      )}
                      <div className="plan-buttons">
                        <Button
                          variant="ghost"
                          onClick={() => {
                            setPreview(p);
                            setSelected('flight');
                          }}
                        >
                          Preview itinerary
                        </Button>
                        <Button
                          disabled={!!busy || stale || rejected}
                          onClick={() => setReview(p)}
                        >
                          {rejected
                            ? 'Declined'
                            : stale
                              ? 'Replan required'
                              : 'Review'}
                          <ArrowRight size={14} />
                        </Button>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
            {disrupted && planning && plans.length === 0 && (
              <section className="recovery-empty">
                <ShieldCheck />
                <div>
                  <h2>
                    {planning.status === 'BLOCKED'
                      ? 'Availability could not be confirmed.'
                      : 'No feasible recovery under these limits.'}
                  </h2>
                  <p>{planning.notes.join(' ')}</p>
                </div>
              </section>
            )}
            {planning?.provider_results && disrupted && (
              <details className="exhaustion">
                <summary>
                  Why the original plan cannot be restored{' '}
                  <span>{planning.tool_calls} providers checked</span>
                </summary>
                <div>
                  {planning.provider_results.map((p) => (
                    <section key={p.commitment_id}>
                      <h3>{names[p.commitment_id]}</h3>
                      {Object.entries(p.exhausted).map(([stage, reason]) => (
                        <p key={stage}>{reason}</p>
                      ))}
                    </section>
                  ))}
                </div>
              </details>
            )}
            {!disrupted && !execution && (
              <section className="recovery-empty">
                <div className="recovery-symbol">
                  <Sparkles size={24} />
                </div>
                <div>
                  <p className="eyebrow">RECOVERY WORKSPACE</p>
                  <h2>A plan for when plans change.</h2>
                  <p>
                    Simulate the delay to follow the cascade from disruption to
                    recovery.
                  </p>
                </div>
              </section>
            )}
          </TabsContent>
          <TabsContent value="audit">
            <section className="audit-panel">
              <div className="panel-heading">
                <div>
                  <Activity size={18} />
                  <h2>Every decision leaves a trail.</h2>
                </div>
                <span className="meta">{workspace.audit.length} events</span>
              </div>
              {workspace.audit.length === 0 ? (
                <p className="empty-audit">
                  Your itinerary is ready. Changes and decisions will appear
                  here.
                </p>
              ) : (
                <ol className="audit-list">
                  {[...workspace.audit].reverse().map((a, i) => (
                    <li key={`${a.type}-${i}`}>
                      <span
                        className={`audit-dot ${a.type.includes('failed') || a.type.includes('blocked') ? 'warn' : ''}`}
                      >
                        {a.type.includes('verified') ||
                        a.type === 'incident.resolved' ? (
                          <Check size={14} />
                        ) : (
                          <CircleDot size={14} />
                        )}
                      </span>
                      <div>
                        <strong>
                          {activityTitles[a.type] ||
                            a.type.replaceAll('.', ' ')}
                        </strong>
                        <p>{activityDetail(a)}</p>
                        <details>
                          <summary>Inspect evidence</summary>
                          <pre>{JSON.stringify(a, null, 2)}</pre>
                        </details>
                      </div>
                      <span className="audit-index">
                        {String(workspace.audit.length - i).padStart(2, '0')}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </TabsContent>
        </Tabs>
        <footer>
          <span>
            <ShieldCheck size={14} />
            You stay in control of consequential changes.
          </span>
          <span>
            {local ? 'LIVE WORKSPACE' : 'DEMO REPLAY'} · NO REAL BOOKINGS
          </span>
        </footer>
      </main>
      <Dialog
        open={!!review}
        onOpenChange={(open) => {
          if (!open && !busy) setReview(null);
        }}
      >
        <DialogContent className="review-dialog">
          <DialogHeader>
            <p className="eyebrow">YOUR APPROVAL</p>
            <DialogTitle>
              {review ? planTitle(review) : 'Review recovery'}
            </DialogTitle>
            <DialogDescription>
              Review the exact changes. Approval applies only to this plan and
              its additional spending.
            </DialogDescription>
          </DialogHeader>
          {review && (
            <>
              <div className="approval-finance">
                <div>
                  <strong>{euro(review.additional_cost)}</strong>
                  <span>Additional spend</span>
                </div>
                <div>
                  <strong>{euro(review.loss)}</strong>
                  <span>Unrecoverable loss</span>
                </div>
                <div>
                  <strong>{euro(review.refund)}</strong>
                  <span>Proposed refund</span>
                </div>
              </div>
              <ul className="review-actions">
                {review.actions.map((a) => (
                  <li key={a.id}>
                    <span className="resolution-label">
                      {a.resolution.toLowerCase()}
                    </span>
                    <strong>{clean(a.explanation)}</strong>
                    {a.replacement && (
                      <span>
                        {time(a.replacement.start_at)} –{' '}
                        {time(a.replacement.end_at)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="approval-note">
                <ShieldCheck size={18} />
                This runs against simulated providers. No real reservation or
                payment will change.
              </p>
              <div className="dialog-actions">
                <Button variant="ghost" disabled={!!busy} onClick={reject}>
                  Decline this option
                </Button>
                <Button
                  className="primary-action"
                  disabled={!!busy}
                  onClick={approve}
                >
                  {busy ? <LoaderCircle className="spin" /> : <Check />}Approve
                  & simulate
                </Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
      <Dialog
        open={eventOpen}
        onOpenChange={(open) => {
          if (!busy) setEventOpen(open);
        }}
      >
        <DialogContent className="event-dialog">
          <DialogHeader>
            <DialogTitle>What changed?</DialogTitle>
            <DialogDescription>
              Tell Cascade about a timing update. You’ll review its
              interpretation before anything changes.
            </DialogDescription>
          </DialogHeader>
          <label htmlFor="event-text">Your update</label>
          <Textarea
            id="event-text"
            value={eventText}
            onChange={(e) => {
              setEventText(e.target.value);
              setExtraction(null);
            }}
            rows={4}
          />
          <Button disabled={!!busy || !eventText.trim()} onClick={interpret}>
            {busy ? <LoaderCircle className="spin" /> : <Sparkles />}Interpret
            update
          </Button>
          {extraction && (
            <div className="extraction-result">
              <strong>{extraction.status.replaceAll('_', ' ')}</strong>
              <p>{extraction.extraction.explanation}</p>
              {extraction.mutation && (
                <>
                  <p>
                    {names[extraction.mutation.commitment_id] ||
                      extraction.mutation.commitment_id}
                    : {time(extraction.mutation.new_start_at)} · interpretation
                    confidence{' '}
                    {Math.round(extraction.extraction.confidence * 100)}%
                  </p>
                  <Button disabled={!!busy} onClick={confirmEvent}>
                    Confirm timing update
                    <Check />
                  </Button>
                </>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
