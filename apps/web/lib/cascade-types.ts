export type Commitment = {
  id: string;
  title: string;
  kind: string;
  intent_id: string;
  start_at: string;
  end_at: string;
  source: { source: string; external_id: string };
};
export type Edge = {
  id: string;
  from_id: string;
  to_id: string;
  lag_minutes: number;
  explanation: string;
};
export type Violation = {
  constraint_id: string;
  explanation: string;
  affected_commitment_ids: string[];
  actual_at: string;
  required_at: string;
  severity: string;
  delay_minutes: number;
};
export type World = {
  version: number;
  commitments: Commitment[];
  dependencies: Edge[];
  intents: { id: string; description: string; importance: number }[];
};
export type State = {
  world: World;
  assessment: {
    violations: Violation[];
    projected_starts: Record<string, string>;
  };
};
export type Action = {
  id: string;
  commitment_id: string;
  resolution: string;
  replacement: Commitment | null;
  explanation: string;
  additional_cost: string;
  loss: string;
  refund: string;
  evidence: string;
  intent_quality: number;
};
export type Plan = {
  id: string;
  based_on_version: number;
  world: World;
  actions: Action[];
  additional_cost: string;
  loss: string;
  refund: string;
  intent_quality: Record<string, number>;
  explanation: string;
  status: string;
};
export type Planning = {
  id: string;
  incident_id: string;
  based_on_version: number;
  status: string;
  candidates: Plan[];
  tool_calls: number;
  expansions: number;
  provider_results: {
    commitment_id: string;
    exhausted: Record<string, string>;
    evidence: string;
  }[];
  notes: string[];
};
export type Incident = {
  id: string;
  status: string;
  severity: string | null;
  trigger_commitment_id: string;
  affected_commitment_ids: string[];
  violations: Violation[];
};
export type Outcome = {
  action_id: string;
  commitment_id: string;
  resolution: string;
  status: string;
  explanation: string;
  occurred_at: string;
  side_effect: boolean;
};
export type Execution = {
  id: string;
  plan_id: string;
  approval_id: string;
  status: string;
  next_step: number;
  total_steps: number;
  expected_world_version: number;
  outcomes: Outcome[];
  message: string;
};
export type Approval = {
  id: string;
  plan_id: string;
  status: string;
  additional_cost: string;
};
export type Audit = { type: string; [key: string]: unknown };
export type Comparison = {
  plans: { plan_id: string; explanation: string; tradeoff: string }[];
  recommended_plan_id: string | null;
  rationale: string;
};
export type Workspace = {
  state: State;
  incidents: Incident[];
  planning: Planning | null;
  assisted: {
    planning: Planning;
    comparison: Comparison | null;
    warnings: string[];
  } | null;
  approvals: Approval[];
  executions: Execution[];
  audit: Audit[];
  reasoning: { configured: boolean; live_verified: boolean } | null;
};
export type Extraction = {
  id: string;
  status: string;
  based_on_version: number;
  mutation: {
    commitment_id: string;
    new_start_at: string;
    new_end_at: string;
  } | null;
  extraction: {
    explanation: string;
    confidence: number;
    evidence_quote: string;
  };
  event_result: { incident: Incident | null } | null;
};
