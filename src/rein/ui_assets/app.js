// Entry module: the hash router, the status stream, theme, and the project switcher.
// Rendering lives in the view-* modules; shared plumbing and the route table in api.js.

import { READ_ONLY, esc, postJson, route, state, toast } from "/assets/api.js";
import { renderAttention, renderNext, renderStepper } from "/assets/view-overview.js";
import { renderReview } from "/assets/view-review.js";
import { renderTasks, renderTrace } from "/assets/view-tasks.js";
import { renderOps } from "/assets/view-activity.js";
import "/assets/notify.js";  // side-effect module: badges + opt-in notifications off rein:status

const VIEW_IDS = ["now", "gate", "board", "record", "console"];

// Only the visible view is rendered: a hidden one is caught up when it is opened, from the same
// snapshot. The spine is the exception — it is on every screen, so it always repaints.
function paintViews(includeGate) {
  const d = state.data;
  if (!d || d.error) return;
  renderStepper(d);
  const v = route().view;
  if (v === "now") { renderNext(d); renderAttention(d); }
  else if (v === "board") { renderTasks(d); renderTrace(d); }
  else if (v === "gate" && includeGate) renderReview();
  // record and console react to `rein:view` and `rein:record`; neither reads the snapshot
}

function showView() {
  const r = route();
  VIEW_IDS.forEach(name => { document.getElementById("view-" + name).hidden = name !== r.view; });
  document.querySelectorAll("#tabs .nav-item").forEach(a =>
    a.classList.toggle("active", a.getAttribute("data-view") === r.view));
  document.dispatchEvent(new CustomEvent("rein:view", { detail: r }));
  // The gate pane fetches its own deliverables off that event; painting it here as well would
  // put a second request in flight for the same gate.
  paintViews(false);
}

// ---- the status stream ----
// One EventSource for the life of the page. The server speaks only when the repository moves, so
// there is no interval here, no ETag, no lazy delay for a backgrounded tab and no "refresh now" —
// nothing to schedule, because nothing is being asked. EventSource reconnects on its own using the
// `retry:` the server sends, which is also the whole of the offline story.
function connect() {
  const dot = document.getElementById("dot");
  const live = document.getElementById("live");
  const es = new EventSource("/api/stream");

  es.addEventListener("status", e => {
    const d = JSON.parse(e.data);
    state.data = d;
    if (d.error) { document.getElementById("meta").textContent = "status error: " + d.error; return; }
    document.getElementById("meta").textContent =
      (d.project || "(no project)") + " · " + (d.branch || "-") + " · phase " + (d.current_phase || "-");
    document.dispatchEvent(new CustomEvent("rein:status", { detail: d }));
    paintViews(true);
  });

  // The audit log grew. It need not have changed a single field of the status payload, so it is
  // its own event; the Record screen refetches, and only when someone is looking at it.
  es.addEventListener("record", () => document.dispatchEvent(new CustomEvent("rein:record")));

  es.onopen = () => { dot.classList.remove("off"); live.textContent = "live"; };
  es.onerror = () => { dot.classList.add("off"); live.textContent = "reconnecting…"; };
}

// ---- project switcher (populated from /api/projects; switching persists the active target) ----
async function loadProjects() {
  const sel = document.getElementById("projectSelect");
  try {
    const res = await fetch("/api/projects");
    const d = await res.json();
    if (!d.projects || !d.projects.length) { sel.hidden = true; return; }
    sel.innerHTML = d.projects.map(p =>
      '<option value="' + esc(p.name) + '"' + (p.active ? " selected" : "") +
      (p.exists ? "" : " disabled") + ">" + esc(p.name) + (p.exists ? "" : " (missing)") + "</option>"
    ).join("");
    // Switching writes the active target to the user registry, so it needs the write path; a
    // read-only dashboard shows the current target but cannot change it.
    sel.disabled = READ_ONLY;
    sel.title = READ_ONLY ? "Target project (read-only: cannot switch)" : "Switch target project";
    sel.hidden = false;
  } catch { sel.hidden = true; }
}

// The stream re-reads the active project every tick, so switching needs no follow-up here: the
// next push is already the new repository's.
async function selectProject(name) {
  try {
    const { data } = await postJson("/api/project/select", { name });
    if (data.error) { toast(data.error, "err"); loadProjects(); return; }
    toast("→ " + name, "ok");
    loadProjects();
  } catch { toast("switch failed", "err"); }
}

// ---- theme (auto → dark → light → auto), persisted in localStorage ----
// `data-theme` only sets `color-scheme`; every colour is a light-dark() pair in app.css, so the
// page, its form controls and its scrollbars all follow one switch.
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  const btn = document.getElementById("themeBtn");
  btn.textContent = t || "auto";
  btn.title = "Colours: " + (t || "following your system") + " — click to change";
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const val = !cur ? "dark" : (cur === "dark" ? "light" : "");
  if (val) localStorage.setItem("rein-theme", val); else localStorage.removeItem("rein-theme");
  applyTheme(val);
}

applyTheme(localStorage.getItem("rein-theme") || "");
document.getElementById("themeBtn").onclick = toggleTheme;
document.getElementById("projectSelect").onchange = (e) => selectProject(e.target.value);
window.addEventListener("hashchange", showView);
renderOps();  // static markup, no data of its own — drawn once, never rebuilt under the human
showView();
connect();
loadProjects();
