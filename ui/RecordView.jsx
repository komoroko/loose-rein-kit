// The Record: the hash-chained event log, for watching a headless build.
//
// It refetches when the log actually changed — the server says so, on its own stream event — and
// only while somebody is on this screen. There is no timer and no response-body comparison: both
// existed to make blind polling survivable, and there is no longer any blind polling. Mounting is
// now the whole of "somebody is looking", because the view is not in the DOM when it is not routed.

import { useEffect, useState } from "react";

import { getJson } from "./api.js";
import { Empty, Scroll, Warn } from "./parts.jsx";

const ESCALATION_KINDS = new Set(["blocked", "merge_conflict", "integration_red", "no_runnable", "gate_violation"]);
const OK_KINDS = new Set(["gate_approved", "task_done", "resolve", "security_review"]);

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
  const parts = Object.keys(detail).map((k) => {
    const v = detail[k];
    const s = v !== null && typeof v === "object" ? JSON.stringify(v) : String(v);
    return k + "=" + (s.length > 60 ? s.slice(0, 57) + "…" : s);
  });
  return parts.length ? parts.join("  ") : "-";
}

export default function RecordView({ recordSeq }) {
  const [feed, setFeed] = useState(null);

  useEffect(() => {
    let live = true;
    getJson("/api/events?limit=50").then((d) => {
      if (live) setFeed(d);
    });
    return () => {
      live = false;
    };
  }, [recordSeq]);

  return (
    <div className="view" id="view-record">
      <div className="block">
        <h2>Record</h2>
        <p className="note">
          Every state change, hash-chained. A deleted or reordered line breaks the chain a gate
          receipt pins.
        </p>
        <div id="events">
          {!feed ? (
            <Empty>loading…</Empty>
          ) : feed.error ? (
            <Warn>{feed.error}</Warn>
          ) : !feed.events.length ? (
            <Empty>No events yet (written on the first one).</Empty>
          ) : (
            <>
              <Scroll>
                <table className="events">
                  <tbody>
                    <tr>
                      <th>#</th>
                      <th>Date</th>
                      <th>Event</th>
                      <th>Actor</th>
                      <th>Subjects</th>
                      <th>Detail</th>
                    </tr>
                    {feed.events.map((e) => (
                      <tr className={eventClass(e)} key={e.seq}>
                        <td>{e.seq}</td>
                        <td>{e.date}</td>
                        <td className="mono">
                          {e.event}
                          {e.needs_decision ? " ◆" : ""}
                        </td>
                        <td>{e.actor || "-"}</td>
                        <td className="mono">{(e.subject_ids || []).join(", ") || "-"}</td>
                        <td>{detailText(e.detail)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Scroll>
              <div className="empty" style={{ marginTop: ".4rem" }}>
                latest {feed.events.length} of {feed.total}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
