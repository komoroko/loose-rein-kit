// Reviews: which reviews run, laid out along the cycle, and edited in place (CR-51).
//
// A lane per stage the cycle passes through. The drafting lanes hold the adversarial review before
// the mandate; the build lane holds the reviewer steps, each reading for the reviews on its card;
// the whole-change lane holds the security reading of the merged tree. A review is dragged from the
// palette onto the lane that can run it and taken off with ×. The acceptance lane holds acceptance's
// own readings, the blind extraction and the comparison it feeds, switched on and off in place:
// they are not reviews to add but what acceptance is decided by. Nothing is required, and what was
// not read is named where it is decided on.
//
// The screen edits a draft of the whole document and applies it in one write, with a reason: the
// same function `rein reviews apply` calls, and the same `reviews_changed` event in the chain.
// Nothing here rewinds a gate. `reviews.yaml` is outside the mandate's freeze. The write is made
// against the digest the screen was served: a change that landed meanwhile is refused, not undone.

import { useEffect, useState } from "react";

import { READ_ONLY, getJson, postJson, toast } from "./api.js";
import { Empty, Warn } from "./parts.jsx";

const STAGE_LABEL = { requirements: "Requirements", design: "Design", tasks: "Tasks" };
const WHEN = {
  task: "each batch, before it merges",
  integration: "every join",
  both: "each batch, and a join that resolved a conflict",
};
const OFF_NOTE = {
  adversarial: "off — the mandate screen names this stage, with when and why",
  security: "security off — acceptance says no security reading was taken",
  actual_extraction: "off — nobody reads the code blind for what it does",
  comparison: "off — every claim reaches acceptance as a question for you",
};
const READING_LABEL = { actual_extraction: "actual extraction", comparison: "comparison" };

function move(list, from, to) {
  if (to < 0 || to >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

// The name being dragged travels in the draft's own state rather than only in `dataTransfer`,
// whose contents a browser hides until the drop: a lane has to know during the drag whether it can
// take what is over it. A zone answers for its whole area: a step that refuses a review it already
// reads does not pass the drop on to the lane around it, which would make it a step of its own.
function dropZone({ accepts, dragging, onDrop }) {
  const ok = !READ_ONLY && dragging !== null && accepts(dragging);
  return {
    "data-accepts": ok ? "yes" : undefined,
    onDragOver: (e) => {
      e.stopPropagation();
      if (ok) e.preventDefault();
    },
    onDrop: (e) => {
      e.stopPropagation();
      if (!ok) return;
      e.preventDefault();
      onDrop(dragging);
    },
  };
}

function Placed({ name, onRemove }) {
  return (
    <div className="rcard placed" data-review={name}>
      <span className="grow">{name}</span>
      <button className="icon" aria-label={"remove " + name} disabled={READ_ONLY} title="remove" onClick={onRemove}>
        ×
      </button>
    </div>
  );
}

function StepCard({ step, index, count, drop, onChange, onMove, onRemove }) {
  const set = (patch) => onChange({ ...step, ...patch });
  return (
    <div className="rcard step" data-step={step.name} {...drop}>
      <div className="rcard-head">
        <span className="mono">{step.name}</span>
        <span className="grow"></span>
        <button className="icon" disabled={READ_ONLY || index === 0} onClick={() => onMove(index - 1)} title="earlier">
          ↑
        </button>
        <button
          className="icon"
          disabled={READ_ONLY || index === count - 1}
          onClick={() => onMove(index + 1)}
          title="later"
        >
          ↓
        </button>
        <button className="icon" disabled={READ_ONLY} onClick={onRemove} title="remove this step">
          ×
        </button>
      </div>
      <ul className="chips">
        {step.reviews.map((name, i) => (
          <li key={name} className="chip" data-review={name}>
            <button
              className="icon"
              disabled={READ_ONLY || i === 0}
              title="ask earlier"
              onClick={() => set({ reviews: move(step.reviews, i, i - 1) })}
            >
              ‹
            </button>
            {name}
            <button
              className="icon"
              aria-label={"stop reading for " + name}
              disabled={READ_ONLY}
              title={step.reviews.length === 1 ? "the step's last review: removing it removes the step" : "stop reading for this"}
              onClick={() =>
                step.reviews.length === 1 ? onRemove() : set({ reviews: step.reviews.filter((r) => r !== name) })
              }
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="rcard-row note">
        reads{" "}
        <select
          value={step.stage || "both"}
          disabled={READ_ONLY}
          aria-label="when this step reads"
          onChange={(e) => set({ stage: e.target.value })}
        >
          {Object.keys(WHEN).map((stage) => (
            <option key={stage} value={stage}>
              {WHEN[stage]}
            </option>
          ))}
        </select>{" "}
        · sends back up to{" "}
        <input
          type="number"
          min="0"
          max="10"
          value={step.retries ?? 1}
          disabled={READ_ONLY}
          aria-label="retries"
          onChange={(e) => set({ retries: Number(e.target.value) })}
        />{" "}
        time(s)
      </div>
    </div>
  );
}

export default function ReviewsView() {
  const [loaded, setLoaded] = useState(null);
  const [draft, setDraft] = useState(null);
  const [reason, setReason] = useState("");
  const [custom, setCustom] = useState({ name: "", question: "" });
  const [dragging, setDragging] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    getJson("/api/reviews").then((d) => {
      if (!live) return;
      setLoaded(d);
      setDraft(d.document ? JSON.parse(JSON.stringify(d.document)) : null);
    });
    return () => {
      live = false;
    };
  }, [reload]);

  if (!loaded) return <Empty>loading…</Empty>;
  if (loaded.error) return <Warn>{loaded.error}</Warn>;
  if (!draft) return <Warn>This repository has no .rein/reviews.yaml — `rein sync` writes the packaged one.</Warn>;

  const customs = draft.custom || [];
  const stepReviews = [...loaded.builtin, ...customs.map((c) => c.name)];
  const palette = [...loaded.builtin, ...customs.map((c) => c.name)];
  const whole = draft.whole_change || {};
  const acceptance = draft.acceptance || {};
  const dirty = JSON.stringify(loaded.document) !== JSON.stringify(draft);
  const setSteps = (steps) => setDraft({ ...draft, steps });
  const setWhole = (name, on) => setDraft({ ...draft, whole_change: { ...whole, [name]: on } });
  // The comparison reads the extraction: switching it on takes the extraction with it, and switching
  // the extraction off takes the comparison with it, so the draft never holds a pair it cannot apply.
  const setReading = (name, on) => {
    const next = { ...acceptance, [name]: on };
    if (name === "comparison" && on) next.actual_extraction = true;
    if (name === "actual_extraction" && !on) next.comparison = false;
    setDraft({ ...draft, acceptance: next });
  };
  const setAdversarial = (stage, on) => setDraft({ ...draft, adversarial: { ...draft.adversarial, [stage]: on } });
  const addStep = (name) => {
    let n = draft.steps.length + 1;
    while (draft.steps.some((s) => s.name === "review-" + n)) n += 1;
    setSteps([...draft.steps, { name: "review-" + n, reviews: [name], retries: 1, stage: "both" }]);
  };
  const removeCustom = (name) => {
    // A review taken out of the document is taken out of every step that read for it, and a step
    // left reading for nothing is no step.
    const steps = draft.steps
      .map((s) => ({ ...s, reviews: s.reviews.filter((r) => r !== name) }))
      .filter((s) => s.reviews.length);
    const list = customs.filter((x) => x.name !== name);
    const next = { ...draft, steps, custom: list };
    if (!list.length) delete next.custom;
    setDraft(next);
  };

  const apply = async () => {
    const { status, data } = await postJson("/api/reviews", { document: draft, reason, expect: loaded.digest });
    if (status !== 200 || data.error) {
      toast(data.error || "not applied", "err");
      // Changed by somebody else meanwhile: show what it is now rather than keep a draft of the past.
      if (status === 409) setReload((n) => n + 1);
      return;
    }
    toast(data.changes.length ? data.changes.join(" · ") : "nothing changed", "ok");
    setReason("");
    setReload((n) => n + 1);
  };

  return (
    <div className="view" id="view-reviews" onDragEnd={() => setDragging(null)}>
      <div className="block">
        <h2>Reviews</h2>
        <p className="note">
          Which reviews run. Drag a review onto the lane that runs it; × takes it off. None is required, and what is
          not read is named where it is decided on. Outside the mandate&apos;s freeze: a change here rewinds nothing,
          applies from the next reading of each task, and is recorded with your reason.
        </p>
        <ul className="chips palette" aria-label="reviews to drag">
          {palette.map((name) => (
            <li
              key={name}
              className="chip"
              data-review={name}
              draggable={!READ_ONLY}
              onDragStart={(e) => {
                e.dataTransfer?.setData("text/plain", name);
                setDragging(name);
              }}
            >
              {name}
            </li>
          ))}
        </ul>
        <div className="lanes">
          {loaded.adversarial_stages.map((stage) => (
            <section
              className="lane"
              key={stage}
              data-lane={stage}
              {...dropZone({
                accepts: (name) => name === "adversarial" && !draft.adversarial[stage],
                dragging,
                onDrop: () => setAdversarial(stage, true),
              })}
            >
              <h3>{STAGE_LABEL[stage] || stage}</h3>
              {draft.adversarial[stage] ? (
                <Placed name="adversarial" onRemove={() => setAdversarial(stage, false)} />
              ) : (
                <p className="note">{OFF_NOTE.adversarial}</p>
              )}
            </section>
          ))}
          <section
            className="lane wide"
            data-lane="build"
            {...dropZone({ accepts: (name) => stepReviews.includes(name), dragging, onDrop: addStep })}
          >
            <h3>Build</h3>
            {draft.steps.map((step, i) => (
              <StepCard
                key={step.name}
                step={step}
                index={i}
                count={draft.steps.length}
                drop={dropZone({
                  accepts: (name) => stepReviews.includes(name) && !step.reviews.includes(name),
                  dragging,
                  onDrop: (name) =>
                    setSteps(draft.steps.map((s, j) => (j === i ? { ...s, reviews: [...s.reviews, name] } : s))),
                })}
                onChange={(next) => setSteps(draft.steps.map((s, j) => (j === i ? next : s)))}
                onMove={(to) => setSteps(move(draft.steps, i, to))}
                onRemove={() => setSteps(draft.steps.filter((_, j) => j !== i))}
              />
            ))}
            <p className="note">
              {draft.steps.length ? "" : "No reviewer reads the code before acceptance. "}Dropped on a step, a review is
              read by it; dropped here, it is read by a step of its own.
            </p>
          </section>
          <section
            className="lane"
            data-lane="whole-change"
            {...dropZone({
              accepts: (name) => loaded.whole_change.includes(name) && !whole[name],
              dragging,
              onDrop: (name) => setWhole(name, true),
            })}
          >
            <h3>Whole change</h3>
            {loaded.whole_change.map((name) =>
              whole[name] ? (
                <Placed key={name} name={name} onRemove={() => setWhole(name, false)} />
              ) : (
                <p key={name} className="note">
                  {OFF_NOTE[name]}
                </p>
              ),
            )}
          </section>
          <section className="lane" data-lane="acceptance">
            <h3>Acceptance</h3>
            {loaded.acceptance.map((name) => (
              <label className="rcard toggle" key={name} data-reading={name}>
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={READING_LABEL[name] || name}
                  checked={!!acceptance[name]}
                  disabled={READ_ONLY}
                  onChange={(e) => setReading(name, e.target.checked)}
                />{" "}
                {READING_LABEL[name] || name}
                {acceptance[name] ? null : <div className="note">{OFF_NOTE[name]}</div>}
              </label>
            ))}
          </section>
        </div>

        <h3>Custom reviews</h3>
        <p className="note">
          A name, and the question the reviewer reads for. It is kept in reviews.yaml itself; removing one takes it off
          every step.
        </p>
        <table>
          <tbody>
            {customs.map((c) => (
              <tr key={c.name}>
                <td className="mono">{c.name}</td>
                <td>{c.question}</td>
                <td>
                  <button
                    className="icon"
                    aria-label={"delete " + c.name}
                    disabled={READ_ONLY}
                    title="delete this review, and take it off every step"
                    onClick={() => removeCustom(c.name)}
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {READ_ONLY ? null : (
          <div className="rcard-row">
            <input
              placeholder="name"
              aria-label="custom review name"
              value={custom.name}
              onChange={(e) => setCustom({ ...custom, name: e.target.value })}
            />
            <textarea
              className="grow"
              placeholder="Does every new query use an index?"
              aria-label="custom review question"
              value={custom.question}
              onChange={(e) => setCustom({ ...custom, question: e.target.value })}
            />
            <button
              disabled={!custom.name || !custom.question.trim() || palette.includes(custom.name)}
              onClick={() => {
                const next = { ...draft, custom: [...customs, { name: custom.name, question: custom.question.trim() }] };
                setDraft(next);
                setCustom({ name: "", question: "" });
              }}
            >
              Add
            </button>
          </div>
        )}

        <div className="approvebar">
          {READ_ONLY ? (
            <span className="note">Read-only page. Open the launch link `rein ui` printed to change this.</span>
          ) : (
            <>
              <input
                className="grow"
                placeholder="why — recorded in the audit chain, shown at acceptance"
                aria-label="reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button className="primary" disabled={!dirty || !reason.trim()} onClick={apply}>
                Apply
              </button>
              <button disabled={!dirty} onClick={() => setDraft(JSON.parse(JSON.stringify(loaded.document)))}>
                Discard
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
