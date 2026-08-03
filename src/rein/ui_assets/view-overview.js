// Overview: the lifecycle rail, the next recommended command, and what needs attention.

import { awaitingGate, chip, esc } from "/assets/api.js";

export function renderStepper(d) {
  const gatesByPhase = {};
  d.gates.forEach(g => { gatesByPhase[g.phase] = g; });
  const awaiting = (awaitingGate(d) || {}).name;
  const idx = d.phase_order.indexOf(d.current_phase);
  const rail = d.phase_order.map((p, i) => {
    const g = gatesByPhase[p];
    let gate = "";
    if (g) {
      const cls = g.status === "approved" ? "approved" : (g.name === awaiting ? "await" : "");
      // Every approval is a human's typed confirmation at a terminal — there is no other
      // authority a gate can be opened by, so one tick covers every approved gate.
      const mark = g.status === "approved" ? "✓" : (g.name === awaiting ? "◆" : "○");
      // The awaiting gate is a link into the review pane — "read, then approve" starts here.
      const inner = mark + " g" + g.index;
      gate = g.name === awaiting
        ? '<a class="rgate await" href="#review" title="Open the gate review">' + inner + "</a>"
        : '<span class="rgate ' + cls + '">' + inner + "</span>";
    }
    const cls = i === idx ? "live" : (idx >= 0 && i < idx ? "past" : "future");
    return '<div class="rphase ' + cls + '"><span class="rnode"></span><span class="rname">' +
      esc(p) + "</span>" + gate + "</div>";
  }).join("");
  document.getElementById("stepper").innerHTML =
    rail + '<span class="rloop" title="delta cycle → rein cycle-close">↻</span>';
}

export function renderNext(d) {
  const n = d.next || {};
  const also = (n.also || []).map(a => '<span class="chip">' + esc(a) + "</span>").join(" ");
  const review = n.kind === "run_phase" || n.kind === "close"
    ? "" : ' <a class="chip" href="#review">open review →</a>';
  document.getElementById("next").innerHTML =
    '<div class="console"><span class="prompt">▸</span><code class="cmd">' + esc(n.command) + "</code>" +
    '<button onclick="copyCmd(' + JSON.stringify(n.command || "").replace(/"/g, "&quot;") +
    ', this)">copy</button></div>' +
    '<div class="reason">' + esc(n.reason) + "</div>" +
    (also || review ? '<div style="margin-top:.4rem">' + (also ? "also: " + also : "") + review + "</div>" : "");
}

// Everything standing between this repository and its next gate arrives pre-derived as `pending`
// (status_api.pending_queue): gate blockers straight out of approve.readiness, open escalations,
// stuck tasks, ungrounded claims — one list, already sorted worst-first, each row carrying the
// command that addresses it. This pane deliberately re-derives none of it from `attention` and
// `tasks.rows`: two places deciding what "needs attention" means is exactly how the Tasks tab and
// this one drift apart.
//
// `pending_deep: false` means gate readiness was not probed on this poll. It is said out loud,
// because a queue that silently omits its blocking rows reads like a repository with none.
export function renderAttention(d) {
  const el = document.getElementById("attention");
  let html = "";
  (d.warnings || []).forEach(w => { html += '<div class="warn">' + esc(w) + "</div>"; });
  const pending = d.pending || [];
  if (pending.length) {
    const blocking = pending.filter(p => p.severity === "blocking").length;
    html += '<div class="subhead">' + pending.length + " waiting on you" +
      (blocking ? " · " + blocking + " blocking" : "") +
      (d.pending_deep === false ? " · gate readiness not probed" : "") + "</div>";
    html += '<div class="scroll"><table><tr><th></th><th>Subject</th><th>What is in the way</th>' +
      "<th>Next</th></tr>" +
      pending.map(p => "<tr><td>" + chip(p.severity, p.severity, p.severity === "blocking", false) +
        '</td><td class="mono">' + esc(p.subject) + "</td><td>" + esc(p.headline) +
        '</td><td class="mono">' + esc(p.action || "-") + "</td></tr>").join("") + "</table></div>";
  }
  el.innerHTML = html || '<div class="empty">Nothing needs attention.</div>';
}
