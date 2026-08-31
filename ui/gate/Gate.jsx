// The gate reading room: read what the approval would cover, then decide — in one place.
//
// Which gate is being read is the route (`#gate/<name>`), not a selection held here: a reading room
// is somewhere you can link to and come back to. The spine is the only rendering of gate state on
// the page, so this module draws no gate list of its own.
//
// Gates ①②③⑤ are deliverable review: a document list on the left, rendered markdown on the right.
// Gate ④ is different in kind — it reviews a generated grounded review, and what it asks for is a
// judgement, so its left rail is the review stages and its body is a form at every stage.
//
// The pane used to repaint in two grains, because rebuilding the body from strings would wipe a
// form the reviewer was half way through — so a status push repainted only the heading and the
// footer, and an open footer panel suppressed its own repaint. React reconciles instead of
// replacing, so a push can no longer take anything out from under the human and all of that is
// gone: one render, from state.

import { useCallback, useEffect, useState } from "react";

import { READ_ONLY, circled, getJson, postJson, record, toast } from "../api.js";
import { Empty, Warn } from "../parts.jsx";
import { DeliverableBody, DeliverableList, mainEntries } from "./Deliverables.jsx";
import { StageBody, StageList } from "./stages.jsx";

// Opened documents are a client-side memory aid that outlives a visit to the room, so they live
// beside the module rather than in component state. Nothing in the approval path consults this: what
// a gate-④ tick means instead is human_review.stage_settled, a judgement the repository can show
// afterwards.
const openedSets = {};
function openedSet(project, gate) {
  const key = (project || "") + ":" + gate;
  return (openedSets[key] = openedSets[key] || new Set());
}

// Where to land the reviewer: the first stage still carrying an unrecorded judgement, else the
// first one. No stage is withheld — the whole review is readable from the moment it is generated,
// and what the reviewer owes is a decision, not a sequence.
function firstUnsettled(stages) {
  return ((stages.find((s) => s.settled === false) || stages[0] || {}).name) || null;
}

// The gate's identity line. The spine says which gate waits on you; this says what you are reading
// and, when it is already open, which recorded approval opened it.
function GateHead({ status, gate, review }) {
  const g = ((status || {}).gates || []).find((x) => x.name === gate) || {};
  const where = g.status === "approved"
    ? "opened by approval " + (g.approval_id || "(receipt unreadable)")
    : review && review.is_awaiting
      ? "waiting on you"
      : "not the gate under decision — awaiting " + ((review || {}).awaiting || "none");
  return (
    <div className="gatehead">
      <span className="gtitle">
        Gate {circled(g.index || (review || {}).index || 0)} · {gate || ""}
      </span>
      <span className="gstate">{where}</span>
    </div>
  );
}

// Approval is two steps on purpose: read what it would cover, then decide. The readiness fetch is
// what puts the digests on screen, and the recording POST hands those same digests back — the
// server refuses if the repository moved in between, so an approval can never bind bytes nobody
// read. The confirmation is an anti-misclick, NOT a security control: the authority is the write
// session, which exists only because someone redeemed the launch link `rein ui` printed to its own
// terminal.
//
// It is drawn in this pane rather than in an OS confirm() so the digests stay on screen while they
// are being read, and so the refusal — a gate that is not ready — lands where the person asking is
// looking instead of in the Console's output pane on another screen.
function Panel({ panel, review, unopened, onClose, onApprove, onChanges, onFreeze }) {
  const [target, setTarget] = useState(panel.kind === "changes" ? panel.suggested : "");
  const [reason, setReason] = useState("");
  const index = circled((review || {}).index || 0);
  const cancel = <button onClick={onClose}>Cancel</button>;

  if (panel.kind === "blocked") {
    return (
      <div className="confirm">
        <p className="lede">Gate {index} will not open yet.</p>
        <ul className="note">
          {panel.blockers.map((b) => <li key={b}>{b}</li>)}
        </ul>
        <div className="row">
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    );
  }

  if (panel.kind === "approve") {
    return (
      <div className="confirm">
        <p className="lede">Approving gate {index} binds these digests. The gate opens when you confirm.</p>
        <div className="scroll">
          <table>
            <tbody>
              {Object.entries(panel.covers).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td className="mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {unopened.length ? <p className="note">Not opened in this pane yet: {unopened.join(", ")}</p> : null}
        <div className="row" style={{ marginTop: ".8rem" }}>
          <button className="primary" onClick={onApprove}>
            Approve gate {index}
          </button>
          {cancel}
        </div>
      </div>
    );
  }

  if (panel.kind === "changes") {
    return (
      <div className="confirm">
        <p className="lede">Send this back with a target.</p>
        <p className="note">
          Anchoring to a place is the point, not a formality: it is what lets the fix read one slice instead of
          re-running the phase over the whole deliverable.
        </p>
        <label className="fld">
          <span>where</span>
          <input
            autoFocus
            value={target}
            placeholder="docs/10-requirements.md#R-3 · T-004 · C-001"
            onChange={(e) => setTarget(e.target.value)}
          />
        </label>
        <label className="fld">
          <span>what is wrong with it</span>
          <textarea rows="3" value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="row">
          <button className="primary" onClick={() => onChanges(target.trim(), reason.trim())}>
            Request the change
          </button>
          {cancel}
        </div>
      </div>
    );
  }

  return (
    <div className="confirm">
      <p className="lede">Freeze the human review?</p>
      <p className="note">
        It is then bound to this machine review, and regenerating the machine review resets it. This does not
        approve the gate.
      </p>
      <div className="row">
        <button className="primary" onClick={onFreeze}>
          Freeze it
        </button>
        {cancel}
      </div>
    </div>
  );
}

export default function Gate({ status, gate }) {
  const project = (status || {}).project || null;
  const [review, setReview] = useState(null);
  const [session, setSession] = useState(null);
  const [stage, setStage] = useState(null);
  const [stageData, setStageData] = useState(null);
  const [asBuilt, setAsBuilt] = useState(null);
  const [selected, setSelected] = useState(null);
  const [panel, setPanel] = useState(null);
  const [reload, setReload] = useState(0);

  const isBuild = gate === "build";

  // Two effects, and neither resets anything: `<Gate>` is keyed on the gate and the project in
  // App.jsx, so switching either remounts this component and every piece of state below starts
  // fresh. That is what makes "is this payload mine?" structural rather than a comparison the pane
  // has to remember to make — the approval footer is computed from `review`, and a human must never
  // be able to approve one gate having read another. `cancelled` is the newest-request-wins rule: a
  // response that arrives after the effect was torn down is dropped.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const payload = await getJson("/api/review/" + gate);
      if (cancelled) return;
      setReview(payload);
      if (payload.error) return;

      const items = mainEntries(payload);
      setSelected((current) => (items.some((x) => x.id === current) ? current : (items[0] || {}).id || null));
      if (gate !== "build") return;

      const s = await getJson("/api/review/session");
      if (cancelled) return;
      setSession(s);
      // Keep the stage being read across a refetch; land on the first unsettled one otherwise.
      const names = (s.stages || []).map((x) => x.name);
      setStage((current) => (names.includes(current) ? current : firstUnsettled(s.stages || [])));
    })();
    return () => {
      cancelled = true;
    };
  }, [gate, reload]);

  // The stage's own content, which moves when the stage does and when a recorded answer changes it.
  useEffect(() => {
    if (!isBuild || !stage) return undefined;
    let cancelled = false;
    getJson("/api/review/stage/" + encodeURIComponent(stage)).then((d) => {
      if (!cancelled) setStageData(d);
    });
    return () => {
      cancelled = true;
    };
  }, [isBuild, stage, reload]);

  const refetch = useCallback(() => setReload((n) => n + 1), []);

  function selectStage(name) {
    if (name === stage) return;
    setStage(name);
    setStageData(null);
    setAsBuilt(null);
  }

  async function post(action, body) {
    if (!session || !session.machine_digest) return;
    try {
      const { status: code, data } = await postJson("/api/review/" + action, {
        ...body,
        machine_digest: session.machine_digest,
      });
      if (code === 409) {
        toast("the machine review changed — reloading", "err");
        refetch();
        return;
      }
      if (data.error) {
        toast(data.error, "err");
        return;
      }
      toast("recorded", "ok");
      refetch(); // the session, the stage content and the blockers all move together
    } catch (e) {
      toast("request failed: " + e, "err");
    }
  }

  // The as-built body, fetched from the commit the review is bound to. The server refuses any path
  // the stored brief did not publish, so this cannot become a way to read the repository.
  async function showAsBuilt(path) {
    const payload = await getJson("/api/review/as-built/" + encodeURIComponent(path));
    if (payload.error) return toast(payload.error, "err");
    if (payload.too_large) {
      return toast(
        `${path} is ${payload.bytes} bytes, over the ${payload.limit} this pane shows — read it at ` +
        `${payload.commit.slice(0, 12)} instead`,
        "err"
      );
    }
    setAsBuilt(payload);
  }

  async function openApproval() {
    const ready = await getJson(`/api/gate/${encodeURIComponent(gate)}/readiness`);
    if (ready.error) return toast(ready.error, "err");
    setPanel(ready.ok ? { kind: "approve", covers: ready.covers || {} } : { kind: "blocked", blockers: ready.blockers || [] });
  }

  async function confirmApproval() {
    const covers = panel.covers;
    setPanel(null);
    // The stream reports the opened gate to the spine by itself; only this pane's own payload —
    // the deliverables and the footer — has to be asked for again.
    if (await record("/api/gate/approve", { gate, covers })) refetch();
  }

  async function submitChanges(target, reason) {
    if (!target) return toast("name the place that has to change", "err");
    if (!reason) return toast("say what is wrong with it", "err");
    setPanel(null);
    if (await record("/api/changes", { gate, target, reason })) refetch();
  }

  function selectDeliverable(id) {
    openedSet(project, gate).add(id);
    setSelected(id);
  }

  const buildMode = isBuild && session && !session.error && session.generated !== false;
  const opened = openedSet(project, gate);

  let body;
  if (!status) body = <Empty>waiting for status…</Empty>;
  else if (!review) body = <Empty>loading…</Empty>;
  else if (review.error) body = <Warn>{review.error}</Warn>;
  else {
    body = (
      <div className="rv-grid">
        <aside className="rv-list">
          {buildMode ? (
            <StageList stages={session.stages || []} stage={stage} onSelect={selectStage} />
          ) : (
            <DeliverableList review={review} selected={selected} opened={opened} onSelect={selectDeliverable} />
          )}
        </aside>
        <div className="rv-body">
          {buildMode ? (
            <StageBody
              data={stageData}
              review={review}
              session={session}
              asBuilt={asBuilt}
              onAsBuilt={showAsBuilt}
              onPost={post}
              onFreeze={() => setPanel({ kind: "freeze" })}
            />
          ) : (
            <DeliverableBody review={review} selected={selected} />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="view" id="view-gate">
      <div className="block">
        <div id="rvBar">
          <GateHead status={status} gate={gate} review={review} />
        </div>
        <div id="rvMain">{body}</div>
        <div id="rvFoot">
          {review && !review.error ? (
            <>
              <div className="approvebar">
                <Footer review={review} session={session} isBuild={isBuild} gate={gate}
                  onApprove={openApproval}
                  onChanges={() => setPanel({
                    kind: "changes",
                    suggested: (mainEntries(review).find((x) => x.id === selected) || {}).path || "",
                  })}
                />
              </div>
              {panel ? (
                <Panel
                  panel={panel}
                  review={review}
                  unopened={isBuild ? [] : mainEntries(review).filter((x) => !opened.has(x.id)).map((x) => x.label)}
                  onClose={() => setPanel(null)}
                  onApprove={confirmApproval}
                  onChanges={submitChanges}
                  onFreeze={() => {
                    setPanel(null);
                    post("complete", {});
                  }}
                />
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Footer({ review, session, isBuild, gate, onApprove, onChanges }) {
  if (READ_ONLY) {
    return (
      <span className="note">
        Read-only page. Open the launch link `rein ui` printed to decide here, or run{" "}
        <code>rein approve {gate || "<gate>"}</code> at a terminal.
      </span>
    );
  }
  if (review.status === "approved") {
    return <span className="okline">✓ gate {circled(review.index)} already open</span>;
  }
  if (!review.is_awaiting) return <span className="note">Not the gate under decision.</span>;

  const warn = review.gate === "release" && review.open_escalations
    ? <span className="warn">{review.open_escalations} open escalation(s) — resolve before the release decision</span>
    : null;

  if (isBuild && session && !session.error && session.generated !== false && !session.can_freeze) {
    return (
      <>
        {warn}
        <span className="warn">
          The human review is not frozen — {(session.completion_blockers || []).length} blocker(s).
        </span>
        <button className="primary" disabled>
          Approve gate {circled(review.index)}
        </button>
      </>
    );
  }

  return (
    <>
      {warn}
      <button className="primary" onClick={onApprove}>
        Approve gate {circled(review.index)}
      </button>{" "}
      <button onClick={onChanges}>Request changes</button>
    </>
  );
}
