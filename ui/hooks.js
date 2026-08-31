// The three things the whole page is driven by: the route, the status stream, and the theme.

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { subscribeToasts, toastList } from "./api.js";

// ---- the hash router ----
// The hash carries the gate: a reading room is a place you can link to, bookmark and come back to,
// not a selection held in a module variable. Unknown or empty lands on `now`, the screen that says
// what to do.
const PLAIN_VIEWS = ["now", "board", "record", "console"];

function readRoute() {
  const h = location.hash.replace(/^#/, "");
  if (h.startsWith("gate/")) {
    const gate = decodeURIComponent(h.slice(5));
    if (gate) return { view: "gate", gate };
  }
  return { view: PLAIN_VIEWS.includes(h) ? h : "now", gate: null };
}

export function useRoute() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

// ---- the status stream ----
// One EventSource for the life of the page. The server speaks only when the repository moves, so
// there is no interval here, no ETag, no lazy delay for a backgrounded tab and no "refresh now" —
// nothing to schedule, because nothing is being asked. EventSource reconnects on its own using the
// `retry:` the server sends, which is also the whole of the offline story.
//
// `recordSeq` counts pushes on the audit log. The log can grow without moving a single field of the
// status payload, so it is its own event; the Record screen refetches off this counter, and only
// when someone is looking at it.
export function useStream() {
  const [status, setStatus] = useState(null);
  const [live, setLive] = useState(false);
  const [recordSeq, setRecordSeq] = useState(0);

  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.addEventListener("status", (e) => setStatus(JSON.parse(e.data)));
    es.addEventListener("record", () => setRecordSeq((n) => n + 1));
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    return () => es.close();
  }, []);

  return { status, live, recordSeq };
}

// ---- theme (auto → dark → light → auto), persisted in localStorage ----
// `data-theme` only sets `color-scheme`; every colour is a light-dark() pair in app.css, so the
// page, its form controls and its scrollbars all follow one switch.
export function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("rein-theme") || "");

  useEffect(() => {
    if (theme) document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
  }, [theme]);

  const cycle = useCallback(() => {
    setTheme((current) => {
      const next = !current ? "dark" : current === "dark" ? "light" : "";
      if (next) localStorage.setItem("rein-theme", next);
      else localStorage.removeItem("rein-theme");
      return next;
    });
  }, []);

  return [theme, cycle];
}

export function useToasts() {
  return useSyncExternalStore(subscribeToasts, toastList, toastList);
}
