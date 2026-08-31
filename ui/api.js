// Shared plumbing: the authorized POST, the toast store, and the few derivations every view needs.
//
// There is no `esc` here any more. JSX escapes every interpolation by default, so the page's
// escaping is a property of the renderer rather than of 103 hand-written calls that had to be
// remembered one at a time — on the page that holds the approval token, where a forgotten one is
// an XSS into a self-approval. The two places that put server HTML in the DOM say so out loud
// with `dangerouslySetInnerHTML`, and both render mdlite output, which escapes at the source.

export const TOKEN = window.TOKEN;
export const READ_ONLY = window.READ_ONLY;

// Gate indices in the notation the documents use (AGENTS.md, the phase commands, the review pane's
// own prose) — so the dashboard and the docs name the same gate the same way.
const CIRCLED = ["", "①", "②", "③", "④", "⑤"];
export function circled(i) {
  return CIRCLED[i] || "g" + i;
}

// ---- toasts ----
// A store rather than a hook, because the callers are event handlers and async writes, not
// components. `<Toasts/>` subscribes; everything else just calls `toast()`.

let toasts = [];
let nextToastId = 0;
const toastSubs = new Set();

function publish() {
  for (const fn of toastSubs) fn(toasts);
}

export function subscribeToasts(fn) {
  toastSubs.add(fn);
  return () => toastSubs.delete(fn);
}

export function toastList() {
  return toasts;
}

export function toast(message, kind) {
  const id = ++nextToastId;
  toasts = toasts.concat({ id, message, kind: kind || "" });
  publish();
  setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    publish();
  }, 3500);
}

// ---- writes ----

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

// A decision recorded at a gate. Nothing is echoed anywhere: the result of a decision is the
// repository moving, and the stream reports that by itself within a tick. Resolves true when the
// write landed, so a caller can refetch the one thing the stream does not carry — its own pane.
//
// /api/gate/approve DOES open the gate: the write session a launch link minted is the capability
// handover, so reaching the handler means the approval was recorded and an `approval_id` came back.
// A refusal never arrives here: an unready gate is a 409 and a moved repository a 409, both
// carrying `error`, which the branch below owns.
export async function record(path, body) {
  try {
    const { data } = await postJson(path, body);
    if (data.error) {
      toast(data.error, "err");
      return false;
    }
    toast(data.approval_id ? `✓ gate ${data.gate} approved (${data.approval_id})` : "done", "ok");
    return true;
  } catch (e) {
    toast("request failed: " + e, "err");
    return false;
  }
}

/** GET a JSON route, turning a transport failure into the same `{error}` shape a handler returns. */
export async function getJson(path) {
  try {
    const res = await fetch(path);
    return await res.json();
  } catch (e) {
    return { error: "request failed: " + e };
  }
}

// ---- derivations every view shares ----

// The gate the human is standing at: the first one not yet approved. Derived once — the spine, the
// tab badge, the review pane and the notifier all have to agree on it.
export function awaitingGate(status) {
  return ((status || {}).gates || []).find((g) => g.status !== "approved") || null;
}

export function copyCmd(cmd, el) {
  if (navigator.clipboard) navigator.clipboard.writeText(cmd);
  if (el) {
    const original = el.textContent;
    el.textContent = "✓ copied";
    setTimeout(() => {
      el.textContent = original;
    }, 1200);
  }
}
