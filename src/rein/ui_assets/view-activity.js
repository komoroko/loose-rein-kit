// Activity: the live event feed (watching a headless build) and the operations console — kept away
// from the review flow, because destructive actions live on this tab and approval lives on Review.
//
// There is no speculative-work / roll-back log section here any more. Those logs live in the phase
// deliverables under docs/, never in the status payload, so the pane that read `status.logs` could
// only ever hide itself.

import { READ_ONLY, esc, pollDelay, post } from "/assets/api.js";

// ---- event feed: polled only while this tab is visible ----
const ESCALATION_KINDS = new Set(["blocked", "merge_conflict", "integration_red", "no_runnable", "gate_violation"]);
const OK_KINDS = new Set(["gate_approved", "task_done", "resolve", "security_review"]);
let tabVisible = false;
let lastEvents = "";

// `needs_decision` is the server's word for "this event is still waiting on a human" — it is
// computed in ui.py from events.ATTENTION_EVENTS, so the feed and the Overview agree by construction.
function eventClass(e) {
  if (ESCALATION_KINDS.has(e.event)) return e.needs_decision ? "ev-bad" : "ev-closed";
  if (OK_KINDS.has(e.event)) return "ev-ok";
  return "";
}

// `detail` is a JSON object in the audit record, not a string — interpolating it directly rendered
// every row as "[object Object]". Flattened to `key=value` pairs, truncated so one fat payload
// cannot push the rest of the table off the screen.
function detailText(detail) {
  if (detail === null || detail === undefined) return "-";
  if (typeof detail !== "object") return String(detail);
  const parts = Object.keys(detail).map(k => {
    const v = detail[k];
    const s = (v !== null && typeof v === "object") ? JSON.stringify(v) : String(v);
    return k + "=" + (s.length > 60 ? s.slice(0, 57) + "…" : s);
  });
  return parts.length ? parts.join("  ") : "-";
}

async function fetchEvents() {
  const el = document.getElementById("events");
  try {
    const res = await fetch("/api/events?limit=50");
    const text = await res.text();
    if (text === lastEvents) return;  // unchanged tail: keep the DOM (and any hover) alive
    lastEvents = text;
    const d = JSON.parse(text);
    if (d.error) { el.innerHTML = '<div class="warn">' + esc(d.error) + "</div>"; return; }
    if (!d.events.length) { el.innerHTML = '<div class="empty">No events yet (created on first event).</div>'; return; }
    el.innerHTML = '<div class="scroll"><table class="events">' +
      "<tr><th>#</th><th>Date</th><th>Event</th><th>Actor</th><th>Subjects</th><th>Detail</th></tr>" +
      d.events.map(e => '<tr class="' + eventClass(e) + '"><td>' + esc(e.seq) + "</td><td>" + esc(e.date) +
        '</td><td class="mono">' + esc(e.event) + (e.needs_decision ? " ◆" : "") + "</td><td>" +
        esc(e.actor || "-") + '</td><td class="mono">' + esc((e.subject_ids || []).join(", ") || "-") +
        "</td><td>" + esc(detailText(e.detail)) + "</td></tr>").join("") + "</table></div>" +
      '<div class="empty" style="margin-top:.3rem">showing latest ' + d.events.length + " of " + d.total + "</div>";
  } catch (err) { el.innerHTML = '<div class="empty">event feed unavailable</div>'; }
}

document.addEventListener("rein:view", e => {
  tabVisible = e.detail === "activity";
  if (tabVisible) fetchEvents();
});
document.addEventListener("rein:refresh", () => { lastEvents = ""; if (tabVisible) fetchEvents(); });
// Same visibility-aware pacing as the status poll (api.js pollDelay), so a backgrounded dashboard
// does not keep two 3-second loops running against a repo nobody is looking at.
(function pollEvents() {
  setTimeout(() => { if (tabVisible) fetchEvents(); pollEvents(); }, pollDelay());
})();

function runDoctor() { post("/api/run", { action:"doctor", params:{} }); }
function runTests() { post("/api/run", { action:"tests", params:{} }); }
function runRevise() {
  const phase = document.getElementById("revPhase").value;
  const reason = document.getElementById("revReason").value.trim();
  if (!reason) { alert("revise needs a reason"); return; }
  if (confirm("Roll back to '" + phase + "'? Gates from there onward reset to pending."))
    post("/api/run", { action:"revise", params:{ phase:phase, reason:reason } });
}
function runCycleClose() {
  const slug = document.getElementById("closeSlug").value.trim();
  if (!slug) { alert("enter a cycle slug (e.g. payment-refactor)"); return; }
  if (confirm("Close the cycle as '" + slug + "'? Deliverables are archived and gates reset."))
    post("/api/run", { action:"cycle_close", params:{ slug:slug } });
}

// Static markup with no data behind it — app.js draws it once at load. It used to be rebuilt on
// every poll, which silently emptied a revise reason or cycle slug the moment the field lost focus.
export function renderOps() {
  if (READ_ONLY) {
    document.getElementById("ops").innerHTML =
      '<div class="empty">Running with --read-only; actions are disabled.</div>';
    return;
  }
  const phases = ["requirements", "design", "tasks", "build"].map(p => "<option>" + p + "</option>").join("");
  document.getElementById("ops").innerHTML =
    '<div class="ops"><button onclick="runDoctor()">rein doctor</button>' +
    '<button onclick="runTests()">make test</button></div>' +
    '<div class="ops" style="margin-top:.6rem">' +
    '<select id="revPhase">' + phases + "</select>" +
    '<input id="revReason" placeholder="revise reason" size="28">' +
    '<button class="danger" onclick="runRevise()">rein revise</button>' +
    '<input id="closeSlug" placeholder="cycle slug" size="16">' +
    '<button class="danger" onclick="runCycleClose()">rein cycle-close</button></div>';
}

// Named by generated onclick= handlers (module scope is not global scope).
window.runDoctor = runDoctor;
window.runTests = runTests;
window.runRevise = runRevise;
window.runCycleClose = runCycleClose;
