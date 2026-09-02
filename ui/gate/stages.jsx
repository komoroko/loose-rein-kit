// Gate ④'s five stages. It reviews a generated grounded review, and what it asks for is a
// judgement, not a reading — so every stage but `scope` and `diff` ends in a form that records one.
//
// The stage order and completion are the server's (models.REVIEW_STAGE_ORDER,
// human_review.stage_settled); nothing here decides what may be shown next.

import { useState } from "react";

import { toast } from "../api.js";
import { ConfBadge, Empty, EpistemicBadge, OkLine, RiskBadge, Subhead, Table, Warn, paths, short } from "../parts.jsx";
import { ConfidencePicker, SelectField, TextField } from "./fields.jsx";
import Diff from "./diff.jsx";

// --- scope: what this review speaks for, and what it does not ------------------
//
// The first stage. An approval covers a boundary, so the boundary is stated before anything else
// rather than being reconstructible from review.yaml afterwards. It is deliberately the numbers
// only — what was actually built is the orient stage's job, and reading them in that order is what
// stops a reviewer weighing "11 files" without knowing which eleven.

function Axis({ label, value, note, cls }) {
  return (
    <div className={"axis " + (cls || "")}>
      <div className="axlabel">{label}</div>
      <div className="axval">{value}</div>
      {note ? <div className="axnote">{note}</div> : null}
    </div>
  );
}

function bytesText(n) {
  if (!n) return "0 B";
  return n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
}

const sha9 = (s) => (s ? String(s).slice(0, 9) : "—");

export function ScopeStage({ data }) {
  const s = data.scope || {};
  const cov = s.coverage || {};
  const c = s.counts || {};
  const unsupported = cov.unsupported_files || [];
  const generated = cov.generated_files || [];
  const budget = s.budget || [];
  const blown = s.scope_split_required || [];

  return (
    <>
      <div className="card">
        <div className="subhead">This review covers</div>
        <div className="axes">
          <Axis label="range" value={`${sha9(s.base)} … ${sha9(s.head)}`} />
          <Axis
            label="effective risk"
            value={s.effective_risk || "unknown"}
            cls={s.effective_risk === "critical" ? "opinion" : ""}
          />
          <Axis
            label="read"
            value={`${cov.analyzed_files} file(s) / ${cov.analyzed_hunks} hunk(s) / ${bytesText(cov.analyzed_bytes)}`}
          />
          <Axis
            label="claims"
            value={`${c.claims} · gaps ${c.gaps} · scenarios ${c.scenarios} · decision cards ${c.decision_cards} · security ${c.security_findings}`}
          />
          <Axis
            label="you will be asked"
            value={`${c.decision_cards} decision card(s), ${s.decisions_required} of them blocking`}
          />
        </div>
        {/* Staleness is only assertable when both ends are known. "Generated against — but HEAD is
            now —" is the shape of a check that did not run being printed as a check that failed. */}
        {!s.fresh && s.head && s.repo_head ? (
          <Warn>
            This review was generated against {sha9(s.head)}, but HEAD is now {sha9(s.repo_head)}. A commit made
            after the review leaves it stale — regenerate before deciding anything.
          </Warn>
        ) : !s.fresh ? (
          <Warn>
            This review records no commit to check itself against
            {s.repo_head ? "" : ", and HEAD could not be read"}. Whether it still describes the working tree is
            unknown, not confirmed.
          </Warn>
        ) : null}
      </div>

      {/* The uncovered side is a path list, never a count: nobody can act on "eleven files were
          fine", and everybody can act on being told which file was never parsed. */}
      <div className="card">
        <div className="subhead">This review does not cover</div>
        {unsupported.length ? (
          <Table head={["Path", "Why", "Detail"]}>
            {unsupported.map((u) => (
              <tr key={u.path}>
                <td className="mono">{u.path}</td>
                <td>{u.reason}</td>
                <td>{u.detail || "-"}</td>
              </tr>
            ))}
          </Table>
        ) : null}
        {generated.length ? (
          <div className="row" style={{ marginTop: ".5rem" }}>
            generated: <span className="mono">{generated.join(", ")}</span>
          </div>
        ) : null}
        {cov.coverage_status !== "sufficient" ? (
          <Warn>
            Coverage is {cov.coverage_status}. Extra behaviour is <em>undeterminable</em> for this change, not
            zero — a count of 0 is only shown by a manifest that earned it.
          </Warn>
        ) : !unsupported.length && !generated.length ? (
          <OkLine>✓ every changed file was parsed.</OkLine>
        ) : null}
      </div>

      <div className="card">
        <div className="subhead">Review budget</div>
        <Table head={["Budget", "Limit", "Actual"]}>
          {budget.map((b) => (
            <tr key={b.name}>
              <td className="mono">{b.name}</td>
              <td>{b.limit}</td>
              <td className={"mono" + (b.exceeded ? " over" : "")}>{b.actual}</td>
            </tr>
          ))}
        </Table>
        {blown.length ? (
          <Warn>
            Over budget: {blown.join(", ")}. A blown budget splits the scope; it never lengthens this screen. The
            freeze stays blocked until the scope is reduced or the limit is deliberately raised in{" "}
            <code>review_policy.budgets</code>.
          </Warn>
        ) : (
          <OkLine>✓ this change fits one review session.</OkLine>
        )}
      </div>
    </>
  );
}

// --- orient: what was built, and under what conditions -------------------------
//
// The stage that asks for nothing and exists so the decision stage can ask for less. Everything
// here was derived at generation time (brief.derive) and stored in the machine half, so it
// describes the same commit range as the claims beside it. Nothing on this screen is a sentence the
// tool wrote: ids, paths, commands and image references, plus reviewer prose reached by id.

// One declared surface. `as_built` is a link rather than a body: what a person operates is the file
// as it ends up, which no diff shows — and holding it in the review would make the document a copy
// of the repository.
function SurfaceRow({ u, onAsBuilt }) {
  const built = u.as_built || [];
  return (
    <tr>
      <td className="mono">{u.task_id}</td>
      <td>{u.kind.replace(/_/g, " ")}</td>
      <td>{u.name}</td>
      <td>{u.adr || "—"}</td>
      <td>
        {built.length
          ? built.map((a) => (
            <button className="link" key={a.path} onClick={() => onAsBuilt(a.path)}>
              {a.path}
            </button>
          ))
          : paths(u.paths)}
      </td>
    </tr>
  );
}

// The one part of the orientation that can change an approval, so it is the one part ordered by
// decision value: what nobody declared, then what was declared and never read out, then a count of
// the ones that went as foreseen. A table of expected rows is where the first two go to hide.
function RequirementsOnPeople({ section, onAsBuilt }) {
  if (!section) return null;
  const undeclared = section.undeclared || [];
  const unobserved = section.unobserved || [];
  const declared = section.as_declared;
  return (
    <>
      <Subhead spaced>What this change now requires of a person</Subhead>
      {undeclared.length ? (
        <>
          <Table head={["nobody declared this", "read out of the code", "where"]}>
            {undeclared.map((u, i) => (
              <tr key={i}>
                <td>
                  {u.category.replace(/_/g, " ")} <ConfBadge level={u.confidence} />
                </td>
                <td>{u.statement}</td>
                <td className="mono">{paths(u.paths)}</td>
              </tr>
            ))}
          </Table>
          <Warn>
            No task declared these at gate ③, so nobody decided they would be somebody's job. That is what this
            row is: not a defect, a decision that has not been made.
          </Warn>
        </>
      ) : null}
      {unobserved.length ? (
        <>
          <div className="subhead" style={{ marginTop: ".8rem" }}>Declared, nothing read out</div>
          <Table>
            {unobserved.map((u, i) => <SurfaceRow u={u} key={i} onAsBuilt={onAsBuilt} />)}
          </Table>
        </>
      ) : null}
      {declared ? (
        <>
          <div className="subhead" style={{ marginTop: ".8rem" }}>As declared</div>
          <Table>
            <tr>
              <td>foreseen at gate ③ and present</td>
              <td>{declared.count}</td>
            </tr>
          </Table>
          {(declared.entries || []).length ? (
            <details>
              <summary>show them</summary>
              <Table>
                {declared.entries.map((u, i) => <SurfaceRow u={u} key={i} onAsBuilt={onAsBuilt} />)}
              </Table>
            </details>
          ) : null}
        </>
      ) : null}
    </>
  );
}

// Three states, not two. A missing launch step and a launch step that ran nothing are different
// facts, and the shipped placeholder is the third of them: it has a command, and that command
// cannot fail.
function SmokeWarning({ ops }) {
  const consequence = "Tests can be green while packaging, the entry point or dependency resolution is broken.";
  if (!ops) {
    return (
      <Warn>
        No quality-gate step is named <span className="mono">smoke</span>, so nothing declares which step
        launches the deliverable — and nothing in this run started it. {consequence}
      </Warn>
    );
  }
  if (!(ops.command || []).length) {
    return (
      <Warn>
        The smoke step has no command: nothing in this run ever started the deliverable. {consequence}
      </Warn>
    );
  }
  if (ops.placeholder) {
    return (
      <Warn>
        The smoke step is still the placeholder (<span className="mono">{(ops.command || []).join(" ")}</span>): it
        exits zero without starting anything, so nothing in this run launched the deliverable. {consequence}
      </Warn>
    );
  }
  return null;
}

export function OrientStage({ data, review, asBuilt, onAsBuilt }) {
  const b = data.brief || {};
  const residuals = b.residuals || {};
  const verification = b.verification || {};
  const establishedForNothing = verification.established_for_nothing || [];
  const control = b.control || {};
  // The tasks whose negative control could not be taken. `discriminating` is a count because the
  // experiment answered; these are the rows because it did not, and the reason travels with each.
  const uncontrolled = [
    ...(control.no_tests_changed || []).map((t) => ({ ...t, why: "changed no test file" })),
    ...(control.undetermined || []).map((t) => ({ ...t, why: "the control could not be set up" })),
  ];
  const findings = data.residual_findings || [];

  const residualRows = ["awaiting_evidence", "blocked", "unstarted"]
    .filter((k) => residuals[k])
    .map((k) => (
      <tr key={k}>
        <td>{k.replace(/_/g, " ")}</td>
        <td className="mono">{residuals[k].join(" ")}</td>
      </tr>
    ));
  if (residuals.open_change_requests) {
    residualRows.push(
      <tr key="ocr">
        <td>open change requests</td>
        <td className="mono">{residuals.open_change_requests.join(" ")}</td>
      </tr>
    );
  }

  return (
    <>
      {b.delivered ? (
        <>
          <div className="subhead">Delivered</div>
          <Table>
            {b.delivered.map((t) => (
              <tr key={t.task_id}>
                <td className="mono">{t.task_id}</td>
                <td>{t.title || ""}</td>
                <td>
                  {t.kind || ""} <RiskBadge risk={t.risk} />
                </td>
                <td>{t.status}</td>
                <td className="mono">{(t.claim_ids || []).join(" ")}</td>
              </tr>
            ))}
          </Table>
        </>
      ) : null}

      {b.execution_boundary ? (
        <>
          <Subhead spaced>Where the quality gate ran</Subhead>
          <Table head={["step", "sandbox", "image", "network", "command"]}>
            {b.execution_boundary.map((s, i) => (
              <tr key={i}>
                <td>{s.step}</td>
                <td>{s.sandbox || "—"}</td>
                <td className="mono">{s.image || "—"}</td>
                <td>
                  {s.network === "unconfined" ? <span className="conf low">unconfined</span> : s.network || "—"}
                </td>
                <td className="mono">{(s.command || []).join(" ") || s.agent_role || "—"}</td>
              </tr>
            ))}
          </Table>
          <p className="note">
            `none` is what the executor enforced, not what the config asked for: a sandboxed step is refused at
            run time unless its network profile is none. A host step has no boundary to report, which is what
            unconfined says.
          </p>
        </>
      ) : null}

      {b.environment_drift ? (
        <>
          <Subhead spaced>The sandbox moved since gate ③</Subhead>
          <Table>
            <tr>
              <td>approved at gate ③</td>
              <td className="mono">{b.environment_drift.approved_at_gate_three}</td>
            </tr>
            <tr>
              <td>evidence produced in</td>
              <td className="mono">{b.environment_drift.evidence_produced_in}</td>
            </tr>
          </Table>
          <p className="note">
            Allowed, and not a blocker: gate ③ freezes config.yaml without its image pins, so a task that adds a
            dependency can have its sandbox rebuilt without re-approving a plan nothing changed. You are approving
            over evidence produced in the later one.
          </p>
        </>
      ) : null}

      {b.stack || b.data ? (
        <>
          <Subhead spaced>What moved underneath the code</Subhead>
          <Table>
            {(b.stack || {}).dependency_files ? (
              <tr>
                <td>dependency manifests</td>
                <td>{paths(b.stack.dependency_files)}</td>
              </tr>
            ) : null}
            {(b.stack || {}).generated_files ? (
              <tr>
                <td>generated files</td>
                <td>{paths(b.stack.generated_files)}</td>
              </tr>
            ) : null}
            {(b.data || {}).migrations ? (
              <tr>
                <td>migrations</td>
                <td>{paths(b.data.migrations)}</td>
              </tr>
            ) : null}
          </Table>
        </>
      ) : null}

      <RequirementsOnPeople section={b.requirements_on_people} onAsBuilt={onAsBuilt} />

      {b.verification || b.operations || b.control ? (
        <>
          <Subhead spaced>What the gate established</Subhead>
          <Table>
            <tr>
              <td>steps in the quality gate</td>
              <td>{verification.steps == null ? "—" : verification.steps}</td>
            </tr>
            {establishedForNothing.length ? (
              <tr>
                <td>established for no task</td>
                <td className="mono">{establishedForNothing.join(" ")}</td>
              </tr>
            ) : null}
            {control.discriminating ? (
              <tr>
                <td>greens shown to be controlled</td>
                <td>{control.discriminating}</td>
              </tr>
            ) : null}
            {uncontrolled.map((t) => (
              <tr key={t.task_id}>
                <td>green not controlled</td>
                <td>
                  <span className="mono">{t.task_id}</span> — {t.detail || t.why}
                </td>
              </tr>
            ))}
          </Table>
          {uncontrolled.length ? (
            <Warn>
              Those tasks&apos; greens were never shown to be able to go red: the gate passed over a change with
              no test of its own to fail. Not a blocker — work covered by tests that already existed is a real
              thing — but the DoD says less about them than about the rest.
            </Warn>
          ) : null}
          {establishedForNothing.length ? (
            <Warn>
              Those steps ran for nothing: every task's diff missed their paths, or the run never got that far.
            </Warn>
          ) : null}
          <SmokeWarning ops={b.operations} />
        </>
      ) : null}

      {residualRows.length ? (
        <>
          <Subhead spaced>Still open</Subhead>
          <Table>{residualRows}</Table>
        </>
      ) : null}

      {(residuals.accounts || []).length ? (
        <>
          <Subhead spaced>What the implementer said about them</Subhead>
          <Table>
            {residuals.accounts.map((a) => (
              <tr key={a.task_id}>
                <td className="mono">{a.task_id}</td>
                <td>{a.outcome || ""}</td>
                <td>{a.summary}</td>
              </tr>
            ))}
          </Table>
          <p className="claim">
            A claim by the agent that did the work, not a finding: nothing independent checked it. It is here
            because these tasks are the ones you are being asked to approve around.
          </p>
        </>
      ) : null}

      {findings.length ? (
        <>
          <Subhead spaced>Unresolved review findings</Subhead>
          {findings.map((f, i) => (
            <div className="card" key={i}>
              <div className="subhead">
                {f.task_id} <RiskBadge risk={f.severity === "must_fix" ? "high" : "low"} />
              </div>
              <p>{f.statement}</p>
              <div className="empty">
                {f.anchor || "no anchor"} · observed against{" "}
                {(f.observed_commit || "an unrecorded commit").slice(0, 9)}, not the reviewed HEAD
              </div>
            </div>
          ))}
          <p className="note">
            These were written by each task's own reviewer against that task's tree at that moment. The merged
            tree may have moved since — that is why the commit is printed beside each one rather than presented as
            an observation about this review.
          </p>
        </>
      ) : null}

      {/* The file as it ends up at the commit this review is bound to — not the diff, and not your
          working tree. */}
      {asBuilt ? (
        <>
          <Subhead spaced>
            As built — {asBuilt.path} <span className="mono">@{short(asBuilt.commit)}</span>
          </Subhead>
          <pre className="blob">{asBuilt.content || ""}</pre>
          <p className="note">
            The file as it ends up at the commit this review is bound to — not the diff, and not your working
            tree.
          </p>
        </>
      ) : null}

      {/* Always last and always present: the claims the comparator settled have no card, so a
          reviewer reading cards alone would see only what the review could not conclude.
          `ExpectedActual` says so itself when there are none, which is why there is no fallback. */}
      <Subhead spaced>Expected vs actual</Subhead>
      <ExpectedActual data={data} review={review} />
    </>
  );
}

// The three axes, side by side and never merged. models.py: there is no single `verified` field,
// because integrity is a fact, semantic support is somebody's judgement, and conformance is an
// observation — and `machine_assessed` is an AI's opinion, which must not be drawn like the others.
function ClaimAxes({ claim }) {
  const sem = claim.semantic_support || {};
  const integ = claim.integrity || {};
  const conf = claim.conformance || {};
  const opinion = sem.assessment_basis === "machine_assessed";
  return (
    <>
      <div className="axes">
        <Axis label="integrity · fact" value={integ.status || "unknown"} note={integ.code_anchor_digest ? "anchored" : ""} />
        <Axis
          label="semantic support · judgement"
          value={sem.status || "unknown"}
          note={sem.assessment_basis}
          cls={opinion ? "opinion" : ""}
        />
        <Axis label="conformance · observation" value={conf.status || "unknown"} note={(conf.scope || []).join(", ")} />
      </div>
      {opinion ? <p className="note">The middle lane is an AI&apos;s assessment, not an observation.</p> : null}
    </>
  );
}

function ExpectedActual({ data }) {
  const claims = data.claims || [];
  if (!claims.length) return <Empty>the comparison produced no claim results.</Empty>;
  const actual = {};
  (data.actual_extraction || []).forEach((a) => { actual[a.id] = a; });
  return claims.map((c) => (
    <div className="card" key={c.claim_id}>
      <div className="subhead">
        {c.claim_id} <span className={"conf " + (c.verdict === "aligned" ? "high" : "low")}>{c.verdict}</span>
      </div>
      <p>{(c.expected || {}).statement || ""}</p>
      <ClaimAxes claim={c} />
      {(c.actual_statement_ids || []).length ? (
        <>
          <div className="subhead" style={{ marginTop: ".6rem" }}>Observed</div>
          <ul className="note">
            {c.actual_statement_ids.map((id) => (
              <li key={id}>
                {id}: {(actual[id] || {}).statement || "(not in this extraction)"}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {(c.unknowns || []).length ? <Warn>Unknown: {c.unknowns.join("; ")}</Warn> : null}
    </div>
  ));
}

// --- decision ------------------------------------------------------------------

// A card's evidence — the Expected the plan states and the Actual a reviewer that never saw the
// plan read. Shown with the card, always. It used to be stripped until the reviewer had recorded an
// unprimed guess about the card, which meant the one screen asking for a judgement withheld the
// material for making it.
function Evidence({ evidence }) {
  if (!evidence || typeof evidence !== "object") return null;
  const keys = Object.keys(evidence);
  if (!keys.length) return null;
  return (
    <>
      <div className="subhead" style={{ marginTop: ".6rem" }}>Evidence</div>
      <Table>
        {keys.map((k) => (
          <tr key={k}>
            <td>{k.replace(/_/g, " ")}</td>
            <td>{typeof evidence[k] === "string" ? evidence[k] : JSON.stringify(evidence[k])}</td>
          </tr>
        ))}
      </Table>
    </>
  );
}

function DecisionCard({ card, statements, prior, onSubmit }) {
  const [choice, setChoice] = useState(prior ? prior.choice : "");
  const [confidence, setConfidence] = useState("");
  const [reason, setReason] = useState("");
  const domains = card.requires_domains || [];

  function submit() {
    if (!choice) return toast("pick an option first", "err");
    if (!confidence) return toast("say how sure you are", "err");
    onSubmit({ card_id: card.id, choice, confidence, reason });
  }

  return (
    <div className="card asks">
      <div className="subhead">
        Decision {card.id} <RiskBadge risk={card.risk} />
      </div>
      <p>{card.question}</p>
      {domains.length ? <Warn>Needs familiarity with: {domains.join(", ")}</Warn> : null}
      {prior ? (
        <OkLine>
          recorded: {prior.choice} (confidence {prior.confidence}){prior.reason ? ` — ${prior.reason}` : ""}
        </OkLine>
      ) : null}
      <Evidence evidence={card.evidence} />
      <div className="opts">
        {(card.options || []).map((o) => (
          <label className="opt" key={o.id}>
            <input
              type="radio"
              name={"dc-" + card.id}
              value={o.id}
              checked={choice === o.id}
              onChange={() => setChoice(o.id)}
            />{" "}
            <b>{o.id}.</b> {(statements[o.statement_id] || {}).text || o.statement_id}
          </label>
        ))}
      </div>
      <ConfidencePicker value={confidence} onChange={setConfidence} />
      <TextField label="why" value={reason} onChange={setReason} />
      <div className="row">
        <button className="primary" onClick={submit}>
          {prior ? "Change the decision" : "Record the decision"}
        </button>
      </div>
    </div>
  );
}

const DISPOSITIONS = [
  "revise_implementation", "revise_design", "revise_requirement",
  "run_experiment", "request_expert", "reduce_scope", "dispute_finding",
];

// A gap's disposition form lives here and nowhere else — recording what happens to a gap is a
// judgement the schema, the API and the blocker list all expect, and until this section was
// reachable the pane offered no way to make it.
function GapCard({ gap, onSubmit }) {
  const [action, setAction] = useState("");
  const [note, setNote] = useState("");
  return (
    <div className="card asks">
      <div className="subhead">
        {gap.id} {gap.kind || ""} <RiskBadge risk={gap.risk} />
        {gap.blocking === true ? <span className="conf low">blocking</span> : null}
      </div>
      <SelectField label="what happens to it" value={action} options={DISPOSITIONS} onChange={setAction} />
      <TextField label="note" value={note} onChange={setNote} />
      <div className="row">
        <button
          className="primary"
          onClick={() => (action ? onSubmit({ subject_id: gap.id, action, note }) : toast("pick what happens to it", "err"))}
        >
          Record the disposition
        </button>
      </div>
    </div>
  );
}

// Returns nothing when there is nothing: this is a sub-section, not a stage, and an "all clear"
// line under a stack of open decisions would be reassuring about the wrong thing.
function WhatRaisedThese({ data, onDisposition }) {
  const gaps = data.gaps || [];
  const extras = data.extra_behaviors || [];
  const statements = data.statements || [];
  if (!gaps.length && !extras.length && !statements.length) return null;
  return (
    <>
      <Subhead spaced>What raised these</Subhead>
      {gaps.map((g) => <GapCard gap={g} key={g.id} onSubmit={onDisposition} />)}
      {extras.map((e) => (
        <div className="card" key={e.id}>
          <div className="subhead">
            {e.id} {e.category || ""} <RiskBadge risk={e.risk} />
          </div>
          <p className="note">{e.grounded ? "Grounded in a requirement." : "No requirement asked for this."}</p>
        </div>
      ))}
      {statements.length ? (
        <>
          <Subhead spaced>Statements</Subhead>
          {statements.map((s) => (
            <div className="stmt" key={s.id}>
              <EpistemicBadge status={s.epistemic_status} /> {s.text}
            </div>
          ))}
        </>
      ) : null}
    </>
  );
}

function SecurityFindings({ data }) {
  const findings = data.security_findings || [];
  if (!findings.length) return null;
  return (
    <>
      <Subhead spaced>Security review</Subhead>
      {findings.map((f) => (
        <div className="card" key={f.id}>
          <div className="subhead">
            {f.id} {f.category || ""} <RiskBadge risk={f.severity} />
            {f.status === "resolved" ? (
              <span className="conf high">resolved</span>
            ) : f.blocking === true ? (
              <span className="conf low">blocking</span>
            ) : null}
          </div>
          <p>{f.attack_scenario || ""}</p>
          {f.status === "resolved" ? (
            <p className="note">
              The code this finding anchored to is gone at{" "}
              {short((f.resolved_at && f.resolved_at.subject_head_sha) || "")}.
            </p>
          ) : null}
          {f.recommended_fix ? <p className="note">Suggested fix: {f.recommended_fix}</p> : null}
        </div>
      ))}
    </>
  );
}

export function DecisionStage({ data, onPost }) {
  const cards = data.decision_cards || [];
  const statements = {};
  (data.statements || []).forEach((s) => { statements[s.id] = s; });
  const answered = {};
  (data.decisions || []).forEach((a) => { answered[a.card_id] = a; });

  return (
    <>
      {cards.length ? (
        cards.map((card) => (
          <DecisionCard
            key={card.id}
            card={card}
            statements={statements}
            prior={answered[card.id]}
            onSubmit={(body) => onPost("decision", body)}
          />
        ))
      ) : (
        <OkLine>✓ nothing in this review needs a decision.</OkLine>
      )}
      <WhatRaisedThese data={data} onDisposition={(body) => onPost("disposition", body)} />
      <SecurityFindings data={data} />
    </>
  );
}

// --- freeze --------------------------------------------------------------------

function ExpertiseCard({ gap, onPost }) {
  const [level, setLevel] = useState("");
  const [reason, setReason] = useState("");
  return (
    <div className="card asks">
      <div className="subhead">
        Domain {gap.domain} ({gap.level})
      </div>
      <p>
        High or critical work here outruns what you have declared. Declare familiarity, or route it to someone who
        has it — a general reviewer&apos;s risk acceptance is not enough.
      </p>
      <SelectField
        label="your familiarity"
        value={level}
        options={["familiar", "partial", "unfamiliar"]}
        onChange={setLevel}
      />
      <TextField label="reason (if requesting an expert)" value={reason} onChange={setReason} />
      <div className="row">
        <button
          className="primary"
          onClick={() => (level ? onPost("expertise", { domain: gap.domain, level }) : toast("pick a familiarity level", "err"))}
        >
          Declare
        </button>
        <button onClick={() => onPost("expert", { domain: gap.domain, subject_ids: [gap.domain], reason })}>
          Request an expert
        </button>
      </div>
    </div>
  );
}

export function FreezeStage({ data, session, onPost, onFreeze }) {
  const blockers = data.completion_blockers || session.completion_blockers || [];
  const frozen = session.human_status === "frozen";
  return (
    <>
      {(session.expertise_gaps || []).map((g) => <ExpertiseCard gap={g} key={g.domain} onPost={onPost} />)}
      {blockers.length ? (
        <Warn>
          <b>The human review cannot be frozen yet.</b>
          <ul>
            {blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        </Warn>
      ) : (
        <OkLine>✓ every blocker is clear.</OkLine>
      )}
      <div className="row" style={{ marginTop: ".8rem" }}>
        {frozen ? (
          <span className="okline">✓ the human review is {session.human_status}</span>
        ) : (
          <button className="primary" disabled={blockers.length > 0} onClick={onFreeze}>
            Freeze the human review
          </button>
        )}
      </div>
      <p className="note">
        Freezing records your review. Opening the gate is a separate act, and it is the one below.
      </p>
    </>
  );
}

// --- the rail and the body -----------------------------------------------------

// A tick means a judgement is on record (human_review.stage_settled); a dot means the stage records
// nothing, so no claim is made about it either way.
export function StageList({ stages, stage, onSelect }) {
  return (
    <>
      <div className="subhead">Review stages</div>
      {stages.map((s) => (
        <button
          type="button"
          key={s.name}
          className={"rv-item" + (s.name === stage ? " active" : "")}
          onClick={() => onSelect(s.name)}
        >
          <span className={"rv-read" + (s.settled === true ? " settled" : s.settled === false ? " open" : "")}>
            {s.settled === true ? "✓" : s.settled === false ? "◆" : "·"}
          </span>
          {s.name.replace(/_/g, " ")}
        </button>
      ))}
      <div className="empty" style={{ marginTop: ".6rem" }}>
        ✓ decided · ◆ waiting on you · · nothing to record
      </div>
    </>
  );
}

// Real stage names come from the server (models.REVIEW_STAGE_ORDER: scope, orient, decision, diff,
// freeze) — these cases must match those verbatim, or a stage silently falls through to "nothing to
// show" and its form becomes unreachable from the dashboard.
export function StageBody({ data, review, session, asBuilt, onAsBuilt, onPost, onFreeze }) {
  if (!data) return <Empty>loading…</Empty>;
  if (data.error) return <Warn>{data.error}</Warn>;
  if (data.generated === false) {
    return (
      <Warn>
        No machine review has been generated. Gate ④ approves a grounded review, not a green test run — run{" "}
        <code>rein review generate</code> first.
      </Warn>
    );
  }
  switch (data.stage) {
    case "scope":
      return <ScopeStage data={data} />;
    case "orient":
      return <OrientStage data={data} review={review} asBuilt={asBuilt} onAsBuilt={onAsBuilt} />;
    case "decision":
      return <DecisionStage data={data} onPost={onPost} />;
    case "diff":
      return <Diff diff={data.diff || {}} meta={review.review_meta} />;
    case "freeze":
      return <FreezeStage data={data} session={session} onPost={onPost} onFreeze={onFreeze} />;
    default:
      return <Empty>nothing to show.</Empty>;
  }
}
