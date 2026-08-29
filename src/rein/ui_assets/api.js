// Shared plumbing for every view: token-carrying POST, escaping, toasts, and the last status.
// ES module — loaded only via app.js; nothing here touches the DOM except #out and #toasts.

export const TOKEN = window.TOKEN;
export const READ_ONLY = window.READ_ONLY;

// The single mutable snapshot the views render from (app.js writes it when the poll brings a change).
// `etag` is the server's identity for `data` — sent back as If-None-Match so an unchanged SSOT costs
// a 304 and nothing else; clearing it forces the next poll to fetch a full payload.
export const state = { data: null, etag: null, lastGen: null };

// A supervised build is mostly waiting, and a backgrounded tab is nobody watching — poll it lazily.
export const POLL_MS = 3000;
export const POLL_HIDDEN_MS = 15000;
export function pollDelay() { return document.hidden ? POLL_HIDDEN_MS : POLL_MS; }

// Re-poll on the next tick regardless of what the server's ETag says (after a write, or when the
// human asks for it explicitly) — the caller still has to trigger the poll itself.
export function invalidate() { state.etag = null; }

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

// POSTs ask app.js for a fresh status via a DOM event (no circular import between the entry module
// and this one).
//
// `echo` is what separates the two kinds of write that come through here. A Console command's
// output IS the result, so it goes to #out on that screen. A decision recorded at a gate is not a
// command: its result is the repository moving, which the toast names and the next poll shows —
// echoing it would leave a stale approval payload sitting in a pane on another screen, read later
// as if it were the output of whatever was run last.
export async function post(path, body, echo = true) {
  const out = document.getElementById("out");
  const write = text => { if (!echo) return; out.hidden = false; out.textContent = text; };
  write("running…");
  try {
    const res = await fetch(path, { method:"POST",
      headers:{ "Content-Type":"application/json", "X-Rein-Token":TOKEN },
      body: JSON.stringify(body) });
    const data = await res.json();
    if (data.error) { write("ERROR: " + data.error); toast(data.error, "err"); return; }
    if ("exit_code" in data) {
      write("$ " + data.argv.join(" ") + "\n(exit " + data.exit_code + ")\n\n"
        + (data.stdout || "") + (data.stderr ? "\n[stderr]\n" + data.stderr : ""));
      toast((data.exit_code === 0 ? "✓ " : "✗ exit " + data.exit_code + " — ") + data.argv.join(" "),
        data.exit_code === 0 ? "ok" : "err");
    } else {
      write(JSON.stringify(data, null, 2));
      // /api/gate/approve DOES open the gate: the write session a launch link minted is the
      // capability handover, so reaching the handler means the approval was recorded and an
      // `approval_id` came back. This branch used to say "is ready — run the command shown to
      // approve", written when the endpoint only reported readiness. It kept saying it after the
      // endpoint started recording, so the one judgement in this product that widens what happens
      // next was announced to the human as not having happened. A refusal never arrives here —
      // an unready gate is a 409 and a moved repository a 409, both carrying `error`, which the
      // branch above owns.
      toast(data.approval_id
        ? "✓ gate " + data.gate + " approved (" + data.approval_id + ")"
        : "done", "ok");
    }
    invalidate();  // the action just moved the SSOT — take the next payload whatever the ETag says
    document.dispatchEvent(new CustomEvent("rein:refresh"));
  } catch (e) { write("request failed: " + e); toast("request failed", "err"); }
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
