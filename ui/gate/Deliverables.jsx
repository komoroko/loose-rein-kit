// Deliverable review (gates ①②③⑤): a document list on the left, rendered markdown on the right.
//
// The document bodies arrive pre-rendered from the server (mdlite, escape-first). They are the only
// server HTML this page inserts as HTML, and they are marked as such at both of the two sites that
// do it.

import { Empty, Warn } from "../parts.jsx";
import Diff from "./diff.jsx";

export const DIFF_ID = "__diff__";

export function mainEntries(review) {
  if (!review || review.error) return [];
  const items = [];
  if (review.diff && review.gate !== "build") {
    items.push({ id: DIFF_ID, label: "change set (git diff)", exists: !review.diff.error });
  }
  return items.concat(review.deliverables || []);
}

// Opening a document is not reading it, and this list has never claimed otherwise since it stopped
// being called a "read" set: it is a client-side memory aid, it is labelled "opened" on screen, and
// nothing in the approval path consults it.
export function DeliverableList({ review, selected, opened, onSelect }) {
  const item = (e, isContext) => (
    <button
      type="button"
      key={e.id}
      className={"rv-item" + (e.id === selected ? " active" : "") + (e.exists === false ? " missing" : "")}
      onClick={() => onSelect(e.id)}
    >
      {isContext ? null : <span className="rv-read">{opened.has(e.id) ? "○" : "·"}</span>}
      {e.label}
      {e.exists === false ? " (missing)" : ""}
    </button>
  );
  return (
    <>
      <div className="subhead">Deliverables</div>
      {mainEntries(review).map((e) => item(e, false))}
      {(review.context || []).length ? (
        <>
          <div className="subhead" style={{ marginTop: ".8rem" }}>Context</div>
          {review.context.map((e) => item(e, true))}
        </>
      ) : null}
      <div className="empty" style={{ marginTop: ".6rem" }}>
        ○ opened in this pane — a memory aid, not a record
      </div>
    </>
  );
}

// The phase agent's own account of its confidence: a claim, drawn as one.
function SelfAssessment({ sa }) {
  if (!sa) return null;
  return (
    <div className="sa claim">
      <div className="subhead">
        Self-assessment{" "}
        {sa.confidence ? (
          <span className={"conf " + sa.confidence}>{sa.confidence}</span>
        ) : (
          <span className="conf unset">unset</span>
        )}
      </div>
      {/* mdlite output: escaped at the source, in the server's renderer. */}
      <div dangerouslySetInnerHTML={{ __html: sa.html }} />
    </div>
  );
}

export function DeliverableBody({ review, selected }) {
  if (selected === DIFF_ID) return <Diff diff={review.diff} meta={review.review_meta} />;
  const entry = mainEntries(review).concat(review.context || []).find((x) => x.id === selected);
  if (!entry) return <Empty>Select a deliverable.</Empty>;
  if (entry.exists === false) {
    return <Warn>{entry.label} does not exist yet — the phase has not produced it.</Warn>;
  }
  return (
    <>
      <SelfAssessment sa={entry.self_assessment} />
      {entry.truncated ? <Warn>Truncated at 300KB — open the file for the rest.</Warn> : null}
      {/* mdlite output: escaped at the source, in the server's renderer. */}
      <div className="md" dangerouslySetInnerHTML={{ __html: entry.html }} />
      {entry.mtime ? (
        <div className="empty" style={{ marginTop: ".8rem" }}>last modified {entry.mtime}</div>
      ) : null}
    </>
  );
}
