// Reviews: which reviews run, laid out along the cycle, and edited in place (CR-51).
//
// A lane per stage the cycle passes through: each drafting stage, Build (each task) and
// Acceptance (the whole change). Every lane has the same two parts. What belongs to that stage
// alone is switched on and off in place — the adversarial review of a drafted document, and the
// blind extraction and the comparison acceptance is decided by. Any review is dragged from the
// palette onto any lane, where it reads what that lane reads, and taken off with ×. Nothing is
// required, and what was not read is named where it is decided on.
//
// The screen edits a draft of the whole document and applies it in one write, with a reason: the
// same function `rein reviews apply` calls, and the same `reviews_changed` event in the chain.
// Nothing here rewinds a gate. `reviews.yaml` is outside the mandate's freeze. The write is made
// against the digest the screen was served: a change that landed meanwhile is refused, not undone.

import { useEffect, useState } from "react";

import { READ_ONLY, getJson, postJson, toast } from "./api.js";
import { Empty, Warn } from "./parts.jsx";

const STAGE_LABEL = {
  requirements: "Requirements",
  design: "Design",
  tasks: "Tasks",
  build: "Build",
  acceptance: "Acceptance",
};
const READS = {
  requirements: "the requirements",
  design: "the design",
  tasks: "the task plan",
  build: "each task's change, before it merges",
  acceptance: "the whole change, once every task has merged",
};
const SWITCH_LABEL = {
  adversarial: "adversarial review",
  actual_extraction: "actual extraction",
  comparison: "comparison",
};
const OFF_NOTE = {
  adversarial: "off — the mandate screen names this stage, with when and why",
  actual_extraction: "off — nobody reads the code blind for what it does",
  comparison: "off — every claim reaches acceptance as a question for you",
};

function move(list, from, to) {
  if (to < 0 || to >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

// The comparison reads the extraction: switching it on takes the extraction with it, and switching
// the extraction off takes the comparison with it, so the draft never holds a pair it cannot apply.
function switched(stage, name, on) {
  const next = { ...stage, [name]: on };
  if (name === "comparison" && on) next.actual_extraction = true;
  if (name === "actual_extraction" && !on) next.comparison = false;
  return next;
}

// The name being dragged travels in the screen's own state rather than only in `dataTransfer`,
// whose contents a browser hides until the drop: a lane has to know during the drag whether it can
// take what is over it.
function dropZone({ accepts, dragging, onDrop }) {
  const ok = !READ_ONLY && dragging !== null && accepts(dragging);
  return {
    "data-accepts": ok ? "yes" : undefined,
    onDragOver: (e) => {
      if (ok) e.preventDefault();
    },
    onDrop: (e) => {
      if (!ok) return;
      e.preventDefault();
      onDrop(dragging);
    },
  };
}

function Lane({ name, switches, stage, dragging, onChange }) {
  const reviews = stage.reviews || [];
  // A stage's own review is switched, not added: the adversarial review of a document is the one
  // a drafting lane runs whether or not anything is added to it.
  const accepts = (review) => !switches.includes(review) && !reviews.includes(review);
  return (
    <section
      className="lane"
      data-lane={name}
      {...dropZone({ accepts, dragging, onDrop: (review) => onChange({ ...stage, reviews: [...reviews, review] }) })}
    >
      <h3>{STAGE_LABEL[name] || name}</h3>
      <p className="note">reads {READS[name] || name}</p>
      {switches.map((sw) => (
        <label className="rcard toggle" key={sw} data-switch={sw}>
          <input
            type="checkbox"
            role="switch"
            aria-label={SWITCH_LABEL[sw] || sw}
            checked={!!stage[sw]}
            disabled={READ_ONLY}
            onChange={(e) => onChange(switched(stage, sw, e.target.checked))}
          />{" "}
          {SWITCH_LABEL[sw] || sw}
          {stage[sw] ? null : <div className="note">{OFF_NOTE[sw]}</div>}
        </label>
      ))}
      <ul className="chips">
        {reviews.map((review, i) => (
          <li key={review} className="chip" data-review={review}>
            <button
              className="icon"
              disabled={READ_ONLY || i === 0}
              title="ask earlier"
              onClick={() => onChange({ ...stage, reviews: move(reviews, i, i - 1) })}
            >
              ‹
            </button>
            {review}
            <button
              className="icon"
              aria-label={`remove ${review} from ${name}`}
              disabled={READ_ONLY}
              title="remove"
              onClick={() => onChange({ ...stage, reviews: reviews.filter((r) => r !== review) })}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {reviews.length ? null : <p className="note">no review added — drag one here</p>}
    </section>
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
  const palette = [...loaded.builtin, ...customs.map((c) => c.name)];
  const dirty = JSON.stringify(loaded.document) !== JSON.stringify(draft);
  const removeCustom = (name) => {
    // A review taken out of the document is taken off every lane that read for it.
    const next = { ...draft };
    for (const { name: stage } of loaded.stages) {
      next[stage] = { ...draft[stage], reviews: (draft[stage].reviews || []).filter((r) => r !== name) };
    }
    const list = customs.filter((x) => x.name !== name);
    next.custom = list;
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
          Which reviews run. Drag a review onto a lane and it reads what that lane reads; × takes it off. What
          belongs to one stage alone is switched in place. None is required, and what is not read is named where it
          is decided on. Outside the mandate&apos;s freeze: a change here rewinds nothing, applies from the next
          reading, and is recorded with your reason.
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
          {loaded.stages.map(({ name, switches }) => (
            <Lane
              key={name}
              name={name}
              switches={switches}
              stage={draft[name] || { reviews: [] }}
              dragging={dragging}
              onChange={(next) => setDraft({ ...draft, [name]: next })}
            />
          ))}
        </div>

        <h3>Custom reviews</h3>
        <p className="note">
          A name, and the question the reviewer reads for. It is kept in reviews.yaml itself; deleting one takes it off
          every lane.
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
                    title="delete this review, and take it off every lane"
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
