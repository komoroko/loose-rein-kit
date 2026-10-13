// Reviews: which reviews run, laid out along the cycle, and edited in place (CR-51).
//
// A lane per stage the cycle passes through: each drafting stage, Build (each task) and
// Acceptance (the whole change). Every lane has the same two parts. What belongs to that stage
// alone is switched on and off in place — the adversarial review of a drafted document, and the
// blind extraction and the comparison acceptance is decided by. Any review is dragged from the
// palette onto any lane, where it reads what that lane reads, and taken off with ×. Nothing is
// required, and what was not read is named where it is decided on.
//
// The screen edits a draft of the whole document and applies it in one write, with a reason if one is given: the
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

function Lane({ name, switches, stage, palette, changed, dragging, onChange }) {
  const reviews = stage.reviews || [];
  // A stage's own review is switched, not added: the adversarial review of a document is the one
  // a drafting lane runs whether or not anything is added to it.
  const accepts = (review) => !switches.includes(review) && !reviews.includes(review);
  const add = (review) => onChange({ ...stage, reviews: [...reviews, review] });
  const addable = palette.filter(accepts);
  return (
    <section
      className="lane"
      data-lane={name}
      data-changed={changed ? "yes" : undefined}
      {...dropZone({ accepts, dragging, onDrop: add })}
    >
      <h3>
        {STAGE_LABEL[name] || name}
        {changed ? <span className="lane-mark"> · changed</span> : null}
      </h3>
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
      {reviews.length ? null : <p className="note">no review added</p>}
      {/* Dragging is one way in, not the only one: a keyboard, a touch screen and an editor's
          embedded browser cannot drag, and a screen only a mouse can edit is one some people
          cannot apply a change from. */}
      {READ_ONLY || !addable.length ? null : (
        <select
          className="lane-add"
          aria-label={`add a review to ${name}`}
          value=""
          onChange={(e) => e.target.value && add(e.target.value)}
        >
          <option value="">add a review…</option>
          {addable.map((review) => (
            <option key={review} value={review}>
              {review}
            </option>
          ))}
        </select>
      )}
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
  // What applying a draft would change, in the chain's own words, or why it cannot be applied —
  // kept with the draft it answers, so an answer about an older draft is never shown as this one's.
  const [answered, setAnswered] = useState(null);

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

  const asked = draft ? JSON.stringify(draft) : "";
  const dirty = Boolean(loaded && draft) && JSON.stringify(loaded.document) !== asked;
  const preview = answered && answered.draft === asked ? answered : null;

  // Asked of the server, not worked out here: `reviews_cmd.prepare` is what the write runs, so the
  // list under Apply is the list the record will hold, and a draft it would refuse is refused now.
  useEffect(() => {
    if (!dirty || READ_ONLY) return undefined;
    let live = true;
    const timer = setTimeout(() => {
      postJson("/api/reviews/preview", { document: JSON.parse(asked) }).then(
        ({ data }) =>
          live && setAnswered(data.error ? { draft: asked, error: data.error } : { draft: asked, changes: data.changes }),
        () => live && setAnswered({ draft: asked, error: "The dashboard did not answer. Is `rein ui` still running?" }),
      );
    }, 200);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [asked, dirty]);

  if (!loaded) return <Empty>loading…</Empty>;
  if (loaded.error) return <Warn>{loaded.error}</Warn>;
  if (!draft) return <Warn>This repository has no .rein/reviews.yaml — `rein sync` writes the packaged one.</Warn>;

  const customs = draft.custom || [];
  const palette = [...loaded.builtin, ...customs.map((c) => c.name)];
  const laneChanged = (name) => JSON.stringify(loaded.document[name]) !== JSON.stringify(draft[name]);
  const blocked = !preview || Boolean(preview.error);
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
    toast(data.changes.length ? `Applied: ${data.changes.join("; ")}` : "nothing changed", "ok");
    setReason("");
    setReload((n) => n + 1);
  };

  return (
    <div className="view" id="view-reviews" onDragEnd={() => setDragging(null)}>
      <div className="block">
        <h2>Reviews</h2>
        {READ_ONLY ? (
          <Warn>
            This page is read-only, so nothing here can be changed or applied. Open the dashboard through the
            launch link <code>rein ui</code> printed in its terminal; that link is what grants this browser write
            access.
          </Warn>
        ) : null}
        <p className="note">
          Which reviews run. Drag a review onto a lane, or pick one under the lane, and it reads what that lane reads;
          × takes it off. What
          belongs to one stage alone is switched in place. None is required, and what is not read is named where it
          is decided on. Outside the mandate&apos;s freeze: a change here rewinds nothing, applies from the next
          reading, and is recorded with your reason if you give one.
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
              palette={palette}
              changed={laneChanged(name)}
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

        {READ_ONLY ? null : (
          <div className="pending" data-dirty={dirty ? "yes" : undefined} aria-live="polite">
            {dirty ? (
              <>
                <div className="pending-head">Not applied yet</div>
                {preview === null ? (
                  <p className="note">checking what this changes…</p>
                ) : preview.error ? (
                  <Warn>{preview.error}</Warn>
                ) : (
                  <ul className="pending-list">
                    {preview.changes.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                )}
                <label className="pending-reason">
                  Reason, optional. It is recorded in the audit chain and shown at acceptance.
                  <input
                    aria-label="reason"
                    placeholder="why this changes which reviews run"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
                <div className="pending-actions">
                  <button className="primary" disabled={blocked} onClick={apply}>
                    Apply
                  </button>
                  <button onClick={() => setDraft(JSON.parse(JSON.stringify(loaded.document)))}>Discard</button>
                </div>
              </>
            ) : (
              <p className="note">No changes. Edit a lane or a custom review, and what it changes appears here.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
