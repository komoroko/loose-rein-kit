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
// Now the server derives one `status.decision` from the same table `rein next` prints, with an `id`
// that changes only when the decision itself changes. One decision, one notification, carrying the
// single command that settles it; re-deriving the same decision is silent no matter how much else
// moved underneath it.

import { useEffect, useRef, useState } from "react";

import { awaitingGate, toast } from "./api.js";

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
    decisionId: decision.waiting_on_human ? decision.id || "" : "",
    decisionHeadline: decision.headline || "",
    decisionAction: decision.action || "",
  };
}

// grey = quiet loop, brass = a gate waits on the human, red = something blocks the gate outright.
//
// These three are the notifier's own, not the page's. The page signals "waiting on you" by
// inverting the gate's row, which a 32px disc in browser chrome cannot do — and chrome is painted
// by the browser's theme, not the dashboard's, so a colour taken from app.css would be tuned
// against the wrong background half the time.
const FAVICON = { quiet: "#6b7680", waiting: "#c08a1e", blocked: "#c8412e" };

function faviconColor(s) {
  return s.blocking > 0 ? FAVICON.blocked : s.awaiting ? FAVICON.waiting : FAVICON.quiet;
}

// Only three distinct icons exist, and they follow the loop's mood, not its detail — so repaint only
// when the colour actually changes. Otherwise every status change (a task flipping to done, say)
// would allocate a canvas, PNG-encode it, and hand the browser a fresh data: URI to decode.
function paintFavicon(color) {
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
}

/**
 * The bell's state, plus the title/favicon badges and the one notification a new decision earns.
 *
 * The badges are a side effect on documents the page does not own (`document.title`, the favicon
 * link in `<head>`), which is why they live in an effect rather than in anybody's render.
 */
export function useNotifier(status) {
  const [enabled, setEnabled] = useState(() => localStorage.getItem("rein-notify") === "on");
  const prev = useRef(null);
  const lastColor = useRef(null);

  useEffect(() => {
    if (!status || status.error) return;
    const s = snapshot(status);

    // One decision, one notification. A changed `id` is a genuinely different call to make; the
    // same id re-derived is the same call, however many tasks moved in between. Transitions only
    // fire within the same project.
    const previous = prev.current;
    if (previous && previous.project === s.project && s.decisionId && s.decisionId !== previous.decisionId) {
      if (enabled && typeof Notification !== "undefined" && Notification.permission === "granted") {
        try {
          new Notification("Loose Rein — " + (s.project || "dashboard"), {
            body: s.decisionHeadline + (s.decisionAction ? "\n▸ " + s.decisionAction : ""),
          });
        } catch {
          /* headless/denied environments: the title and favicon badges still carry the signal */
        }
      }
    }
    prev.current = s;

    const flag = s.blocking > 0
      ? `(!${s.openItems}) `
      : s.openItems > 0
        ? `(◆${s.openItems}) `
        : s.awaiting
          ? `(◆g${s.awaitingIndex}) `
          : "";
    document.title = flag + "Loose Rein — " + (s.project || "dashboard");

    const color = faviconColor(s);
    if (color !== lastColor.current) {
      paintFavicon(color);
      lastColor.current = color;
    }
  }, [status, enabled]);

  async function toggle() {
    if (!enabled) {
      if (typeof Notification !== "undefined" && Notification.permission !== "granted") {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          toast("browser notifications are blocked", "err");
          return;
        }
      }
      localStorage.setItem("rein-notify", "on");
      setEnabled(true);
      toast("notifications on", "ok");
    } else {
      localStorage.setItem("rein-notify", "off");
      setEnabled(false);
      toast("notifications off");
    }
  }

  return { enabled, toggle };
}
