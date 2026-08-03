// Approval-wait notifications: the human's response latency is the loop's bottleneck, so the
// dashboard actively signals "it's your turn" instead of waiting to be looked at. Three layers:
// browser notifications (opt-in via the bell — permission needs a user gesture), the tab title
// badge, and a canvas-drawn favicon (offline self-contained: no image assets, no external URLs).
//
// The unit of interruption is a DECISION, not an event. This used to fire on four separate status
// transitions — a gate becoming current, an escalation opening, a task going needs-revision, a
// build finishing — of which two were the same decision seen from different angles and one was not
// a decision at all. So a busy moment produced a burst of pings that together said "look at the
// dashboard", which is what the dashboard already says by existing.
//
// Now the server derives one `status.decision` from the same table `rein next` prints, with an
// `id` that changes only when the decision itself changes. One decision, one notification, carrying
// the single command that settles it; re-deriving the same decision is silent no matter how much
// else moved underneath it.

import { awaitingGate, state, toast } from "/assets/api.js";

let enabled = localStorage.getItem("rein-notify") === "on";
let prev = null;  // last snapshot; transitions only fire within the same project

function snapshot(d) {
  const awaiting = awaitingGate(d);
  const decision = d.decision || {};
  return {
    project: d.project || "",
    awaiting: awaiting ? awaiting.name : null,
    awaitingIndex: awaiting ? awaiting.index : 0,
    // How much is waiting comes from the status queue's counts, not from the event list. An
    // escalation is only one way a repository stops moving: a review bound to a commit that is no
    // longer HEAD, or an undispositioned finding, blocks the gate and writes no event at all — so
    // counting events left the badge quiet for a repository that could not advance.
    blocking: decision.blocking || 0,
    openItems: decision.open || 0,
    decisionId: decision.waiting_on_human ? (decision.id || "") : "",
    decisionHeadline: decision.headline || "",
    decisionAction: decision.action || "",
  };
}

function notify(body) {
  if (!enabled || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try { new Notification("Loose Rein — " + ((state.data || {}).project || "dashboard"), { body }); }
  catch (e) { /* headless/denied environments: the title/favicon badges still carry the signal */ }
}

// teal = quiet loop, amber = a gate waits on the human, red = something blocks the gate outright
function faviconColor(s) {
  const styles = getComputedStyle(document.documentElement);
  const raw = s.blocking > 0 ? (styles.getPropertyValue("--bad") || "#c23b2f")
    : s.awaiting ? (styles.getPropertyValue("--gate") || "#b3760f")
    : (styles.getPropertyValue("--accent") || "#0c7d73");
  return raw.trim();
}

// Only three distinct icons exist, and they follow the loop's mood, not its detail — so repaint only
// when the colour actually changes. Otherwise every status change (a task flipping to done, say)
// would allocate a canvas, PNG-encode it, and hand the browser a fresh data: URI to decode.
let lastColor = null;
function favicon(s) {
  const color = faviconColor(s);
  if (color === lastColor) return;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const g = canvas.getContext("2d");
  if (!g) return;
  g.beginPath();
  g.arc(16, 16, 13, 0, Math.PI * 2);
  g.fillStyle = color;
  g.fill();
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = canvas.toDataURL("image/png");
  lastColor = color;
}

function badges(s) {
  const flag = s.blocking > 0 ? "(!" + s.openItems + ") "
    : s.openItems > 0 ? "(◆" + s.openItems + ") "
    : (s.awaiting ? "(◆g" + s.awaitingIndex + ") " : "");
  document.title = flag + "Loose Rein — " + (s.project || "dashboard");
  favicon(s);
}

// One decision, one notification. A changed `id` is a genuinely different call to make; the same
// id re-derived is the same call, however many tasks moved in between.
function onStatus(d) {
  const s = snapshot(d);
  if (prev && prev.project === s.project && s.decisionId && s.decisionId !== prev.decisionId)
    notify(s.decisionHeadline + (s.decisionAction ? "\n▸ " + s.decisionAction : ""));
  prev = s;
  badges(s);
}

async function toggle() {
  if (!enabled) {
    if (typeof Notification !== "undefined" && Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("browser notifications are blocked", "err"); return; }
    }
    enabled = true;
    localStorage.setItem("rein-notify", "on");
    toast("notifications on", "ok");
  } else {
    enabled = false;
    localStorage.setItem("rein-notify", "off");
    toast("notifications off");
  }
  paintBell();
}

function paintBell() {
  const btn = document.getElementById("bellBtn");
  btn.textContent = enabled ? "🔔" : "🔕";
  btn.title = enabled ? "Notifications on (click to disable)" : "Notify me when a gate or escalation waits";
}

document.getElementById("bellBtn").onclick = toggle;
document.addEventListener("rein:status", e => onStatus(e.detail));
// The badge colours come from theme variables, and an idle repo can go a long time without a
// changed status payload — so a theme switch has to invalidate the cached icon itself.
document.addEventListener("rein:theme", () => { lastColor = null; if (prev) badges(prev); });
paintBell();
