'use client';

import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge as FlowEdge,
  type Node as FlowNode,
  type NodeProps,
} from '@xyflow/react';
import {
  CircleDot,
  Hotel,
  PlaneLanding,
  Ticket,
  TramFront,
  Utensils,
} from 'lucide-react';
import { useMemo } from 'react';
import type { Commitment, Edge, Violation, World } from '@/lib/cascade-types';

type CommitmentData = {
  commitment: Commitment;
  atRisk: boolean;
  selected: boolean;
  status: string;
  onSelect: () => void;
};

type CommitmentFlowNode = FlowNode<CommitmentData, 'commitment'>;

const icons = {
  flight: PlaneLanding,
  transfer: TramFront,
  hotel: Hotel,
  restaurant: Utensils,
  ticket: Ticket,
} as const;

const labels = {
  flight: 'Flight',
  transfer: 'Transfer',
  hotel: 'Hotel',
  restaurant: 'Dinner',
  ticket: 'Movie',
} as const;

function shortTime(value: string) {
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
}

function FlowCommitmentNode({ data }: NodeProps<CommitmentFlowNode>) {
  const Icon = icons[data.commitment.kind as keyof typeof icons] || CircleDot;
  return (
    <div
      className={`flow-commitment-node ${data.atRisk ? 'at-risk' : ''} ${data.selected ? 'selected' : ''}`}
    >
      <Handle type="target" position={Position.Left} />
      <button
        type="button"
        className="flow-node-button"
        aria-pressed={data.selected}
        aria-label={`Inspect ${labels[data.commitment.kind as keyof typeof labels] || data.commitment.title}`}
        onClick={data.onSelect}
      >
        <span className="flow-node-icon">
          <Icon size={18} />
        </span>
        <span className="flow-node-time">
          {shortTime(data.commitment.start_at)}
        </span>
        <strong>
          {labels[data.commitment.kind as keyof typeof labels] ||
            data.commitment.title}
        </strong>
        <span className="flow-node-status">{data.status}</span>
      </button>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { commitment: FlowCommitmentNode };

function levelsFor(world: World) {
  const predecessors = new Map<string, string[]>();
  for (const commitment of world.commitments)
    predecessors.set(commitment.id, []);
  for (const edge of world.dependencies) {
    predecessors.get(edge.to_id)?.push(edge.from_id);
  }
  const levels = new Map<string, number>();
  const visiting = new Set<string>();
  const level = (id: string): number => {
    const known = levels.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const value = Math.max(
      0,
      ...(predecessors.get(id) || []).map((parent) => level(parent) + 1),
    );
    visiting.delete(id);
    levels.set(id, value);
    return value;
  };
  for (const commitment of world.commitments) level(commitment.id);
  return levels;
}

function makeNodes(
  world: World,
  atRisk: Set<string>,
  selected: string,
  recovered: boolean,
  onSelect: (id: string) => void,
): CommitmentFlowNode[] {
  const levels = levelsFor(world);
  const rows = new Map<number, number>();
  return world.commitments.map((commitment) => {
    const column = levels.get(commitment.id) || 0;
    const row = rows.get(column) || 0;
    rows.set(column, row + 1);
    const risk = atRisk.has(commitment.id);
    return {
      id: commitment.id,
      type: 'commitment',
      position: { x: column * 255, y: row * 155 },
      data: {
        commitment,
        atRisk: risk,
        selected: selected === commitment.id,
        status: risk ? 'At risk' : recovered ? 'Verified' : 'On track',
        onSelect: () => onSelect(commitment.id),
      },
    };
  });
}

function makeEdges(
  world: World,
  atRisk: Set<string>,
  disrupted: boolean,
): FlowEdge[] {
  return world.dependencies.map((dependency: Edge) => {
    const highlighted =
      disrupted &&
      (atRisk.has(dependency.from_id) || atRisk.has(dependency.to_id));
    const color = highlighted ? '#e5ad68' : '#6676b8';
    return {
      id: dependency.id,
      source: dependency.from_id,
      target: dependency.to_id,
      animated: highlighted,
      label: dependency.lag_minutes ? `${dependency.lag_minutes}m` : undefined,
      labelStyle: { fill: '#9eacc4', fontSize: 11 },
      labelBgStyle: { fill: '#111923', fillOpacity: 0.9 },
      style: { stroke: color, strokeWidth: highlighted ? 2.5 : 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    };
  });
}

export function DependencyFlow({
  world,
  violations,
  selected,
  recovered,
  preview,
  onSelect,
}: {
  world: World;
  violations: Violation[];
  selected: string;
  recovered: boolean;
  preview: boolean;
  onSelect: (id: string) => void;
}) {
  const atRisk = useMemo(
    () =>
      new Set(
        preview
          ? []
          : violations.flatMap(
              (violation) => violation.affected_commitment_ids,
            ),
      ),
    [preview, violations],
  );
  const nodes = useMemo(
    () => makeNodes(world, atRisk, selected, recovered, onSelect),
    [world, atRisk, selected, recovered, onSelect],
  );
  const edges = useMemo(
    () => makeEdges(world, atRisk, !preview && atRisk.size > 0),
    [world, atRisk, preview],
  );
  return (
    <div className="flow-graph-shell">
      <div
        className="flow-graph-canvas"
        aria-label="Commitment dependency graph"
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.25, minZoom: 0.72, maxZoom: 1.25 }}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          zoomOnDoubleClick={false}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#34435e" gap={18} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="graph-footnote">
        <CircleDot size={14} />
        {preview
          ? 'Hypothetical itinerary. Nothing changes until you approve.'
          : atRisk.size
            ? 'Amber edges show the timing ripple through downstream dependencies.'
            : 'Select a commitment to inspect it. Drag the graph to explore the connections.'}
      </div>
    </div>
  );
}
