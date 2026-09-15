"use strict";

const state = {
  world: null,
  assessment: null,
  incident: null,
  planning: null,
  selected: null,
  skills: [],
  skill: null,
  approval: null,
  execution: null,
  extraction: null,
  authRole: null,
};

const el = (id) => document.getElementById(id);

async function call(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/v1/auth/")) showLogin();
    throw new Error(body.detail || `${options?.method || "GET"} ${path} failed`);
  }
  return body;
}

function showLogin() {
  el("login").hidden = false;
  document.body.classList.add("auth-required");
}

function hideLogin(role) {
  state.authRole = role;
  el("login").hidden = true;
  document.body.classList.remove("auth-required");
  const roleNode = el("role");
  roleNode.textContent = role;
  roleNode.hidden = false;
  el("logout").hidden = false;
  for (const id of ["inject", "reset-hint", "read-message", "live-inference"])
    el(id).disabled = role === "demo";
}

async function authenticate() {
  const response = await fetch("/v1/auth/me", { credentials: "same-origin" });
  if (response.status === 401) {
    showLogin();
    return false;
  }
  if (!response.ok) throw new Error("Authentication status unavailable");
  const me = await response.json();
  hideLogin(me.role);
  return true;
}

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.hidden = true;
  }, 6000);
}

function time(value) {
  // Show the itinerary's own wall clock. Converting to the viewer's zone would
  // silently rewrite the times the constraint engine actually reasoned about.
  return String(value).slice(11, 16);
}

function truncate(value, limit) {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function money(value) {
  return `€${Number(value).toFixed(0)}`;
}

function text(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function manifestDetails(manifest) {
  if (!manifest) return null;
  const details = document.createElement("details");
  details.className = "reasoning-manifest";
  details.append(text("summary", null, "What Nemotron saw"));
  const grid = text("div", "manifest-grid");
  for (const [label, value] of [
    ["Task", manifest.task],
    ["Model", manifest.model],
    ["Context", `${Number(manifest.bytes_sent).toLocaleString()} bytes`],
    ["Entities", manifest.entity_ids?.length || "None"],
  ])
    grid.append(text("span", null, label), text("strong", null, String(value)));
  details.append(grid);
  details.append(text("p", null, `Fields: ${(manifest.fields || []).join(", ")}`));
  if (manifest.entity_ids?.length)
    details.append(text("p", null, `IDs: ${manifest.entity_ids.join(", ")}`));
  details.append(text("code", null, `${String(manifest.input_hash).slice(0, 16)}…`));
  return details;
}

// ---------------------------------------------------------------- rendering

function renderSummary() {
  const { world, assessment } = state;
  const hard = assessment.violations.filter((v) => v.severity === "hard");
  const stable = hard.length === 0;
  const body = el("summary-body");
  body.replaceChildren();
  const stats = [
    [world.commitments.length, "active commitments", ""],
    [world.dependencies.length + world.deadlines.length, "explicit constraints", ""],
    [hard.length, "hard conflicts", stable ? "state-ok" : "state-bad"],
    [world.intents.length, "intents tracked", ""],
  ];
  for (const [value, label, className] of stats) {
    const card = text("div", `stat ${className}`.trim());
    card.append(text("b", null, String(value)), text("span", null, label));
    body.append(card);
  }
  const headline = text("div", `stat ${stable ? "state-ok" : "state-bad"}`);
  headline.append(
    text("b", null, stable ? "STABLE" : "CASCADE"),
    text(
      "span",
      null,
      stable ? "no constraint is violated" : "a change invalidated downstream plans",
    ),
  );
  body.prepend(headline);
  el("version").textContent = `world version ${world.version}`;
}

function layout(world) {
  // Longest-path layering: a node sits one column right of its deepest parent.
  const depth = Object.fromEntries(world.commitments.map((c) => [c.id, 0]));
  for (let pass = 0; pass < world.commitments.length; pass += 1) {
    for (const edge of world.dependencies) {
      depth[edge.to_id] = Math.max(depth[edge.to_id], depth[edge.from_id] + 1);
    }
  }
  const rows = {};
  const position = {};
  for (const commitment of world.commitments) {
    const column = depth[commitment.id];
    rows[column] = (rows[column] || 0) + 1;
    position[commitment.id] = { column, row: rows[column] - 1 };
  }
  return position;
}

function renderGraph() {
  const { world, assessment } = state;
  const svg = el("graph");
  svg.replaceChildren();
  const position = layout(world);
  const width = 200;
  const height = 62;
  const gapX = 46;
  const gapY = 20;
  const columns = Math.max(...Object.values(position).map((p) => p.column)) + 1;
  const rows = Math.max(...Object.values(position).map((p) => p.row)) + 1;
  const totalWidth = columns * width + (columns - 1) * gapX + 24;
  const totalHeight = rows * height + (rows - 1) * gapY + 24;
  svg.setAttribute("viewBox", `0 0 ${totalWidth} ${totalHeight}`);
  // The viewBox scales the graph to the panel; the stylesheet's floor only forces a
  // horizontal scroll once the panel is too narrow to read at all.

  const broken = new Set();
  const brokenEdges = new Set();
  for (const violation of assessment.violations) {
    if (violation.severity !== "hard") continue;
    broken.add(violation.affected_commitment_ids.at(-1));
    brokenEdges.add(violation.constraint_id);
  }
  // A closed incident stops colouring the graph: the blast radius is history.
  const open = state.incident?.status === "OPEN" ? state.incident : null;
  const affected = new Set(open?.affected_commitment_ids || []);
  const trigger = open?.trigger_commitment_id;
  const box = (id) => {
    const { column, row } = position[id];
    return { x: 12 + column * (width + gapX), y: 12 + row * (height + gapY) };
  };

  const svgNode = (name, attributes) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  };

  for (const edge of world.dependencies) {
    const from = box(edge.from_id);
    const to = box(edge.to_id);
    const x1 = from.x + width;
    const y1 = from.y + height / 2;
    const x2 = to.x;
    const y2 = to.y + height / 2;
    const mid = (x1 + x2) / 2;
    const line = svgNode("path", {
      d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
      fill: "none",
      class: `edge ${brokenEdges.has(edge.id) ? "broken" : ""}`.trim(),
    });
    const label = svgNode("text", {
      x: mid,
      y: (y1 + y2) / 2 - 6,
      "text-anchor": "middle",
      class: "edge-label",
    });
    label.textContent = `+${edge.lag_minutes}m`;
    svg.append(line, label);
  }

  for (const commitment of world.commitments) {
    const { x, y } = box(commitment.id);
    let tone = "";
    if (commitment.id === trigger) tone = "trigger";
    else if (broken.has(commitment.id)) tone = "broken";
    else if (affected.has(commitment.id)) tone = "affected";
    const rect = svgNode("rect", {
      x,
      y,
      width,
      height,
      rx: 10,
      class: `node-box ${tone}`.trim(),
    });
    const title = svgNode("text", { x: x + 14, y: y + 25, class: "node-title" });
    title.textContent = truncate(commitment.title, 26);
    const full = svgNode("title", {});
    full.textContent = commitment.title;
    title.append(full);
    const when = svgNode("text", { x: x + 14, y: y + 44, class: "node-time" });
    const projected = state.assessment.projected_starts[commitment.id];
    const scheduled = time(commitment.start_at);
    const earliest = time(projected);
    when.textContent =
      scheduled === earliest ? scheduled : `${scheduled} → earliest ${earliest}`;
    svg.append(rect, title, when);
  }
}

function renderIncident() {
  const section = el("incident");
  if (!state.incident) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const incident = state.incident;
  const body = el("incident-body");
  body.replaceChildren();
  const head = text("div", "total");
  const left = text("div");
  left.append(
    text("div", null, `Trigger: ${incident.trigger_commitment_id} (${incident.trigger_event_id})`),
    text(
      "small",
      null,
      `${incident.affected_commitment_ids.length} downstream commitments, ` +
        `${incident.threatened_intent_ids.length} intents threatened`,
    ),
  );
  const right = text("div");
  if (incident.severity) {
    right.append(text("span", `severity ${incident.severity}`, incident.severity));
  }
  right.append(text("span", "pill", incident.status));
  head.append(left, right);
  body.append(head);

  const list = text("ul", "violations");
  for (const violation of state.assessment.violations) {
    const item = document.createElement("li");
    item.append(
      text("strong", null, `${violation.severity.toUpperCase()} · ${violation.constraint_id}`),
      text("span", null, violation.explanation),
      text(
        "span",
        null,
        `earliest ${time(violation.actual_at)}, required by ${time(violation.required_at)} ` +
          `(${Math.round(violation.delay_minutes)} minutes late)`,
      ),
    );
    list.append(item);
  }
  body.append(list);

  if (incident.status === "OPEN") {
    const action = text("div", "total");
    const chooser = text("label", null, "Recovery template: ");
    const menu = document.createElement("select");
    menu.className = "button";
    menu.append(new Option("match automatically", ""));
    for (const template of state.skills) {
      const suffix = template.auto_select ? "" : " (opt-in)";
      const label = `${template.name} v${template.version}${suffix}`;
      menu.append(new Option(label, template.name, false, state.skill === template.name));
    }
    menu.addEventListener("change", () => {
      state.skill = menu.value || null;
    });
    chooser.append(menu);
    const button = text("button", "button primary", "Search feasible recoveries");
    button.disabled = state.authRole === "demo";
    button.addEventListener("click", () => searchRecoveries(button));
    action.append(chooser, button);
    body.append(action);
    body.append(
      text("div", "trace", "Search is bounded and queries only fixture inventory."),
    );
  }
  el("incident-caption").textContent =
    incident.status === "OPEN"
      ? "Deterministic propagation, before any model is consulted."
      : `Closed: ${incident.resolution_note}`;
}

function qualityClass(value) {
  if (value >= 1) return ["kept", "kept"];
  if (value > 0) return ["part", "degraded"];
  return ["lost", "lost"];
}

function renderPlans() {
  const section = el("plans");
  if (!state.planning) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const body = el("plans-body");
  body.replaceChildren();
  const intents = Object.fromEntries(state.world.intents.map((i) => [i.id, i.description]));

  if (!state.planning.candidates.length) {
    const empty = text("div", "row bad");
    empty.append(
      text(
        "div",
        null,
        `No feasible recovery: ${state.planning.status.replace(/_/g, " ").toLowerCase()}.`,
      ),
    );
    body.append(empty);
  }

  for (const [index, plan] of state.planning.candidates.entries()) {
    const card = text("div", `plan ${state.selected === plan.id ? "selected" : ""}`.trim());
    card.append(text("h3", null, `Option ${index + 1}`));
    const amounts = text("div", "money");
    amounts.append(
      spanWith("spend", money(plan.additional_cost)),
      spanWith("lost", money(plan.loss)),
      spanWith("recovered", money(plan.refund)),
    );
    card.append(amounts);

    const list = text("div", "intents");
    for (const [id, quality] of Object.entries(plan.intent_quality)) {
      const [tone, label] = qualityClass(quality);
      const row = text("div", "intent");
      row.append(text("span", null, intents[id] || id), text("i", tone, label));
      list.append(row);
    }
    card.append(list);

    const steps = text("ul", "actions");
    for (const action of plan.actions) {
      const item = document.createElement("li");
      item.append(
        text("span", "tag", action.resolution),
        document.createTextNode(action.explanation),
      );
      steps.append(item);
    }
    card.append(steps);

    const choose = text(
      "button",
      "button",
      state.selected === plan.id ? "Selected" : "Review this option",
    );
    choose.disabled = state.authRole === "demo";
    choose.addEventListener("click", () => reviewPlan(plan.id));
    card.append(choose);
    body.append(card);
  }

  const applied = state.planning.skill;
  el("plans-caption").textContent = applied
    ? `Template ${applied.name} v${applied.version} bounded this search. ` +
      "Every option below still passes the constraint engine."
    : "Every option below passes the constraint engine.";

  if (state.planning.notes.length) {
    const notes = text("ul", "notes");
    for (const note of state.planning.notes) notes.append(text("li", null, note));
    body.append(notes);
  }
}

function spanWith(label, value) {
  const node = text("span");
  node.append(text("b", null, value), document.createTextNode(` ${label}`));
  return node;
}

function renderApproval() {
  const section = el("approval");
  const approval = state.approval;
  if (!approval || approval.status !== "PENDING") {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const body = el("approval-body");
  body.replaceChildren();
  const rows = text("div", "rows");
  for (const item of approval.items) {
    const row = text("div", "row wait");
    const left = text("div");
    left.append(
      text("strong", null, `${item.operation} · ${item.commitment_id}`),
      text("small", null, `${item.description} — ${item.reason}`),
    );
    row.append(left, text("span", "tier", `tier ${item.risk_tier} · ${money(item.amount)}`));
    rows.append(row);
  }
  body.append(rows);

  const total = text("div", "total");
  const amount = text("div");
  amount.append(
    text("b", null, money(approval.total_amount)),
    text("small", null, "committed if you approve; nothing has happened yet"),
  );
  const approve = text("button", "button primary", "Approve and execute");
  approve.disabled = state.authRole === "demo";
  approve.addEventListener("click", () => approveAndExecute(approve));
  const reject = text("button", "button danger", "Reject");
  reject.disabled = state.authRole === "demo";
  reject.addEventListener("click", () => rejectApproval(reject));
  const buttons = text("div", "bar-actions");
  buttons.append(reject, approve);
  total.append(amount, buttons);
  body.append(total);
}

function renderExecution() {
  const section = el("execution");
  if (!state.execution) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const execution = state.execution;
  const body = el("execution-body");
  body.replaceChildren();
  const rows = text("div", "rows");
  for (const step of execution.steps) {
    const good = step.status === "EXECUTED" || step.status === "SKIPPED";
    const row = text("div", `row ${good ? "ok" : "bad"}`);
    const left = text("div");
    left.append(
      text("strong", null, `${step.status} · ${step.commitment_id} · ${step.resolution}`),
      text("small", null, step.note),
    );
    const reference = step.call?.result?.external_reference;
    row.append(
      left,
      text("span", "tier", reference ? `verified ${reference}` : "no external effect"),
    );
    rows.append(row);
  }
  body.append(rows);
  const summary = text("div", "total");
  summary.append(
    text(
      "div",
      null,
      `${execution.status} — ${execution.side_effects} side effects, ` +
        `${execution.remaining_violations.length} violations remaining, ` +
        `world version ${execution.world_version_after}`,
    ),
  );
  body.append(summary);
  if (execution.notes.length) {
    const notes = text("ul", "notes");
    for (const note of execution.notes) notes.append(text("li", null, note));
    body.append(notes);
  }
}

function renderExtraction() {
  const body = el("extraction-body");
  body.replaceChildren();
  const result = state.extraction;
  if (!result) return;
  const change = result.extraction;
  const tone =
    result.status === "APPLIED" ? "ok" : result.status === "UNSUPPORTED" ? "bad" : "wait";
  const row = text("div", `row ${tone}`);
  const left = text("div");
  left.append(
    text("strong", null, `${result.status} · ${change.outcome}`),
    text("small", null, change.explanation),
  );
  if (result.mutation) {
    const applied = result.event_result;
    left.append(
      text(
        "small",
        null,
        `${result.mutation.commitment_id} → ${time(result.mutation.new_start_at)}, ` +
          (applied
            ? `applied at world version ${applied.world_version}`
            : `proposed against world version ${result.based_on_version}`),
      ),
    );
  }
  row.append(left, text("span", "tier", `confidence ${change.confidence}`));
  body.append(row);

  if (change.evidence_quote) {
    body.append(text("p", "quote", `“${change.evidence_quote}”`));
  }
  const call = result.model_call;
  body.append(
    text(
      "div",
      "trace",
      `${call.provider} · ${call.model} · ${call.attempts} attempt(s) · ${call.elapsed_ms} ms · ` +
        `output ${call.output_hash.slice(0, 12)}…`,
    ),
  );
  const manifest = manifestDetails(call.manifest);
  if (manifest) body.append(manifest);

  if (result.mutation && result.status !== "APPLIED") {
    const confirm = text("div", "total");
    const button = text("button", "button primary", "Apply this change");
    button.disabled = state.authRole === "demo";
    button.addEventListener("click", () => confirmExtraction(button));
    confirm.append(
      text("small", null, "The model proposed it. Applying it is your decision."),
      button,
    );
    body.append(confirm);
  }
}

async function renderReasoningStatus() {
  const pill = el("reasoning-status");
  try {
    const [status, privacy] = await Promise.all([
      call("/v1/reasoning/status"),
      call("/v1/privacy"),
    ]);
    el("live-inference").checked = privacy.live_inference;
    pill.textContent = status.configured
      ? `${status.model} ${status.live_inference ? "· enabled" : "· disabled"}`
      : "Nemotron not configured — set NEBIUS_API_KEY";
  } catch {
    pill.textContent = "reasoning status unavailable";
  }
}

async function renderAudit() {
  const [audit, sandbox] = await Promise.all([call("/v1/audit"), call("/v1/security/sandbox")]);
  const list = el("audit-body");
  list.replaceChildren();
  for (const entry of audit.slice(-40).reverse()) {
    const item = document.createElement("li");
    item.append(text("b", null, entry.type || "event"));
    const detail =
      entry.incident_id || entry.event_id || entry.approval?.id || entry.result?.status || "";
    if (detail) item.append(document.createTextNode(` — ${detail}`));
    const manifest = manifestDetails(
      entry.call?.manifest || entry.result?.model_call?.manifest,
    );
    if (manifest) item.append(manifest);
    list.append(item);
  }
  const box = el("sandbox-body");
  box.replaceChildren();
  if (!sandbox.enforced) {
    box.append(text("div", null, "No execution boundary is configured."));
    return;
  }
  const profile = sandbox.policy;
  box.append(
    text(
      "div",
      null,
      `Profile ${profile.name}: ${profile.allowed_providers.length} providers allowed, ` +
        `ceiling ${money(profile.max_amount)}, network ${profile.allow_network ? "on" : "off"}.`,
    ),
  );
  for (const denial of sandbox.denials) {
    box.append(text("div", null, `DENIED ${denial.provider}/${denial.operation}: ${denial.reason}`));
  }
}

// ------------------------------------------------------------------ actions

async function refresh() {
  const [{ world, assessment }, incidents, skills] = await Promise.all([
    call("/v1/state"),
    call("/v1/incidents"),
    call("/v1/skills"),
  ]);
  state.skills = skills;
  state.world = world;
  state.assessment = assessment;
  state.incident = incidents.at(-1) || null;
  renderSummary();
  renderGraph();
  renderIncident();
  renderPlans();
  renderApproval();
  renderExecution();
  renderExtraction();
  await renderAudit();
}

async function guard(button, work) {
  if (state.authRole === "demo") {
    toast("Demo accounts have read-only gateway access.");
    return;
  }
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Working…";
  }
  try {
    await work();
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

async function inject(button) {
  await guard(button, async () => {
    await call("/v1/demo/scenarios/flight_delay/inject", { method: "POST" });
    state.planning = null;
    state.selected = null;
    state.approval = null;
    state.execution = null;
    await refresh();
  });
}

async function searchRecoveries(button) {
  await guard(button, async () => {
    state.planning = await call(`/v1/incidents/${state.incident.id}/plan`, {
      method: "POST",
      body: JSON.stringify({
        expected_version: state.world.version,
        ...(state.skill ? { skill: state.skill } : {}),
      }),
    });
    state.selected = null;
    state.approval = null;
    state.execution = null;
    await refresh();
  });
}

async function reviewPlan(planId) {
  state.selected = planId;
  state.execution = null;
  const result = await call(`/v1/recovery-plans/${planId}/execute`, {
    method: "POST",
    body: JSON.stringify({ expected_version: state.world.version }),
  }).catch((error) => {
    toast(error.message);
    return null;
  });
  if (!result) return;
  state.approval = result.approval_request;
  state.execution = result.status === "AWAITING_APPROVAL" ? null : result;
  await refresh();
}

async function approveAndExecute(button) {
  await guard(button, async () => {
    await call(`/v1/approvals/${state.approval.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        approved_action_ids: state.approval.items.map((item) => item.action_id),
        acknowledged_amount: state.approval.total_amount,
      }),
    });
    state.execution = await call(`/v1/recovery-plans/${state.selected}/execute`, {
      method: "POST",
      body: JSON.stringify({ expected_version: state.world.version }),
    });
    state.approval = null;
    state.planning = null;
    await refresh();
  });
}

async function rejectApproval(button) {
  await guard(button, async () => {
    await call(`/v1/approvals/${state.approval.id}/reject`, {
      method: "POST",
      body: JSON.stringify({ note: "Declined in the Cascade UI." }),
    });
    state.approval = null;
    await refresh();
  });
}

async function readMessage(button) {
  await guard(button, async () => {
    try {
      state.extraction = await call("/v1/events/text", {
        method: "POST",
        body: JSON.stringify({
          event_id: `ui_${Date.now()}`,
          expected_version: state.world.version,
          text: el("event-text").value,
        }),
      });
    } catch (error) {
      state.extraction = null;
      // A missing or failing model never blocks the deterministic path.
      toast(`${error.message} The deterministic simulator still works.`);
    }
    await refresh();
  });
}

async function confirmExtraction(button) {
  await guard(button, async () => {
    state.extraction = await call(`/v1/extractions/${state.extraction.id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ expected_version: state.extraction.based_on_version }),
    });
    state.planning = null;
    state.selected = null;
    state.approval = null;
    state.execution = null;
    await refresh();
  });
}

function listen() {
  // Notifications say what changed; state still comes from the versioned endpoints.
  const source = new EventSource("/v1/stream", { withCredentials: true });
  let pending = null;
  source.onmessage = null;
  for (const name of [
    "state.changed",
    "incident.created",
    "incident.updated",
    "incident.resolved",
    "recovery.plan.created",
    "approval.required",
    "approval.decided",
    "action.completed",
    "preference.changed",
  ]) {
    source.addEventListener(name, () => {
      // Coalesce a burst of notifications into one read.
      clearTimeout(pending);
      pending = setTimeout(() => refresh().catch(() => {}), 150);
    });
  }
}

el("inject").addEventListener("click", (event) => inject(event.currentTarget));
el("read-message").addEventListener("click", (event) => readMessage(event.currentTarget));
el("live-inference").addEventListener("change", (event) => {
  const input = event.currentTarget;
  call("/v1/privacy", {
    method: "PATCH",
    body: JSON.stringify({ live_inference: input.checked }),
  }).catch((error) => {
    input.checked = !input.checked;
    toast(error.message);
  });
});
el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const password = el("login-password").value;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  el("login-error").hidden = true;
  try {
    const result = await call("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    form.reset();
    hideLogin(result.role);
    await renderReasoningStatus();
    await refresh();
    listen();
  } catch (error) {
    const message = el("login-error");
    message.textContent = error.message;
    message.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
el("logout").addEventListener("click", async () => {
  await call("/v1/auth/logout", { method: "POST" }).catch(() => {});
  state.authRole = null;
  showLogin();
});
authenticate()
  .then((ok) => (ok ? Promise.all([renderReasoningStatus(), refresh()]).then(listen) : null))
  .catch((error) => toast(error.message));
