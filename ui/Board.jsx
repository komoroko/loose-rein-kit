// The Board: the dependency DAG, the frontier order, task detail, and the traceability table.

import { useState } from "react";

import { Chip, Empty, Scroll, Table, Warn } from "./parts.jsx";

// ---- dependency graph as inline SVG (no external namespace literal → stays offline-safe) ----

// One arrowhead per edge colour. A marker does not inherit the path's stroke, so the two are
// separate defs filled from app.css; `orient="auto"` turns them along the curve's end tangent.
function ArrowDefs() {
  return (
    <defs>
      {["dagarw", "dagarwc"].map((id) => (
        <marker
          key={id}
          id={id}
          viewBox="0 0 6 6"
          refX="6"
          refY="3"
          markerWidth="6"
          markerHeight="6"
          orient="auto"
        >
          <path d="M0 0 L6 3 L0 6 z" />
        </marker>
      ))}
    </defs>
  );
}

const COL_W = 170, ROW_H = 52, NODE_W = 132, NODE_H = 34, PAD_X = 12, PAD_Y = 12;

function Dag({ tasks, byId, onPick }) {
  const crit = new Set(tasks.critical_path);
  const pos = {};
  tasks.layers.forEach((ids, c) => ids.forEach((id, r) => { pos[id] = { c, r }; }));
  const cols = tasks.layers.length || 1;
  const rows = Math.max(1, ...tasks.layers.map((l) => l.length));
  const width = cols * COL_W + PAD_X * 2;
  const height = rows * ROW_H + PAD_Y * 2;
  const X = (id) => PAD_X + pos[id].c * COL_W;
  const Y = (id) => PAD_Y + pos[id].r * ROW_H;

  const edges = [];
  tasks.rows.forEach((tk) =>
    (tk.blocked_by || []).forEach((dep) => {
      if (!pos[dep] || !pos[tk.id]) return;
      // The edge stops 3px short of the node so the arrowhead points *at* the border instead of
      // being buried under it. Direction is the whole point of the head: dep → dependent.
      const x1 = X(dep) + NODE_W, y1 = Y(dep) + NODE_H / 2;
      const x2 = X(tk.id) - 3, y2 = Y(tk.id) + NODE_H / 2;
      const cx = (x1 + x2) / 2;
      const isCrit = crit.has(dep) && crit.has(tk.id);
      edges.push(
        <path
          key={dep + "->" + tk.id}
          className={"edge" + (isCrit ? " crit" : "")}
          markerEnd={`url(#dagarw${isCrit ? "c" : ""})`}
          d={`M${x1} ${y1} C${cx} ${y1} ${cx} ${y2} ${x2} ${y2}`}
        />
      );
    })
  );

  return (
    <Scroll>
      <svg className="dag" viewBox={`0 0 ${width} ${height}`} width={width} height={height}>
        <ArrowDefs />
        {edges}
        {tasks.layers.flat().map((id) => {
          const tk = byId[id] || { status: "todo" };
          const isCrit = crit.has(id);
          return (
            <g key={id} onClick={() => onPick(id)}>
              <rect
                className={"nd " + tk.status + (isCrit ? " crit" : "")}
                x={X(id)}
                y={Y(id)}
                width={NODE_W}
                height={NODE_H}
                rx="6"
              />
              <text x={X(id) + 8} y={Y(id) + 21}>{id}</text>
            </g>
          );
        })}
      </svg>
    </Scroll>
  );
}

// What the graph's geometry means. The status colours are already keyed by the pills above it, so
// the legend covers only what nothing else states: the axis, the arrows, and the teal.
function Legend() {
  const arrow = (cls) => (
    <svg width="26" height="8" viewBox="0 0 26 8" aria-hidden="true">
      <path className={"lgline" + cls} d="M0 4 H19" />
      <path className={"lghead" + cls} d="M19 1 L25 4 L19 7 z" />
    </svg>
  );
  return (
    <div className="legend">
      <span className="li">columns = execution layers (L0 → L1 → …)</span>
      <span className="li">{arrow("")}arrow = blocked_by: the tail must finish first</span>
      <span className="li">{arrow(" crit")}critical path</span>
    </div>
  );
}

// Per-layer progress: one row per execution layer so a running build reads at a glance (derived
// entirely from layers + statuses already in the status payload — no extra API).
function Layers({ tasks, byId, onPick }) {
  if (!tasks.layers.length) return null;
  const st = (id) => (byId[id] || { status: "todo" }).status;
  return (
    <div className="layers">
      {tasks.layers.map((ids, i) => {
        const done = ids.filter((id) => st(id) === "done").length;
        const running = ids.filter((id) => st(id) === "in-progress").length;
        return (
          <div className="lrow" key={i}>
            <span className="lname">L{i}</span>
            <span className="lbar">
              {ids.map((id) => (
                <span
                  key={id}
                  className={"seg " + st(id) + " clk"}
                  title={`${id} (${st(id)})`}
                  onClick={() => onPick(id)}
                ></span>
              ))}
            </span>
            <span className="lcount">
              {done}/{ids.length}
              {running ? ` · ${running} running` : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

const SALVAGE = {
  pending: "work-in-progress preserved",
  restored: "previous work restored",
  conflict: "previous work conflicts — left on its branch",
};

// Field names are `_tasks_block`'s rows verbatim: a task answers for `claim_ids`. The older
// `req`/`test` names never exist in the payload and would always print "—".
function TaskDetail({ task }) {
  if (!task) return null;
  const list = (ids) => (ids && ids.length ? ids.join(", ") : "—");
  // Why a task is on its second attempt. A status alone reads the same whether the build reached it
  // once or four times; the rest of the record (failure summary, retry budget) is state.yaml's.
  const h = task.handoff || {};
  const carried = [h.failed_step ? "last failed at " + h.failed_step : "", SALVAGE[h.salvage_state] || ""]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="detail">
      <b className="mono">{task.id}</b> — {task.title}
      <dt>status / kind / risk</dt>
      <div>{task.status} / {task.kind} / {task.risk}</div>
      {/* The commit that landed the task, so "done" is something you can go and read. */}
      {task.commit ? (
        <>
          <dt>landed in</dt>
          <div className="mono">{String(task.commit).slice(0, 12)}</div>
        </>
      ) : null}
      {carried ? (
        <>
          <dt>carried over</dt>
          <div>{carried}</div>
        </>
      ) : null}
      <dt>blockedBy</dt>
      <div className="mono">{list(task.blocked_by)}</div>
      <dt>claims</dt>
      <div className="mono">{list(task.claim_ids)}</div>
    </div>
  );
}

// The requirement → claim → task thread, drawn from the same TraceReport `rein dag --trace` exits
// on. There is deliberately no "design" column: nothing in the trace report knows whether a
// requirement reached the design document, and a column that could only ever print "—" was reading
// as an answer rather than as an absence.
function Trace({ trace, byId, onPick }) {
  if (!trace) return null;
  const findings = [
    ...(trace.errors || []).map((f) => <div className="bad" key={"e" + f}>{f}</div>),
    ...(trace.warnings || []).map((f) => <Warn key={"w" + f}>{f}</Warn>),
  ];
  return (
    <div className="block" id="traceSection">
      <h2>Requirement → claim → task</h2>
      <div id="trace">
        <Table head={["Requirement", "claims", "tasks"]}>
          {(trace.requirements || []).map((r) => (
            <tr key={r.id}>
              <td className="mono">
                {r.id}
                {r.nfr ? <span className="empty"> NFR</span> : null}
              </td>
              <td>
                {r.claims.length
                  ? r.claims.map((id, i) => (
                    <span key={id}>
                      {i ? ", " : ""}
                      <span className="mono">{id}</span>
                    </span>
                  ))
                  : <span className="empty">(no claim)</span>}
              </td>
              <td>
                {r.tasks.length
                  ? r.tasks.map((id) => (
                    <Chip key={id} id={id} status={(byId[id] || {}).status || "todo"} onClick={() => onPick(id)} />
                  ))
                  : <span className="empty">(no task)</span>}
              </td>
            </tr>
          ))}
        </Table>
        {findings.length ? findings : <div className="okline">✓ every requirement threads to a claim and a task.</div>}
      </div>
    </div>
  );
}

export default function Board({ status }) {
  const [picked, setPicked] = useState(null);
  if (!status || status.error) {
    return (
      <div className="view" id="view-board">
        <div className="block">
          <Empty>{status ? status.error : "waiting for status…"}</Empty>
        </div>
      </div>
    );
  }

  const tasks = status.tasks;
  const byId = {};
  if (tasks) tasks.rows.forEach((t) => { byId[t.id] = t; });

  return (
    <div className="view" id="view-board">
      <div className="block">
        <h2>Tasks</h2>
        <div id="tasks">
          {!tasks ? (
            <Empty>No tasks.yaml yet (created by /tasks).</Empty>
          ) : (
            <>
              {/* The vocabulary is the server's: `counts` is keyed by models.TASK_STATUS_ORDER and
                  arrives in that order. A hand-kept copy of the list here drifted twice — once
                  spelling a running task with an underscore (Mermaid's spelling, which matches
                  nothing in the DOM) and once omitting `awaiting-evidence` entirely, so a task
                  parked waiting for a person to record what they saw was counted in `total` and
                  shown in no pill. Reading the keys back cannot drift again. */}
              <div className="pills">
                {Object.keys(tasks.counts).map((s) => (
                  <span className={"chip " + s} key={s}>
                    {s} {tasks.counts[s] || 0}
                  </span>
                ))}
                <span className="pill">total {tasks.total}</span>
              </div>
              {tasks.rows.length ? (
                <>
                  <Layers tasks={tasks} byId={byId} onPick={setPicked} />
                  <Legend />
                  <Dag tasks={tasks} byId={byId} onPick={setPicked} />
                </>
              ) : (
                <Empty>(no tasks)</Empty>
              )}
              <div className="subhead" style={{ marginTop: "1rem" }}>
                Frontier — what can start now, best first
              </div>
              {tasks.frontier.length ? (
                <Table head={["ID", "Title", "Kind", "fan-out"]}>
                  {tasks.frontier.map((f) => (
                    <tr className="clk" key={f.id} onClick={() => setPicked(f.id)}>
                      <td className="mono">{f.id}</td>
                      <td>{f.title}</td>
                      <td>{f.kind}</td>
                      <td>{f.fan_out}</td>
                    </tr>
                  ))}
                </Table>
              ) : (
                <Empty>(no startable todo)</Empty>
              )}
            </>
          )}
        </div>
        <div id="taskDetail">
          <TaskDetail task={byId[picked]} />
        </div>
      </div>
      <Trace trace={status.trace} byId={byId} onPick={setPicked} />
    </div>
  );
}
