// Tasks: the dependency DAG, the frontier order, task detail, and the traceability table.

import { chip, esc, onTaskClick, taskAttr, taskById } from "/assets/api.js";

// ---- dependency graph as inline SVG (no external namespace literal → stays offline-safe) ----

// One arrowhead per edge colour. A marker does not inherit the path's stroke, so the two are
// separate defs filled from app.css; `orient="auto"` turns them along the curve's end tangent.
const ARROW_DEFS =
  '<defs>' +
  ['dagarw', 'dagarwc'].map(id =>
    '<marker id="' + id + '" viewBox="0 0 6 6" refX="6" refY="3" markerWidth="6" markerHeight="6" ' +
    'orient="auto"><path d="M0 0 L6 3 L0 6 z"/></marker>').join("") +
  '</defs>';

function buildDag(t, byId) {
  const crit = new Set(t.critical_path);
  const pos = {};
  t.layers.forEach((ids, c) => ids.forEach((id, r) => { pos[id] = { c:c, r:r }; }));
  const colW = 170, rowH = 52, nodeW = 132, nodeH = 34, padX = 12, padY = 12;
  const cols = t.layers.length || 1;
  const rows = Math.max(1, ...t.layers.map(l => l.length));
  const W = cols * colW + padX * 2, H = rows * rowH + padY * 2;
  const X = id => padX + pos[id].c * colW, Y = id => padY + pos[id].r * rowH;
  let edges = "", nodes = "";
  t.rows.forEach(tk => (tk.blocked_by || []).forEach(dep => {
    if (!pos[dep] || !pos[tk.id]) return;
    // The edge stops 3px short of the node so the arrowhead points *at* the border instead of
    // being buried under it. Direction is the whole point of the head: dep → dependent.
    const x1 = X(dep) + nodeW, y1 = Y(dep) + nodeH / 2, x2 = X(tk.id) - 3, y2 = Y(tk.id) + nodeH / 2;
    const cx = (x1 + x2) / 2;
    const c = (crit.has(dep) && crit.has(tk.id)) ? " crit" : "";
    edges += '<path class="edge' + c + '" marker-end="url(#dagarw' + (c ? "c" : "") + ')" d="M' +
      x1 + " " + y1 + " C" + cx + " " + y1 + " " + cx + " " + y2 + " " + x2 + " " + y2 + '"/>';
  }));
  t.layers.forEach(ids => ids.forEach(id => {
    const tk = byId[id] || { status:"todo" };
    const c = crit.has(id) ? " crit" : "", x = X(id), y = Y(id);
    nodes += "<g" + taskAttr(id) + ">" +
      '<rect class="nd ' + esc(tk.status) + c + '" x="' + x + '" y="' + y + '" width="' + nodeW +
      '" height="' + nodeH + '" rx="6"/>' +
      '<text x="' + (x + 8) + '" y="' + (y + 21) + '">' + esc(id) + "</text></g>";
  }));
  return '<div class="scroll"><svg class="dag" viewBox="0 0 ' + W + " " + H + '" width="' + W +
    '" height="' + H + '">' + ARROW_DEFS + edges + nodes + "</svg></div>";
}

// What the graph's geometry means. The status colours are already keyed by the pills above it, so
// the legend covers only what nothing else states: the axis, the arrows, and the teal.
function graphLegend() {
  const arrow = c =>
    '<svg width="26" height="8" viewBox="0 0 26 8" aria-hidden="true">' +
    '<path class="lgline' + c + '" d="M0 4 H19"/><path class="lghead' + c + '" d="M19 1 L25 4 L19 7 z"/></svg>';
  return '<div class="legend">' +
    '<span class="li">columns = execution layers (L0 → L1 → …)</span>' +
    '<span class="li">' + arrow("") + "arrow = blocked_by: the tail must finish first</span>" +
    '<span class="li">' + arrow(" crit") + "critical path</span>" +
    "</div>";
}

export function showTaskDetail(id) {
  const t = taskById(id), el = document.getElementById("taskDetail");
  if (!t || !el) return;
  // Field names are `_tasks_block`'s rows verbatim: a task answers for `claim_ids`. The older
  // `req`/`test` names never exist in the payload and would always print "—".
  const list = ids => (ids && ids.length ? esc(ids.join(", ")) : "—");
  // Why a task is on its second attempt. A status alone reads the same whether the build reached
  // it once or four times; the rest of the record (failure summary, retry budget) is state.yaml's.
  const h = t.handoff || {};
  const salvage = { pending: "work-in-progress preserved", restored: "previous work restored",
                    conflict: "previous work conflicts — left on its branch" };
  const parts = [h.failed_step ? "last failed at " + esc(h.failed_step) : "",
                 salvage[h.salvage_state] || ""].filter(Boolean);
  const handoff = parts.length ? "<dt>carried over</dt><div>" + parts.join(" · ") + "</div>" : "";
  el.innerHTML = '<div class="detail"><b class="mono">' + esc(t.id) + "</b> — " + esc(t.title) +
    "<dt>status / kind / risk</dt><div>" + esc(t.status) + " / " + esc(t.kind) + " / " + esc(t.risk) + "</div>" +
    handoff +
    '<dt>blockedBy</dt><div class="mono">' + list(t.blocked_by) +
    '</div><dt>claims</dt><div class="mono">' + list(t.claim_ids) + "</div></div>";
}

// Per-layer progress: one row per execution layer so a running build reads at a glance
// (derived entirely from layers + statuses already in the status payload — no extra API).
function layersBar(t, byId) {
  if (!t.layers.length) return "";
  return '<div class="layers">' + t.layers.map((ids, i) => {
    const st = id => (byId[id] || { status: "todo" }).status;
    const done = ids.filter(id => st(id) === "done").length;
    const running = ids.filter(id => st(id) === "in-progress").length;
    const segs = ids.map(id =>
      '<span class="seg ' + esc(st(id)) + ' clk" title="' + esc(id) + " (" + esc(st(id)) +
      ')"' + taskAttr(id) + "></span>").join("");
    return '<div class="lrow"><span class="lname">L' + i + '</span><span class="lbar">' + segs +
      '</span><span class="lcount">' + done + "/" + ids.length +
      (running ? " · " + running + " running" : "") + "</span></div>";
  }).join("") + "</div>";
}

export function renderTasks(d) {
  const el = document.getElementById("tasks");
  const t = d.tasks;
  if (!t) { el.innerHTML = '<div class="empty">No tasks.yaml yet (created by /tasks).</div>'; return; }
  const byId = {}; t.rows.forEach(x => { byId[x.id] = x; });
  // Status spellings are models.TASK_STATUS_ORDER verbatim — hyphenated, not underscored. An
  // underscored copy here indexed `counts` with a key it does not have, so every running task
  // rendered as a zero and the layer bar never showed a build moving.
  const order = ["todo", "in-progress", "blocked", "needs-revision", "done"];
  const pills = '<div class="pills">' + order.map(s => '<span class="chip ' + s + '">' + esc(s) + " " +
    (t.counts[s] || 0) + "</span>").join("") + '<span class="pill">total ' + t.total + "</span></div>";
  const graph = t.rows.length
    ? layersBar(t, byId) + graphLegend() + buildDag(t, byId)
    : '<div class="empty">(no tasks)</div>';
  const frontier = t.frontier.length
    ? '<div class="scroll"><table><tr><th>ID</th><th>Title</th><th>Kind</th><th>fan-out</th></tr>' +
      t.frontier.map(f => '<tr class="clk"' + taskAttr(f.id) + '><td class="mono">' +
        esc(f.id) + "</td><td>" + esc(f.title) + "</td><td>" + esc(f.kind) + "</td><td>" + f.fan_out +
        "</td></tr>").join("") + "</table></div>"
    : '<div class="empty">(no startable todo)</div>';
  el.innerHTML = pills + graph +
    '<div style="margin-top:.6rem;font-size:.72rem;color:var(--muted);font-weight:700">' +
    "FRONTIER (optimal order)</div>" + frontier;  // #taskDetail lives outside #tasks (see index.html)
}

// The requirement → claim → task thread, drawn from the same TraceReport `rein dag --trace`
// exits on. There is deliberately no "design" column: nothing in the trace report knows whether a
// requirement reached the design document, and a column that could only ever print "—" was reading
// as an answer rather than as an absence.
export function renderTrace(d) {
  const sec = document.getElementById("traceSection"), tr = d.trace;
  if (!tr) { sec.style.display = "none"; return; }
  sec.style.display = "";
  const rows = (tr.requirements || []).map(r => {
    const claims = r.claims.length
      ? r.claims.map(id => '<span class="mono">' + esc(id) + "</span>").join(", ")
      : '<span class="empty">(no claim)</span>';
    const tasks = r.tasks.length
      ? r.tasks.map(id => chip(id, (taskById(id) || {}).status || "todo", false, true)).join(" ")
      : '<span class="empty">(no task)</span>';
    return '<tr><td class="mono">' + esc(r.id) + (r.nfr ? ' <span class="empty">NFR</span>' : "") +
      "</td><td>" + claims + "</td><td>" + tasks + "</td></tr>";
  }).join("");
  // Errors block a gate, warnings do not — so they are not drawn with the same weight.
  const findings = (tr.errors || []).map(f => '<div class="bad">' + esc(f) + "</div>").join("") +
    (tr.warnings || []).map(f => '<div class="warn">' + esc(f) + "</div>").join("");
  document.getElementById("trace").innerHTML =
    '<div class="scroll"><table><tr><th>Requirement</th><th>claims</th><th>tasks</th></tr>' +
    rows + "</table></div>" +
    (findings || '<div class="okline">✓ every requirement threads to a claim and a task.</div>');
}

onTaskClick(showTaskDetail);  // one delegated listener for every [data-task] the views emit
