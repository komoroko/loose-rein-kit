// Reviews: which reviews run, laid out along the cycle, and edited in place (CR-51).
//
// A lane per stage the cycle passes through. The drafting lanes hold the adversarial review before
// the mandate; the build lane holds the reviewer steps, each reading for the reviews on its card;
// the whole-change lane holds the reviews read over the merged tree before acceptance, switched
// like the adversarial review; the acceptance lane holds what acceptance is decided by, shown and
// never offered — comparison is not a way of improving the work, so there is nothing here to
// switch it off with.
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

function move(list, from, to) {
  if (to < 0 || to >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

function StepCard({ step, index, count, known, onChange, onMove, onRemove }) {
  const [adding, setAdding] = useState("");
  const offered = known.filter((name) => !step.reviews.includes(name));
  const set = (patch) => onChange({ ...step, ...patch });
  return (
    <div className="rcard step" data-step={step.name}>
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
              disabled={READ_ONLY || step.reviews.length === 1}
              title={step.reviews.length === 1 ? "a step reads for one review at least — remove the step instead" : "stop reading for this"}
              onClick={() => set({ reviews: step.reviews.filter((r) => r !== name) })}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {offered.length && !READ_ONLY ? (
        <div className="rcard-row">
          <select value={adding} onChange={(e) => setAdding(e.target.value)} aria-label="review to add">
            <option value="">add a review…</option>
            {offered.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <button
            disabled={!adding}
            onClick={() => {
              set({ reviews: [...step.reviews, adding] });
              setAdding("");
            }}
          >
            Add
          </button>
        </div>
      ) : null}
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
  const known = [...loaded.builtin, ...customs.map((c) => c.name)];
  const dirty = JSON.stringify(loaded.document) !== JSON.stringify(draft);
  const setSteps = (steps) => setDraft({ ...draft, steps });
  const setCustoms = (list) => {
    const next = { ...draft, custom: list };
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
    <div className="view" id="view-reviews">
      <div className="block">
        <h2>Reviews</h2>
        <p className="note">
          Which reviews run. Outside the mandate&apos;s freeze: a change here rewinds nothing, applies from the next
          reading of each task, and is recorded with your reason. Acceptance lists what each task was actually read
          for.
        </p>
        <div className="lanes">
          {loaded.adversarial_stages.map((stage) => (
            <section className="lane" key={stage} data-lane={stage}>
              <h3>{STAGE_LABEL[stage] || stage}</h3>
              <label className="rcard">
                <input
                  type="checkbox"
                  checked={!!draft.adversarial[stage]}
                  disabled={READ_ONLY}
                  onChange={(e) => setDraft({ ...draft, adversarial: { ...draft.adversarial, [stage]: e.target.checked } })}
                />{" "}
                adversarial review
                {draft.adversarial[stage] ? null : (
                  <div className="note">off — the mandate screen names this stage, with when and why</div>
                )}
              </label>
            </section>
          ))}
          <section className="lane wide" data-lane="build">
            <h3>Build</h3>
            {draft.steps.map((step, i) => (
              <StepCard
                key={step.name}
                step={step}
                index={i}
                count={draft.steps.length}
                known={known}
                onChange={(next) => setSteps(draft.steps.map((s, j) => (j === i ? next : s)))}
                onMove={(to) => setSteps(move(draft.steps, i, to))}
                onRemove={() => setSteps(draft.steps.filter((_, j) => j !== i))}
              />
            ))}
            {draft.steps.length ? null : <p className="note">No reviewer reads the code before acceptance.</p>}
            {READ_ONLY ? null : (
              <button
                onClick={() => {
                  let n = draft.steps.length + 1;
                  while (draft.steps.some((s) => s.name === "review-" + n)) n += 1;
                  setSteps([...draft.steps, { name: "review-" + n, reviews: ["adversarial"], retries: 1, stage: "both" }]);
                }}
              >
                Add a reviewer step
              </button>
            )}
          </section>
          <section className="lane" data-lane="whole-change">
            <h3>Whole change</h3>
            <label className="rcard">
              <input
                type="checkbox"
                checked={!!draft.whole_change?.security}
                disabled={READ_ONLY}
                onChange={(e) =>
                  setDraft({ ...draft, whole_change: { ...draft.whole_change, security: e.target.checked } })
                }
              />{" "}
              security review
              {draft.whole_change?.security ? null : (
                <div className="note">off — acceptance says no security reading was taken</div>
              )}
            </label>
          </section>
          <section className="lane" data-lane="acceptance">
            <h3>Acceptance</h3>
            {loaded.acceptance.map((name) => (
              <div className="rcard fixed" key={name} title="what acceptance is decided by — not configurable">
                {name}
              </div>
            ))}
          </section>
        </div>

        <h3>Custom reviews</h3>
        <p className="note">A name, and the question the reviewer reads for. It is kept in reviews.yaml itself.</p>
        <table>
          <tbody>
            {customs.map((c) => (
              <tr key={c.name}>
                <td className="mono">{c.name}</td>
                <td>{c.question}</td>
                <td>
                  <button
                    className="icon"
                    disabled={READ_ONLY || draft.steps.some((s) => s.reviews.includes(c.name))}
                    title="remove it from every step first"
                    onClick={() => setCustoms(customs.filter((x) => x.name !== c.name))}
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
              disabled={!custom.name || !custom.question.trim() || known.includes(custom.name)}
              onClick={() => {
                setCustoms([...customs, { name: custom.name, question: custom.question.trim() }]);
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
