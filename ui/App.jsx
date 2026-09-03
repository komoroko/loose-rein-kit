// The page: a topbar that says where you are and whether you are still being told, a spine whose
// gate list *is* the lifecycle, and one view.
//
// Only the routed view is mounted. The old page kept all five in the DOM and toggled `hidden`,
// which meant four screens rendering into nodes nobody was looking at and a "paint only the visible
// one" rule to stop them.

import { useEffect, useState } from "react";

import { READ_ONLY, awaitingGate, circled, getJson, postJson, toast } from "./api.js";
import { useRoute, useStream, useTheme, useToasts } from "./hooks.js";
import { useNotifier } from "./notify.js";
import Now from "./Now.jsx";
import Board from "./Board.jsx";
import RecordView from "./RecordView.jsx";
import ConsoleView from "./ConsoleView.jsx";
import Gate from "./gate/Gate.jsx";

// The lifecycle rail, and the page's only rendering of "which gate waits on you". Three states and
// no fourth: opened by a recorded human approval, waiting on you, not yet reached. The waiting one
// is the single inverted block on the page — nothing else on any screen is painted that way.
function Spine({ status, route }) {
  const awaiting = (awaitingGate(status) || {}).name;
  const item = (view, label) => (
    <a className={"nav-item" + (route.view === view ? " active" : "")} href={"#" + view} data-view={view}>
      {label}
    </a>
  );
  return (
    <nav className="spine" id="tabs" aria-label="Dashboard">
      {item("now", "Now")}
      <p className="spine-label">Gates</p>
      <div className="stations" id="stepper">
        {((status || {}).gates || []).map((g) => {
          const here = g.name === route.gate;
          const cls = [
            "station",
            g.status === "approved" ? "approved" : g.name === awaiting ? "awaiting" : "future",
            g.phase === status.current_phase ? "live" : "",
            here ? "active" : "",
          ].filter(Boolean).join(" ");
          // Every approval is a human's typed confirmation and the receipt id is the proof of it,
          // so an opened gate says which approval opened it rather than only that it is open.
          const title = g.status === "approved"
            ? `approved (${g.approval_id || "receipt unreadable"})`
            : g.name === awaiting
              ? "waiting on you — read it, then decide"
              : "not reached yet";
          return (
            <a
              key={g.name}
              className={cls}
              href={"#gate/" + g.name}
              title={title}
              aria-current={here ? "page" : undefined}
            >
              <span className="mark">{g.status === "approved" ? "✓" : g.name === awaiting ? "◆" : "·"}</span>
              <span className="gname">
                {g.name} <span className="gidx">{circled(g.index)}</span>
              </span>
            </a>
          );
        })}
      </div>
      <p className="spine-label">Inspect</p>
      {item("board", "Board")}
      {item("record", "Record")}
      {item("console", "Console")}
    </nav>
  );
}

// Switching writes the active target to the user registry, so it needs the write path; a read-only
// dashboard shows the current target but cannot change it.
function ProjectSelect() {
  const [projects, setProjects] = useState([]);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getJson("/api/projects").then((d) => {
      if (!cancelled) setProjects(d.projects || []);
    });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  // The stream re-reads the active project every tick, so switching needs no follow-up here: the
  // next push is already the new repository's.
  const select = async (name) => {
    try {
      const { data } = await postJson("/api/project/select", { name });
      if (data.error) toast(data.error, "err");
      else toast("→ " + name, "ok");
    } catch {
      toast("switch failed", "err");
    }
    setReload((n) => n + 1);
  };

  if (!projects.length) return null;
  return (
    <select
      id="projectSelect"
      value={(projects.find((p) => p.active) || {}).name || ""}
      disabled={READ_ONLY}
      title={READ_ONLY ? "Target project (read-only: cannot switch)" : "Switch target project"}
      onChange={(e) => select(e.target.value)}
    >
      {projects.map((p) => (
        <option key={p.name} value={p.name} disabled={!p.exists}>
          {p.name}
          {p.exists ? "" : " (missing)"}
        </option>
      ))}
    </select>
  );
}

function Toasts() {
  const items = useToasts();
  return (
    <div id="toasts" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={"toast " + t.kind}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const route = useRoute();
  const { status, live, recordSeq } = useStream();
  const [theme, cycleTheme] = useTheme();
  const notifier = useNotifier(status);

  const meta = !status
    ? "loading…"
    : status.error
      ? "status error: " + status.error
      : `${status.project || "(no project)"} · ${status.branch || "-"} · phase ${status.current_phase || "-"}`;

  return (
    <>
      <header className="topbar">
        <span className="wordmark">
          loose<i>rein</i>
        </span>
        <ProjectSelect />
        <span className="stat" id="meta">{meta}</span>
        <span className="grow"></span>
        {/* No refresh button: the server pushes when the repository moves, so there is nothing to
            ask for. This says whether the page is still being told. */}
        <span className="stat">
          <span className={"dot" + (live ? "" : " off")} id="dot"></span>
          <span id="live">{live ? "live" : "reconnecting…"}</span>
        </span>
        <button
          className="icon"
          id="bellBtn"
          aria-pressed={notifier.enabled ? "true" : "false"}
          title={notifier.enabled ? "Notifications on — click to stop" : "Notify me when a decision waits on me"}
          onClick={notifier.toggle}
        >
          {notifier.enabled ? "notify ●" : "notify ○"}
        </button>
        <button
          className="icon"
          id="themeBtn"
          title={"Colours: " + (theme || "following your system") + " — click to change"}
          onClick={cycleTheme}
        >
          {theme || "auto"}
        </button>
      </header>

      <div className="frame">
        <Spine status={status} route={route} />
        <main>
          {route.view === "now" && <Now status={status} />}
          {/* Keyed: a different gate — or the same gate in a different repository — is a
              different reading, so it gets its own state rather than inheriting the last one's. */}
          {route.view === "gate" && (
            <Gate key={((status || {}).project || "") + ":" + route.gate} status={status} gate={route.gate} />
          )}
          {route.view === "board" && <Board status={status} />}
          {route.view === "record" && <RecordView recordSeq={recordSeq} />}
          {route.view === "console" && <ConsoleView status={status} />}
        </main>
      </div>

      <Toasts />
    </>
  );
}
