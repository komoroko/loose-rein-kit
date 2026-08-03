// Entry module: hash-routed tabs, the status poll, theme, and the project switcher.
// Rendering lives in the view-* modules; shared plumbing in api.js.

import { READ_ONLY, TOKEN, awaitingGate, esc, invalidate, pollDelay, state, toast } from "/assets/api.js";
import { renderAttention, renderNext, renderStepper } from "/assets/view-overview.js";
import { renderReview } from "/assets/view-review.js";
import { renderTasks, renderTrace } from "/assets/view-tasks.js";
import { renderOps } from "/assets/view-activity.js";
import "/assets/notify.js";  // side-effect module: badges + opt-in notifications off rein:status

// ---- tabs (location.hash is the router; unknown/empty hash lands on overview) ----
const VIEWS = ["overview", "review", "tasks", "activity"];
export function currentView() {
  const h = location.hash.replace("#", "");
  return VIEWS.includes(h) ? h : "overview";
}
// Only the visible tab is rendered. A poll used to rebuild all four (the DAG's SVG included) whether
// or not anyone could see them; a hidden tab is caught up when it is opened, from the same snapshot.
const RENDER = {
  overview: d => { renderStepper(d); renderNext(d); renderAttention(d); },
  review: () => renderReview(),  // reads the shared snapshot itself; nothing to pass
  tasks: d => { renderTasks(d); renderTrace(d); },
  activity: () => {},  // the feed polls itself off `rein:view`; nothing here reads the snapshot
};
function renderCurrent() {
  if (state.data && !state.data.error) RENDER[currentView()](state.data);
}

function showView() {
  const v = currentView();
  VIEWS.forEach(name => {
    document.getElementById("view-" + name).style.display = name === v ? "" : "none";
    document.querySelector('#tabs a[data-view="' + name + '"]').classList.toggle("active", name === v);
  });
  document.dispatchEvent(new CustomEvent("rein:view", { detail: v }));
  if (v !== "review") renderCurrent();  // the review pane fetches its own deliverables on entry
}

function updateReviewBadge(d) {
  const badge = document.getElementById("reviewBadge");
  const awaiting = awaitingGate(d);
  if (awaiting) { badge.textContent = "◆ g" + awaiting.index; badge.style.display = ""; }
  else badge.style.display = "none";
}

// ---- project switcher (populated from /api/projects; switching persists the active target) ----
async function loadProjects() {
  const sel = document.getElementById("projectSelect");
  try {
    const res = await fetch("/api/projects");
    const d = await res.json();
    if (!d.projects || !d.projects.length) { sel.style.display = "none"; return; }
    sel.innerHTML = d.projects.map(p =>
      '<option value="' + esc(p.name) + '"' + (p.active ? " selected" : "") +
      (p.exists ? "" : " disabled") + ">" + esc(p.name) + (p.exists ? "" : " (missing)") + "</option>"
    ).join("");
    // Switching writes the active target to the user registry, so it needs the write path; a
    // read-only dashboard shows the current target but cannot change it.
    sel.disabled = READ_ONLY;
    sel.title = READ_ONLY ? "Target project (read-only: cannot switch)" : "Switch target project";
    sel.style.display = "";
  } catch (e) { sel.style.display = "none"; }
}
async function selectProject(name) {
  try {
    const res = await fetch("/api/project/select", { method:"POST",
      headers:{ "Content-Type":"application/json", "X-Rein-Token":TOKEN },
      body: JSON.stringify({ name }) });
    const d = await res.json();
    if (d.error) { toast(d.error, "err"); loadProjects(); return; }
    toast("→ " + name, "ok");
    invalidate(); await refresh(); loadProjects();
  } catch (e) { toast("switch failed", "err"); }
}

// ---- theme (auto → dark → light → auto), persisted in localStorage ----
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const val = !cur ? "dark" : (cur === "dark" ? "light" : "");
  if (val) localStorage.setItem("rein-theme", val); else localStorage.removeItem("rein-theme");
  applyTheme(val);
  document.dispatchEvent(new CustomEvent("rein:theme", { detail: val }));
}
applyTheme(localStorage.getItem("rein-theme") || "");

// ---- the status poll ----
function tickAgo() {
  const el = document.getElementById("ago");
  if (!state.lastGen) { el.textContent = "—"; return; }
  const secs = Math.max(0, Math.round((Date.now() - new Date(state.lastGen).getTime()) / 1000));
  el.textContent = secs < 60 ? ("updated " + secs + "s ago") : ("updated " + Math.round(secs / 60) + "m ago");
}

// The server's ETag identifies the *state*, not the moment it was read, so an idle repo answers
// 304 with an empty body: no transfer, no parse, and — crucially — no re-render. Every DOM node the
// human is using (a selected task's detail, a half-typed ops field, the scroll inside a long patch)
// survives for as long as the SSOT does not actually move.
async function refresh() {
  const dot = document.getElementById("dot");
  try {
    const res = await fetch("/api/status", state.etag ? { headers: { "If-None-Match": state.etag } } : undefined);
    dot.classList.remove("off");
    if (res.status === 304) {
      state.lastGen = new Date().toISOString();  // the server just confirmed this snapshot is current
      tickAgo();
      return;
    }
    const d = await res.json();
    state.etag = res.headers.get("ETag");
    state.data = d; state.lastGen = d.generated_at;
    if (d.error) { document.getElementById("meta").textContent = "status error: " + d.error; return; }
    document.getElementById("meta").textContent =
      (d.project || "(no project)") + " · " + (d.branch || "-") + " · phase " + (d.current_phase || "-");
    updateReviewBadge(d);
    document.dispatchEvent(new CustomEvent("rein:status", { detail: d }));
    renderCurrent();
    tickAgo();
  } catch (e) {
    dot.classList.add("off");
    document.getElementById("ago").textContent = "disconnected";
  }
}

// Self-rescheduling rather than setInterval, so the delay can follow tab visibility.
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => { await refresh(); schedulePoll(); }, pollDelay());
}

document.getElementById("themeBtn").onclick = toggleTheme;
document.getElementById("refreshBtn").onclick = () => { invalidate(); refresh(); };
document.getElementById("projectSelect").onchange = (e) => selectProject(e.target.value);
document.addEventListener("rein:refresh", () => refresh());
window.addEventListener("hashchange", showView);
// Coming back to the tab should show current state at once, not after the lazy delay it was on.
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); schedulePoll(); });
renderOps();  // static markup, no data of its own — drawn once, never rebuilt under the human
showView();
refresh();
loadProjects();
schedulePoll();
setInterval(tickAgo, 1000);
