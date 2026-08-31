// Shared plumbing for every view: token-carrying POST, escaping, toasts, and the last status.
// ES module — loaded only via app.js; nothing here touches the DOM except #out and #toasts.

export const TOKEN = window.TOKEN;
export const READ_ONLY = window.READ_ONLY;

// The single mutable snapshot the views render from. app.js writes it when the stream pushes a new
// one — and the server only pushes when the repository actually moved, so there is nothing here to
// invalidate, dedupe or schedule.
export const state = { data: null };

// ---- routing ----
// The hash is the router, and it carries the gate: a reading room is a place you can link to,
// bookmark, and come back to, not a selection held in a module variable. Unknown or empty lands
// on `now`, the screen that says what to do.
const PLAIN_VIEWS = ["now", "board", "record", "console"];
export function route() {
  const h = location.hash.replace(/^#/, "");
  if (h.startsWith("gate/")) {
    const gate = decodeURIComponent(h.slice(5));
    if (gate) return { view: "gate", gate };
  }
  return { view: PLAIN_VIEWS.includes(h) ? h : "now", gate: null };
}

export const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// Gate indices, in the notation the documents use (AGENTS.md, the phase commands, the review
// pane's own prose) — so the dashboard and the docs name the same gate the same way.
const CIRCLED = ["", "①", "②", "③", "④", "⑤"];
export function circled(i) { return CIRCLED[i] || ("g" + i); }

export function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 3200);
}

// The authorized POST: the write session (the cookie, sent automatically) plus the CSRF token.
// Callers get the status back too, because 409 is a real answer here — a machine review that moved
// under a human-review write is not an error to report, it is a reload to do.
export async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Rein-Token": TOKEN },
    body: JSON.stringify(body),
  });
  return { status: res.status, data: await res.json() };
}

// A Console command. Its output IS the result, so it goes to #out, on the screen that ran it.
export async function runCommand(action, params) {
  const out = document.getElementById("out");
  out.hidden = false;
  out.textContent = "running…";
  try {
    const { data } = await postJson("/api/run", { action, params });
    if (data.error) { out.textContent = "ERROR: " + data.error; toast(data.error, "err"); return; }
    out.textContent = "$ " + data.argv.join(" ") + "\n(exit " + data.exit_code + ")\n\n"
      + (data.stdout || "") + (data.stderr ? "\n[stderr]\n" + data.stderr : "");
    toast((data.exit_code === 0 ? "✓ " : "✗ exit " + data.exit_code + " — ") + data.argv.join(" "),
      data.exit_code === 0 ? "ok" : "err");
  } catch (e) { out.textContent = "request failed: " + e; toast("request failed", "err"); }
}

// A decision recorded at a gate. Nothing is echoed anywhere: the result of a decision is the
// repository moving, and the stream reports that by itself within a tick. Resolves true when the
// write landed, so a caller can refetch the one thing the stream does not carry — its own pane.
//
// /api/gate/approve DOES open the gate: the write session a launch link minted is the capability
// handover, so reaching the handler means the approval was recorded and an `approval_id` came back.
// The toast here once told the human the gate was merely ready and to go and run the approval
// themselves — wording from when the endpoint only reported readiness, kept after it started
// recording. So the one judgement in this product that widens what happens next was announced as
// not having happened. A refusal never arrives here: an unready gate is a 409 and a moved
// repository a 409, both carrying `error`, which the branch above owns.
export async function record(path, body) {
  try {
    const { data } = await postJson(path, body);
    if (data.error) { toast(data.error, "err"); return false; }
    toast(data.approval_id
      ? "✓ gate " + data.gate + " approved (" + data.approval_id + ")"
      : "done", "ok");
    return true;
  } catch (e) { toast("request failed: " + e, "err"); return false; }
}

export function copyCmd(cmd, btn) {
  if (navigator.clipboard) navigator.clipboard.writeText(cmd);
  if (btn) { const o = btn.textContent; btn.textContent = "✓ copied"; setTimeout(() => btn.textContent = o, 1200); }
}

export function taskById(id) {
  return (state.data && state.data.tasks) ? state.data.tasks.rows.find(x => x.id === id) : null;
}

// The gate the human is standing at: the first one not yet approved. Derived here once — the
// stepper, the tab badge, the review pane and the notifier all have to agree on it.
export function awaitingGate(d) {
  return ((d || {}).gates || []).find(g => g.status !== "approved") || null;
}

export function chip(id, status, critical, clickable) {
  return '<span class="chip ' + esc(status) + (critical ? " critical" : "") + (clickable ? " clk" : "") +
    '" title="' + esc(status) + '"' + taskAttr(clickable && id) + ">" + esc(id) + "</span>";
}

// Task ids come from tasks.yaml, which is agent-written and *not* pattern-validated on load
// (dag.py takes `str(raw["id"])` as-is). Interpolating one into an inline `onclick="f('…')"` would
// let a single quote in an id close the JS string and run arbitrary script on this page — the page
// that holds the approval token, i.e. exactly the XSS→self-approval path mdlite.py exists to make
// impossible. So the id travels as an escaped *attribute value* and a delegated listener reads it
// back with getAttribute; no id ever becomes code.
export function taskAttr(id) {
  return id ? ' data-task="' + esc(id) + '"' : "";
}

export function onTaskClick(handler) {
  document.addEventListener("click", e => {
    const el = e.target.closest && e.target.closest("[data-task]");
    if (el) handler(el.getAttribute("data-task"));
  });
}

export function tableFrom(headers, rows) {
  const th = "<tr>" + headers.map(h => "<th>" + esc(h) + "</th>").join("") + "</tr>";
  const tr = rows.map(r => "<tr>" + r.map(c => "<td>" + esc(c) + "</td>").join("") + "</tr>").join("");
  return '<div class="scroll"><table>' + th + tr + "</table></div>";
}

// Generated HTML uses inline onclick= handlers; modules are not global scope, so the few
// functions those handlers name are published on window explicitly (here and in the views).
window.copyCmd = copyCmd;
