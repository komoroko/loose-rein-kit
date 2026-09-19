// Lenses: task by lens, where each one was applied and where it was not.
//
// The counts in `rein lens --stats` say which lenses earn their place. They cannot say *where* a
// lens went, which is the question after a review: this task was read for these six things and not
// for those nineteen — why not? Every answer was already in the chain and the frozen plan; what was
// missing was a shape that put them beside each other.
//
// Built server-side from the audit chain, not from the observation store, so a cycle nobody had
// this dashboard open during still answers. **A cell says what the record says and never why.**
// Three of the states are an absence with a different provenance, and that difference is the whole
// of what is being shown; guessing between them is what the tally already refuses to do.

import { useEffect, useState } from "react";

import { getJson } from "./api.js";
import { Empty, Scroll, Warn } from "./parts.jsx";

// Applied-and-found and applied-and-found-nothing are deliberately not one colour. "Found nothing"
// is a fact about this change; a lens that keeps finding nothing is a fact about the lens, and that
// is the tally's question, over cycles, not this screen's.
//
// `n/a` gets a treatment of its own rather than the muted one the three absences share: it is not
// an absence, it is a column this row was never going to answer for, and a cell nobody can tell
// from `absent` is the fourth state the key does not explain.
const CELL_CLASS = {
  found: "cell-found",
  applied: "cell-applied",
  narrowed: "cell-off",
  declined: "cell-declined",
  unjudged: "cell-unknown",
  dropped: "cell-off",
  absent: "cell-off",
  pending: "cell-unknown",
  "n/a": "cell-na",
};

// Every row carries a cell for every column — `grid()` fills the ones a row does not answer for
// with `n/a`, so there is no gap here to paper over with a glyph the key never mentions.
function Cell({ cell, meaning }) {
  const title = meaning[cell.state] || cell.state;
  const at = cell.probability === undefined ? "" : ` (p=${cell.probability.toFixed(2)})`;
  return (
    <td className={CELL_CLASS[cell.state] || ""} title={title + at}>
      {cell.state}
      {at}
    </td>
  );
}

export default function LensView({ recordSeq }) {
  const [grid, setGrid] = useState(null);

  useEffect(() => {
    let live = true;
    getJson("/api/lenses").then((d) => {
      if (live) setGrid(d);
    });
    return () => {
      live = false;
    };
  }, [recordSeq]);

  if (!grid) return <div className="view" id="view-lenses"><div className="block"><Empty>loading…</Empty></div></div>;

  const columns = grid.columns || [];
  return (
    <div className="view" id="view-lenses">
      <div className="block">
        <h2>Lenses</h2>
        <p className="note">
          What each review was sent to look for, and what it was not. A cell reports the record, not
          a reason — <code>dropped</code>, <code>absent</code> and <code>pending</code> are three
          absences with different provenance, and nothing here guesses between them.
        </p>
        {grid.error ? (
          <Warn>{grid.error}</Warn>
        ) : !(grid.rows || []).length ? (
          <Empty>No lens in the library — nothing to place.</Empty>
        ) : (
          <>
            <Scroll>
              <table className="lens-grid" id="lensGrid">
                <tbody>
                  <tr>
                    <th>lens</th>
                    <th>stage</th>
                    {columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                  {grid.rows.map((row) => {
                    const byColumn = {};
                    (row.cells || []).forEach((cell) => {
                      byColumn[cell.column] = cell;
                    });
                    return (
                      <tr key={row.lens}>
                        <td className="mono" title={row.applies_when}>{row.lens}</td>
                        <td>{row.stage}</td>
                        {columns.map((column) => (
                          <Cell key={column} cell={byColumn[column]} meaning={grid.meaning || {}} />
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Scroll>
            <dl className="lens-key">
              {Object.keys(grid.meaning || {}).map((state) => (
                <div key={state}>
                  <dt className={CELL_CLASS[state] || ""}>{state}</dt>
                  <dd className="note">{grid.meaning[state]}</dd>
                </div>
              ))}
            </dl>
            {(grid.unplaced || []).length ? (
              <p className="note">
                {grid.unplaced.length} application(s) recorded without a stage or task:{" "}
                {grid.unplaced.join(", ")}. They count, and they are shown in every column they could
                belong to rather than placed in one.
              </p>
            ) : null}
            {/* Read where somebody is about to be invited to edit the library, not after it. */}
            <p className="note" id="lensScope">{grid.scope_note}</p>
          </>
        )}
      </div>
    </div>
  );
}
