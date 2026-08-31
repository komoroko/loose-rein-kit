// Record (the hash-chained event log, for watching a headless build) and Console (the fixed
// whitelist of safe operations) — two screens, because watching a build and sending work backwards
// are different jobs, and neither belongs next to the approval footer.
//
// There is no speculative-work / roll-back log section here. Those logs live in the phase
// deliverables under docs/, never in the status payload, so the pane that read `status.logs` could
// only ever hide itself.

import { READ_ONLY, esc, runCommand, toast } from "/assets/api.js";

// ---- the record ----
const ESCALATION_KINDS = new Set(["blocked", "merge_conflict", "integration_red", "no_runnable", "gate_violation"]);
const OK_KINDS = new Set(["gate_approved", "task_done", "resolve", "security_review"]);
let recordVisible = false;
let stale = true;  // the log moved while nobody was looking; fetch when someone is

// `needs_decision` is the server's word for "this event is still waiting on a human" — it is
// computed in ui.py from events.ATTENTION_EVENTS, so the feed and the Now screen agree by
// construction.
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
  stale = false;
  try {
    const res = await fetch("/api/events?limit=50");
    const d = await res.json();
    if (d.error) { el.innerHTML = '<div class="warn">' + esc(d.error) + "</div>"; return; }
    if (!d.events.length) { el.innerHTML = '<div class="empty">No events yet (written on the first one).</div>'; return; }
    el.innerHTML = '<div class="scroll"><table class="events">' +
      "<tr><th>#</th><th>Date</th><th>Event</th><th>Actor</th><th>Subjects</th><th>Detail</th></tr>" +
      d.events.map(e => '<tr class="' + eventClass(e) + '"><td>' + esc(e.seq) + "</td><td>" + esc(e.date) +
        '</td><td class="mono">' + esc(e.event) + (e.needs_decision ? " ◆" : "") + "</td><td>" +
        esc(e.actor || "-") + '</td><td class="mono">' + esc((e.subject_ids || []).join(", ") || "-") +
        "</td><td>" + esc(detailText(e.detail)) + "</td></tr>").join("") + "</table></div>" +
      '<div class="empty" style="margin-top:.4rem">latest ' + d.events.length + " of " + d.total + "</div>";
  } catch { el.innerHTML = '<div class="empty">event feed unavailable</div>'; }
}

// The feed refetches when the log actually changed — the server says so — and only for someone
// who is looking. There is no timer and no response-body comparison: both existed to make blind
// polling survivable, and there is no longer any blind polling.
document.addEventListener("rein:view", e => {
  recordVisible = e.detail.view === "record";
  if (recordVisible && stale) fetchEvents();
});
document.addEventListener("rein:record", () => {
  stale = true;
  if (recordVisible) fetchEvents();
});

// ---- console ----
// An OS confirm() dialog puts the consequence in a box the page cannot style, cannot keep on
// screen, and cannot be read back afterwards. These two commands move work backwards, so what they
// will do is written on the page, in the page's own voice, above the button that does it.
function askConfirm(question, consequence, run) {
  const el = document.getElementById("opsConfirm");
  el.hidden = false;
  el.innerHTML = '<p class="lede">' + question + "</p><p class=\"note\">" + consequence + "</p>" +
    '<div class="row"><button class="danger" data-confirm="go">Yes, do it</button>' +
    '<button data-confirm="no">Cancel</button></div>';
  el.querySelector('[data-confirm="no"]').onclick = clearConfirm;
  const go = el.querySelector('[data-confirm="go"]');
  go.onclick = () => { clearConfirm(); run(); };
  go.focus();
}
function clearConfirm() {
  const el = document.getElementById("opsConfirm");
  el.hidden = true;
  el.innerHTML = "";
}

function runDoctor() { runCommand("doctor", {}); }
function runTests() { runCommand("tests", {}); }

function runRevise() {
  const phase = document.getElementById("revPhase").value;
  const reason = document.getElementById("revReason").value.trim();
  if (!reason) { toast("say why you are rolling back", "err"); document.getElementById("revReason").focus(); return; }
  askConfirm(
    "Roll back to " + esc(phase) + "?",
    "Gates reset in a chain starting at " + esc(phase) + ": each one goes back to pending, and the " +
    "receipts and reviews built on top of them stop counting. Reason on the record: " + esc(reason),
    () => runCommand("revise", { phase: phase, reason: reason }));
}

function runCycleClose() {
  const slug = document.getElementById("closeSlug").value.trim();
  if (!slug) { toast("name the cycle first", "err"); document.getElementById("closeSlug").focus(); return; }
  askConfirm(
    "Close this cycle as " + esc(slug) + "?",
    "The phase deliverables are archived under that name and every gate resets for the next cycle.",
    () => runCommand("cycle_close", { slug: slug }));
}

// Static markup with no data behind it — app.js draws it once at load. It used to be rebuilt on
// every poll, which silently emptied a revise reason or cycle slug the moment the field lost focus.
export function renderOps() {
  const el = document.getElementById("ops");
  if (READ_ONLY) {
    el.innerHTML = '<div class="empty">Running with --read-only; nothing here can be run.</div>';
    return;
  }
  const phases = ["requirements", "design", "tasks", "build"].map(p => "<option>" + p + "</option>").join("");
  el.innerHTML =
    '<div class="subhead">Diagnostics</div>' +
    '<div class="row"><button data-ops="doctor">rein doctor</button>' +
    '<button data-ops="tests">make test</button></div>' +

    '<div class="subhead" style="margin-top:1.4rem">Send work backwards</div>' +
    '<p class="note">Rewinding an approval is a human privilege and it is not reversible by the ' +
    'loop: gates reset in a chain, and every task the impact analysis flags is reclassified rather ' +
    'than discarded.</p>' +
    '<div class="row"><select id="revPhase" aria-label="Roll back to phase">' + phases + "</select>" +
    '<input id="revReason" placeholder="why" size="30" aria-label="Reason for rolling back">' +
    '<button class="danger" data-ops="revise">rein revise</button></div>' +
    '<div class="row" style="margin-top:.6rem">' +
    '<input id="closeSlug" placeholder="cycle slug, e.g. payment-refactor" size="30" aria-label="Cycle slug">' +
    '<button class="danger" data-ops="close">rein cycle-close</button></div>' +
    '<div id="opsConfirm" class="confirm" hidden></div>';

  const OPS = { doctor: runDoctor, tests: runTests, revise: runRevise, close: runCycleClose };
  el.addEventListener("click", e => {
    const btn = e.target.closest("[data-ops]");
    if (!btn) return;
    const fn = OPS[btn.getAttribute("data-ops")];
    if (fn) fn();
  });
}
