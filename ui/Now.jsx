// The Now view: the next recommended command, and what stands between this repository and its
// next gate.

import { awaitingGate, circled, copyCmd } from "./api.js";
import { Chip, Empty, Scroll, Warn } from "./parts.jsx";

function NextCommand({ status }) {
  const n = status.next || {};
  const awaiting = awaitingGate(status);
  // A command the human runs is not a link to the gate; a decision at a gate is.
  const showRead = !(n.kind === "run_phase" || n.kind === "close" || !awaiting);
  const also = n.also || [];
  return (
    <>
      <div className="console">
        <span className="prompt">▸</span>
        <code className="cmd">{n.command}</code>
        <button onClick={(e) => copyCmd(n.command || "", e.currentTarget)}>copy</button>
      </div>
      <p className="lede">{n.reason}</p>
      {also.length || showRead ? (
        <div className="row">
          {also.length ? "also: " : null}
          {also.map((a) => (
            <span className="chip" key={a}>
              {a}
            </span>
          ))}
          {showRead ? (
            <a className="chip clk" href={"#gate/" + awaiting.name}>
              read gate {circled(awaiting.index)} →
            </a>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

// Everything standing between this repository and its next gate arrives pre-derived as `pending`
// (status_api.pending_queue): gate blockers straight out of approve.readiness, open escalations,
// stuck tasks, ungrounded claims — one list, already sorted worst-first, each row carrying the
// command that addresses it. This pane deliberately re-derives none of it from `attention` and
// `tasks.rows`: two places deciding what "needs attention" means is exactly how the Board and this
// screen drift apart.
//
// `pending_deep: false` means gate readiness was not probed on this push. It is said out loud,
// because a queue that silently omits its blocking rows reads like a repository with none.
function InTheWay({ status }) {
  const awaiting = awaitingGate(status);
  const pending = status.pending || [];
  const blocking = pending.filter((p) => p.severity === "blocking").length;

  return (
    <>
      {(status.warnings || []).map((w) => (
        <Warn key={w}>{w}</Warn>
      ))}
      {pending.length ? (
        <>
          <div className="subhead">
            {pending.length} waiting on you
            {blocking ? ` · ${blocking} blocking` : ""}
            {status.pending_deep === false ? " · gate readiness not probed" : ""}
          </div>
          <Scroll>
            <table>
              <tbody>
                <tr>
                  <th></th>
                  <th>Subject</th>
                  <th>What is in the way</th>
                  <th>Next</th>
                </tr>
                {pending.map((p, i) => (
                  <tr key={p.subject + i}>
                    <td>
                      <Chip id={p.severity} status={p.severity} critical={p.severity === "blocking"} />
                    </td>
                    <td className="mono">{p.subject}</td>
                    <td>{p.headline}</td>
                    <td className="mono">{p.action || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Scroll>
        </>
      ) : awaiting ? (
        <p className="note">
          Nothing is in the way of gate {circled(awaiting.index)} {awaiting.name}.{" "}
          <a href={"#gate/" + awaiting.name}>Read it</a>, then decide.
        </p>
      ) : (
        <p className="note">Every gate is open. Nothing is waiting on you.</p>
      )}
    </>
  );
}

export default function Now({ status }) {
  if (!status || status.error) {
    return (
      <div className="view" id="view-now">
        <div className="block">
          <Empty>{status ? status.error : "waiting for status…"}</Empty>
        </div>
      </div>
    );
  }
  const awaiting = awaitingGate(status);
  const named = awaiting ? `gate ${circled(awaiting.index)} ${awaiting.name}` : "the next gate";
  return (
    <div className="view" id="view-now">
      <div className="block">
        <h2>Do this next</h2>
        <div id="next">
          <NextCommand status={status} />
        </div>
      </div>
      <div className="block">
        <h2 id="attentionHead">In the way of {named}</h2>
        <div id="attention">
          <InTheWay status={status} />
        </div>
      </div>
    </div>
  );
}
