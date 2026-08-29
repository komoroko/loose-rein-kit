// The spine (every screen) and the Now view: the next recommended command, and what stands
// between this repository and its next gate.

import { awaitingGate, chip, circled, esc, route } from "/assets/api.js";

// The lifecycle rail, and the page's only rendering of "which gate waits on you". Three states and
// no fourth: opened by a recorded human approval, waiting on you, not yet reached. The waiting one
// is the single inverted block on the page — nothing else on any screen is painted that way.
export function renderStepper(d) {
  const awaiting = (awaitingGate(d) || {}).name;
  const here = route().gate;
  document.getElementById("stepper").innerHTML = (d.gates || []).map(g => {
    const cls = [
      g.status === "approved" ? "approved" : (g.name === awaiting ? "awaiting" : "future"),
      g.phase === d.current_phase ? "live" : "",
      g.name === here ? "active" : "",
    ].filter(Boolean).join(" ");
    // Every approval is a human's typed confirmation, and the receipt id is the proof of it —
    // so an opened gate says which approval opened it rather than only that it is open.
    const title = g.status === "approved"
      ? "approved (" + (g.approval_id || "receipt unreadable") + ")"
      : (g.name === awaiting ? "waiting on you — read it, then decide" : "not reached yet");
    const mark = g.status === "approved" ? "✓" : (g.name === awaiting ? "◆" : "·");
    return '<a class="station ' + cls + '" href="#gate/' + esc(g.name) + '" title="' + esc(title) +
      '"' + (g.name === here ? ' aria-current="page"' : "") +
      '><span class="mark">' + mark + '</span><span class="gname">' + esc(g.name) +
      ' <span class="gidx">' + circled(g.index) + "</span></span></a>";
  }).join("");
}

export function renderNext(d) {
  const n = d.next || {};
  const awaiting = awaitingGate(d);
  const also = (n.also || []).map(a => '<span class="chip">' + esc(a) + "</span>").join(" ");
  // A command the human runs is not a link to the gate; a decision at a gate is.
  const read = (n.kind === "run_phase" || n.kind === "close" || !awaiting)
    ? ""
    : ' <a class="chip clk" href="#gate/' + esc(awaiting.name) + '">read gate ' +
      circled(awaiting.index) + " →</a>";
  document.getElementById("next").innerHTML =
    '<div class="console"><span class="prompt">▸</span><code class="cmd">' + esc(n.command) + "</code>" +
    '<button onclick="copyCmd(' + JSON.stringify(n.command || "").replace(/"/g, "&quot;") +
    ', this)">copy</button></div>' +
    '<p class="lede">' + esc(n.reason) + "</p>" +
    (also || read ? '<div class="row">' + (also ? "also: " + also : "") + read + "</div>" : "");
}

// Everything standing between this repository and its next gate arrives pre-derived as `pending`
// (status_api.pending_queue): gate blockers straight out of approve.readiness, open escalations,
// stuck tasks, ungrounded claims — one list, already sorted worst-first, each row carrying the
// command that addresses it. This pane deliberately re-derives none of it from `attention` and
// `tasks.rows`: two places deciding what "needs attention" means is exactly how the Board and this
// screen drift apart.
//
// `pending_deep: false` means gate readiness was not probed on this poll. It is said out loud,
// because a queue that silently omits its blocking rows reads like a repository with none.
export function renderAttention(d) {
  const el = document.getElementById("attention");
  const awaiting = awaitingGate(d);
  const named = awaiting ? "gate " + circled(awaiting.index) + " " + awaiting.name : "the next gate";
  document.getElementById("attentionHead").textContent = "In the way of " + named;

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
  } else if (awaiting) {
    html += '<p class="note">Nothing is in the way of ' + esc(named) +
      '. <a href="#gate/' + esc(awaiting.name) + '">Read it</a>, then decide.</p>';
  } else {
    html += '<p class="note">Every gate is open. Nothing is waiting on you.</p>';
  }
  el.innerHTML = html;
}
