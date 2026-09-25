// The gate reading room: read what the approval would cover, then decide — in one place.
//
// Which gate is being read is the route (`#gate/<name>`), not a selection held here: a reading room
// is somewhere you can link to and come back to. The spine is the only rendering of gate state on
// the page, so this module draws no gate list of its own.
//
// The mandate is deliverable review: a document list on the left, rendered markdown on the right.
// Acceptance is different in kind — it reviews a generated grounded review, and what it asks for is
// a judgement, so its left rail is the review stages and its body is a form at every stage.
//
// The pane used to repaint in two grains, because rebuilding the body from strings would wipe a
// form the reviewer was half way through — so a status push repainted only the heading and the
// footer, and an open footer panel suppressed its own repaint. React reconciles instead of
// replacing, so a push can no longer take anything out from under the human and all of that is
// gone: one render, from state.

import { useCallback, useEffect, useState } from "react";

import { READ_ONLY, getJson, postJson, record, toast } from "../api.js";
import { Empty, ReviewRun, Warn } from "../parts.jsx";
import { DeliverableBody, DeliverableList, mainEntries } from "./Deliverables.jsx";
import { StageBody, StageList } from "./stages.jsx";

// Opened documents are a client-side memory aid that outlives a visit to the room, so they live
// beside the module rather than in component state. Nothing in the approval path consults this: what
// an acceptance tick means instead is human_review.stage_settled, a judgement the repository can show
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
        Gate · {gate || ""}
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
// The part of the approval panel that is not a digest. Everything beside it says what was decided
// *with* this human; these say what was decided without them, and an approval ratifies them
// silently unless they are put in front of somebody.
//
// The material was never missing here — `.rein/plan.yaml` is the first deliverable in the left
// rail, and both lists are in it. What was missing is the selection: which of a few hundred lines
// deserve an eye at the moment of approving. `rein approve` prints it before its [y/N]; this route
// did not, and whatever a gate requires on screen belongs on every route that can open it.
function Naming({ naming, gate }) {
  const unasked = (naming || {}).unasked || [];
  const lenses = (naming || {}).lenses || [];
  const crossing = (naming || {}).crossing || [];
  const undeclared = (naming || {}).undeclared || [];
  const delta = (naming || {}).delta || [];
  if (!unasked.length && !lenses.length && !crossing.length && !undeclared.length && !delta.length) return null;
  const crossingTasks = [...new Set(crossing.filter((c) => !c.carried_from).map((c) => c.task_id))];
  const proposedCount = lenses.filter((l) => l.status === "proposed").length;
  return (
    <>
      {delta.length ? (
        <>
          {/* On a re-approval, the part of the plan that is new since the last yes. The approval
              still covers the plan whole — the digests say so — but this is what is being decided. */}
          <div className="subhead" style={{ marginTop: ".8rem" }}>
            What changed since you last approved this mandate ({delta.length})
          </div>
          <table>
            <tbody>
              {delta.map((d) => (
                <tr key={d.what + ":" + d.id}>
                  <td><span className="mono">{d.what} {d.id}</span></td>
                  <td>{d.change}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {crossing.length ? (
        <>
          {/* First, and not in a <details>. At the mandate this is how many more times the cycle
              will stop; at a crossing gate it is the thing about to become permanent. It is the one
              item on this screen that no later gate can reconsider. */}
          <div className="subhead" style={{ marginTop: ".8rem" }}>
            {gate === "mandate"
              ? `${crossingTasks.length} further stop(s) this mandate creates — one before each task that declares work it cannot take back and was not already approved as it stands`
              : "This approval lets the loop do something it cannot undo"}
          </div>
          <table>
            <tbody>
              {crossing.map((c) => (
                <tr key={c.task_id + ":" + c.name}>
                  <td><span className="mono">{c.task_id}</span> {c.title}</td>
                  <td>
                    <div>cannot be undone: {c.name} ({c.kind})</div>
                    <div className="note">
                      decided in: {c.adr || "(no ADR recorded — the reversibility claim is unsupported)"}
                    </div>
                    {c.carried_from ? (
                      <div className="note">
                        unchanged since {c.carried_from} approved it — carried by this approval, not a stop
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {unasked.length ? (
        <>
          <div className="subhead" style={{ marginTop: ".8rem" }}>
            {unasked.length} decision(s) the loop settled without asking you
          </div>
          <div className="scroll">
            <table>
              <tbody>
                {unasked.map((d) => (
                  <tr key={d.id}>
                    <td><span className="mono">{d.id}</span> {d.subject}</td>
                    <td>
                      <div>settled: {d.answer || "(no answer recorded)"}</div>
                      {/* Required for `local` by the schema, so the fallback can only appear under a
                          plan nothing validated — and that is the reach claim most worth reading. */}
                      <div className="note">
                        local because: {d.rationale || "(none recorded — the reach claim is unsupported)"}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note">{naming.overrule_cost}</p>
        </>
      ) : null}
      {undeclared.length ? (
        /* Folded: these are not decisions to make, they are where the plan's derived scope and
           edges have a hole — a contradiction there is found by the build instead of here. */
        <details style={{ marginTop: ".8rem" }}>
          <summary>
            {undeclared.length} acceptance criterion(s) name no path they produce or read
          </summary>
          <table>
            <tbody>
              {undeclared.map((c) => (
                <tr key={c.task_id + ":" + c.id}>
                  <td><span className="mono">{c.task_id}/{c.id}</span></td>
                  <td>{c.statement}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
      {lenses.length ? (
        /* Open when any of them is `proposed`. A list that has to be opened before anything can be
           dropped has "keep them all" as its default, which is the always-on set the class system
           replaced — re-entering through a closed disclosure instead of through an empty `when:`.
           With nothing to decide it stays folded: those are applied, and nobody is being asked. */
        <details style={{ marginTop: ".8rem" }} open={lenses.some((l) => l.status === "proposed")}>
          <summary>
            {lenses.length} review lens(es) this mandate would freeze
            {proposedCount ? ` — ${proposedCount} of them yours to keep or drop` : ""}
          </summary>
          <table>
            <tbody>
              {lenses.map((l) => (
                <tr key={l.stage + ":" + l.id}>
                  <td><span className="mono">{l.id}</span> [{l.stage}]</td>
                  <td>
                    <div>{l.attack}</div>
                    <div className="note">
                      {l.status === "proposed" ? "proposed — yours to keep or drop: " : "applies when: "}
                      {l.applies_when}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
    </>
  );
}

function Panel({ panel, review, unopened, onClose, onApprove, onChanges, onFreeze }) {
  const [target, setTarget] = useState(panel.kind === "changes" ? panel.suggested : "");
  const [reason, setReason] = useState("");
  const name = (review || {}).gate || "";
  const cancel = <button onClick={onClose}>Cancel</button>;

  if (panel.kind === "blocked") {
    return (
      <div className="confirm">
        <p className="lede">Gate {name} will not open yet.</p>
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
        <p className="lede">Approving gate {name} binds these digests. The gate opens when you confirm.</p>
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
        <Naming naming={panel.naming} gate={(review || {}).gate} />
        {unopened.length ? <p className="note">Not opened in this pane yet: {unopened.join(", ")}</p> : null}
        <div className="row" style={{ marginTop: ".8rem" }}>
          <button className="primary" onClick={onApprove}>
            Approve gate {name}
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

  // The acceptance room is the one that reads a generated review rather than a document set.
  const isBuild = gate === "acceptance";

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
      if (gate !== "acceptance") return;

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
    setPanel(ready.ok
      ? { kind: "approve", covers: ready.covers || {}, naming: ready.naming || {} }
      : { kind: "blocked", blockers: ready.blockers || [] });
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
          {/* Acceptance with no machine review falls back to the deliverable list, silently. While a
              generation is in flight that silence is a lie — the stages this room is *for* are
              being read right now. The line comes off the SSE `status` push, because the session
              payload below is fetched once per gate and never polls. */}
          {isBuild && !buildMode ? <ReviewRun run={status ? status.review_run : null} /> : null}
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
    return <span className="okline">✓ gate {review.gate} already open</span>;
  }
  if (!review.is_awaiting) return <span className="note">Not the gate under decision.</span>;

  const warn = review.gate === "acceptance" && review.open_escalations
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
          Approve gate {review.gate}
        </button>
      </>
    );
  }

  return (
    <>
      {warn}
      <button className="primary" onClick={onApprove}>
        Approve gate {review.gate}
      </button>{" "}
      <button onClick={onChanges}>Request changes</button>
    </>
  );
}
