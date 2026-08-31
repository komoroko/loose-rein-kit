// The Console: the fixed whitelist of safe operations. Watching a build and sending work backwards
// are different jobs, and neither belongs next to the approval footer — so this is not the Record
// screen and it is not the gate room.
//
// There is no speculative-work / roll-back log section here. Those logs live in the phase
// deliverables under docs/, never in the status payload, so the pane that read `status.logs` could
// only ever hide itself.

import { useState } from "react";

import { READ_ONLY, postJson, toast } from "./api.js";
import { Empty } from "./parts.jsx";

// An OS confirm() dialog puts the consequence in a box the page cannot style, cannot keep on screen
// and cannot be read back afterwards. These two commands move work backwards, so what they will do
// is written on the page, in the page's own voice, above the button that does it.
function Confirm({ question, consequence, onGo, onCancel }) {
  return (
    <div className="confirm" id="opsConfirm">
      <p className="lede">{question}</p>
      <p className="note">{consequence}</p>
      <div className="row">
        <button className="danger" autoFocus onClick={onGo}>
          Yes, do it
        </button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

const PHASES = ["requirements", "design", "tasks", "build"];

export default function ConsoleView() {
  const [out, setOut] = useState(null);
  const [phase, setPhase] = useState(PHASES[0]);
  const [reason, setReason] = useState("");
  const [slug, setSlug] = useState("");
  const [confirm, setConfirm] = useState(null);

  // A Console command. Its output IS the result, so it lands on the screen that ran it.
  async function run(action, params) {
    setOut("running…");
    try {
      const { data } = await postJson("/api/run", { action, params });
      if (data.error) {
        setOut("ERROR: " + data.error);
        toast(data.error, "err");
        return;
      }
      setOut(
        `$ ${data.argv.join(" ")}\n(exit ${data.exit_code})\n\n` +
        (data.stdout || "") +
        (data.stderr ? "\n[stderr]\n" + data.stderr : "")
      );
      toast(
        (data.exit_code === 0 ? "✓ " : `✗ exit ${data.exit_code} — `) + data.argv.join(" "),
        data.exit_code === 0 ? "ok" : "err"
      );
    } catch (e) {
      setOut("request failed: " + e);
      toast("request failed", "err");
    }
  }

  function askRevise() {
    if (!reason.trim()) {
      toast("say why you are rolling back", "err");
      return;
    }
    setConfirm({
      question: `Roll back to ${phase}?`,
      consequence:
        `Gates reset in a chain starting at ${phase}: each one goes back to pending, and the ` +
        `receipts and reviews built on top of them stop counting. Reason on the record: ${reason.trim()}`,
      go: () => run("revise", { phase, reason: reason.trim() }),
    });
  }

  function askClose() {
    if (!slug.trim()) {
      toast("name the cycle first", "err");
      return;
    }
    setConfirm({
      question: `Close this cycle as ${slug.trim()}?`,
      consequence:
        "The phase deliverables are archived under that name and every gate resets for the next cycle.",
      go: () => run("cycle_close", { slug: slug.trim() }),
    });
  }

  return (
    <div className="view" id="view-console">
      <div className="block">
        <h2>Console</h2>
        <p className="note">
          Reads, diagnostics, and the two commands that send work backwards. Opening a gate is not
          here: that is a human's typed confirmation at their own terminal.
        </p>
        <div id="ops">
          {READ_ONLY ? (
            <Empty>Running with --read-only; nothing here can be run.</Empty>
          ) : (
            <>
              <div className="subhead">Diagnostics</div>
              <div className="row">
                <button onClick={() => run("doctor", {})}>rein doctor</button>
                <button onClick={() => run("tests", {})}>make test</button>
              </div>

              <div className="subhead" style={{ marginTop: "1.4rem" }}>
                Send work backwards
              </div>
              <p className="note">
                Rewinding an approval is a human privilege and it is not reversible by the loop:
                gates reset in a chain, and every task the impact analysis flags is reclassified
                rather than discarded.
              </p>
              <div className="row">
                <select
                  id="revPhase"
                  aria-label="Roll back to phase"
                  value={phase}
                  onChange={(e) => setPhase(e.target.value)}
                >
                  {PHASES.map((p) => (
                    <option key={p}>{p}</option>
                  ))}
                </select>
                <input
                  id="revReason"
                  placeholder="why"
                  size="30"
                  aria-label="Reason for rolling back"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <button className="danger" onClick={askRevise}>
                  rein revise
                </button>
              </div>
              <div className="row" style={{ marginTop: ".6rem" }}>
                <input
                  id="closeSlug"
                  placeholder="cycle slug, e.g. payment-refactor"
                  size="30"
                  aria-label="Cycle slug"
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                />
                <button className="danger" onClick={askClose}>
                  rein cycle-close
                </button>
              </div>
              {confirm ? (
                <Confirm
                  question={confirm.question}
                  consequence={confirm.consequence}
                  onGo={() => {
                    const go = confirm.go;
                    setConfirm(null);
                    go();
                  }}
                  onCancel={() => setConfirm(null)}
                />
              ) : null}
            </>
          )}
        </div>
        {out === null ? null : (
          <pre className="out" id="out">
            {out}
          </pre>
        )}
      </div>
    </div>
  );
}
