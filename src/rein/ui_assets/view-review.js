// The gate reading room: read what the approval would cover, then decide — in one place.
//
// Which gate is being read is the route (`#gate/<name>`), not a selection held here: a reading room
// is somewhere you can link to and come back to. The spine is the only rendering of gate state on
// the page, so this module draws no gate list of its own.
//
// Gates ①②③⑤ are deliverable review: a document list on the left, rendered markdown on the right.
// Gate ④ is different in kind. It reviews a generated grounded review, and what it asks for is a
// judgement, not a reading, so its left rail is the review stages and its body is a form at every
// stage that records one. The stage order and completion are the server's
// (models.REVIEW_STAGE_ORDER, human_review.stage_settled); this module never decides what may be
// shown next.
//
// Deliverable HTML arrives pre-rendered from the server (mdlite, escape-first); diffs and every
// value out of review.yaml are escaped here. Nothing in this module puts unescaped input in the
// DOM, and no server-supplied id is ever interpolated into a generated event handler — ids travel
// as escaped data attributes read back by delegated listeners, the same rule as task ids in api.js.

import { READ_ONLY, circled, esc, postJson, record, route, state, toast } from "/assets/api.js";

const DIFF_ID = "__diff__";  // the synthetic "change set" entry on a non-build gate's list
let review = null;     // last /api/review payload for the routed gate
let session = null;    // last /api/review/session payload (gate ④'s human-review state)
let stage = null;      // selected review stage (gate ④ only)
let stageData = null;  // last /api/review/stage/<stage> payload
let asBuilt = null;    // last /api/review/as-built/<path> payload, shown under the orient section
let selected = null;   // selected deliverable id (non-build gates)
let panel = null;      // an open confirmation or form in the footer; suppresses footer repaints
let tabVisible = false;
let fetchSeq = 0;          // newest request wins; older responses are dropped on arrival
let reviewProject = null;  // which project `review` was fetched for (switcher invalidation)
// Which gate the landed payload answers for — tracked here rather than read off `review.gate`,
// because a refusal (an unknown gate is a 404 carrying `error`) echoes no gate at all. Deciding
// "is this payload mine?" from the echo made every poll refetch a gate that does not exist, and
// the pane flickered between the error and "loading…" forever.
let loaded = null;
const openedSets = {};     // "project:gate" -> Set of deliverable ids opened in this pane

function currentGate() { return route().gate; }
function isBuild() { return currentGate() === "build"; }

// Opening a document is not reading it, and this set has never claimed otherwise since it stopped
// being called a "read" set: it is a client-side memory aid, it is labelled "opened" on screen, and
// nothing in the approval path consults it. What a gate-④ tick means instead is
// human_review.stage_settled — a judgement the repository can still show afterwards.
function openedSet() {
  const key = ((state.data || {}).project || "") + ":" + currentGate();
  return (openedSets[key] = openedSets[key] || new Set());
}

// Every response is tagged with the request that asked for it. A gate opened while an earlier fetch
// is still in flight must not be dropped (the pane would keep showing the old gate's deliverables
// under the new gate's name — and the approval footer is computed from this payload, so the human
// could approve one gate having read another). Newest request wins; stale responses are discarded.
async function fetchReview() {
  const gate = currentGate();
  if (!gate) return;
  if (loaded !== gate) {
    review = null; loaded = null; session = null; stage = null; stageData = null;
    selected = null; asBuilt = null; panel = null;
    paint();  // say "loading…" rather than leave the previous gate's reading on screen
  }
  const seq = ++fetchSeq;
  let payload;
  try {
    const res = await fetch("/api/review/" + gate);
    payload = await res.json();
  } catch (e) { payload = { error: "request failed: " + e }; }
  if (seq !== fetchSeq) return;  // superseded by a later request
  review = payload;
  loaded = gate;
  reviewProject = (state.data || {}).project || null;
  if (!review.error) {
    const items = mainEntries();
    if (!items.some(x => x.id === selected)) selected = (items[0] || {}).id || null;
  }
  session = null;
  if (gate === "build" && !review.error) {
    await fetchSession(seq);
    if (seq !== fetchSeq) return;
    if (!stage || !stageAllowed(stage)) stage = firstUnsettledStage();
    await fetchStage(seq);
  }
  if (seq !== fetchSeq) return;
  paint();
}

async function fetchSession(seq) {
  try {
    const res = await fetch("/api/review/session");
    const payload = await res.json();
    if (seq === fetchSeq) session = payload;
  } catch (e) { if (seq === fetchSeq) session = { error: "request failed: " + e }; }
}

async function fetchStage(seq) {
  if (!stage) { stageData = null; return; }
  try {
    const res = await fetch("/api/review/stage/" + encodeURIComponent(stage));
    const payload = await res.json();
    if (seq === fetchSeq) stageData = payload;
  } catch (e) { if (seq === fetchSeq) stageData = { error: "request failed: " + e }; }
}

function stages() { return ((session || {}).stages) || []; }
function stageAllowed(name) { return stages().some(s => s.name === name); }

// Where to land the reviewer: the first stage still carrying an unrecorded judgement, else the
// first one. No stage is withheld — the whole review is readable from the moment it is generated,
// and what the reviewer owes is a decision, not a sequence.
function firstUnsettledStage() {
  const list = stages();
  const unsettled = list.find(s => s.settled === false);
  return (unsettled || list[0] || {}).name || null;
}

// --- writes -------------------------------------------------------------------

async function postReview(action, body) {
  if (!session || !session.machine_digest) return;
  try {
    const { status, data } = await postJson("/api/review/" + action,
      { ...body, machine_digest: session.machine_digest });
    if (status === 409) { toast("the machine review changed — reloading", "err"); fetchReview(); return; }
    if (data.error) { toast(data.error, "err"); return; }
    toast("recorded", "ok");
    fetchReview();  // the session, the stage content and the blockers all move together
  } catch (e) { toast("request failed: " + e, "err"); }
}

// Read one form's fields by their data-scope, so nothing has to be held in module state between
// paints and no id is ever written into a handler. A radio group needs the *checked* member —
// querySelector would otherwise return the first option and report it as the answer whatever the
// reviewer clicked, which is precisely the kind of fabricated human input this pane must not emit.
function field(scope, name) {
  const form = document.querySelector('[data-scope="' + CSS.escape(scope) + '"]');
  if (!form) return "";
  const checked = form.querySelector('input[type="radio"][data-field="' + name + '"]:checked');
  if (checked) return checked.value;
  const el = form.querySelector('[data-field="' + name + '"]');
  return el && el.type !== "radio" ? el.value : "";
}

function answerDecision(id) {
  const choice = field(id, "choice"), confidence = field(id, "confidence");
  if (!choice) { toast("pick an option first", "err"); return; }
  if (!confidence) { toast("say how sure you are", "err"); return; }
  postReview("decision", { card_id: id, choice, confidence, reason: field(id, "reason") });
}

function declareExpertise(domain) {
  const level = field("exp:" + domain, "level");
  if (!level) { toast("pick a familiarity level", "err"); return; }
  postReview("expertise", { domain, level });
}

function requestExpert(domain) {
  postReview("expert", { domain, subject_ids: [domain], reason: field("exp:" + domain, "reason") });
}

function recordDisposition(subjectId) {
  const action = field("disp:" + subjectId, "action");
  if (!action) { toast("pick what happens to it", "err"); return; }
  postReview("disposition", { subject_id: subjectId, action, note: field("disp:" + subjectId, "note") });
}

// --- selection ----------------------------------------------------------------

async function selectStage(name) {
  if (name === stage) return;
  stage = name; stageData = null; asBuilt = null;
  paint();
  const seq = fetchSeq;
  await fetchStage(seq);
  if (seq === fetchSeq) paint();
}

function selectDeliverable(id) {
  selected = id;
  openedSet().add(id);
  paint();
}

// --- the footer panels ---------------------------------------------------------
//
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

async function openApproval() {
  // The footer is drawn from `review`; refuse to act if it is not the payload for the routed gate,
  // so an approval can never be recorded against deliverables the human did not see.
  const gate = currentGate();
  if (!review || review.error || review.gate !== gate) return;
  let ready;
  try {
    ready = await (await fetch("/api/gate/" + encodeURIComponent(gate) + "/readiness")).json();
  } catch (e) { toast("could not check the gate: " + e, "err"); return; }
  if (ready.error) { toast(ready.error, "err"); return; }
  panel = ready.ok
    ? { kind: "approve", covers: ready.covers || {} }
    : { kind: "blocked", blockers: ready.blockers || [] };
  paintFooter();
}

async function confirmApproval() {
  const gate = currentGate();
  if (!panel || panel.kind !== "approve" || !review || review.gate !== gate) return;
  const covers = panel.covers;
  panel = null;
  // The stream reports the opened gate to the spine by itself; only this pane's own payload —
  // the deliverables and the footer — has to be asked for again.
  if (await record("/api/gate/approve", { gate, covers })) fetchReview(); else paintFooter();
}

function openChanges() {
  if (!review || review.error || review.gate !== currentGate()) return;
  panel = { kind: "changes", suggested: (mainEntries().find(x => x.id === selected) || {}).path || "" };
  paintFooter();
  const el = document.querySelector('[data-scope="changes"] [data-field="target"]');
  if (el) el.focus();
}

async function submitChanges() {
  const target = field("changes", "target").trim();
  const reason = field("changes", "reason").trim();
  if (!target) { toast("name the place that has to change", "err"); return; }
  if (!reason) { toast("say what is wrong with it", "err"); return; }
  panel = null;
  if (await record("/api/changes", { gate: currentGate(), target, reason })) fetchReview(); else paintFooter();
}

function openFreeze() { panel = { kind: "freeze" }; paintFooter(); }
function confirmFreeze() { panel = null; postReview("complete", {}); }
function closePanel() { panel = null; paintFooter(); }

function panelHtml() {
  if (!panel) return "";
  const cancel = '<button data-act="cancel" data-id="-">Cancel</button>';
  if (panel.kind === "blocked") {
    return '<div class="confirm"><p class="lede">Gate ' + circled(gateIndex()) +
      " will not open yet.</p><ul class=\"note\">" +
      panel.blockers.map(b => "<li>" + esc(b) + "</li>").join("") +
      '</ul><div class="row"><button data-act="cancel" data-id="-">Close</button></div></div>';
  }
  if (panel.kind === "approve") {
    const rows = Object.entries(panel.covers).map(([k, v]) =>
      '<tr><td>' + esc(k) + '</td><td class="mono">' + esc(v) + "</td></tr>").join("");
    const unopened = isBuild() ? []
      : mainEntries().filter(x => !openedSet().has(x.id)).map(x => x.label);
    return '<div class="confirm"><p class="lede">Approving gate ' + circled(gateIndex()) +
      " binds these digests. The gate opens when you confirm.</p>" +
      '<div class="scroll"><table>' + rows + "</table></div>" +
      (unopened.length
        ? '<p class="note">Not opened in this pane yet: ' + esc(unopened.join(", ")) + "</p>"
        : "") +
      '<div class="row" style="margin-top:.8rem">' +
      '<button class="primary" data-act="approve-go" data-id="-">Approve gate ' +
      circled(gateIndex()) + "</button>" + cancel + "</div></div>";
  }
  if (panel.kind === "changes") {
    return '<div class="confirm" data-scope="changes">' +
      '<p class="lede">Send this back with a target.</p>' +
      '<p class="note">Anchoring to a place is the point, not a formality: it is what lets the fix ' +
      "read one slice instead of re-running the phase over the whole deliverable.</p>" +
      '<label class="fld"><span>where</span><input data-field="target" value="' +
      esc(panel.suggested) + '" placeholder="docs/10-requirements.md#R-3 · T-004 · C-001"></label>' +
      '<label class="fld"><span>what is wrong with it</span><textarea data-field="reason" rows="3"></textarea></label>' +
      '<div class="row"><button class="primary" data-act="changes-go" data-id="-">Request the change</button>' +
      cancel + "</div></div>";
  }
  if (panel.kind === "freeze") {
    return '<div class="confirm"><p class="lede">Freeze the human review?</p>' +
      '<p class="note">It is then bound to this machine review, and regenerating the machine review ' +
      "resets it. This does not approve the gate.</p>" +
      '<div class="row"><button class="primary" data-act="freeze-go" data-id="-">Freeze it</button>' +
      cancel + "</div></div>";
  }
  return "";
}

function gateIndex() { return review && review.index ? review.index : 0; }

// --- shared rendering ---------------------------------------------------------

const RISKY = new Set(["high", "critical"]);
function riskBadge(risk) {
  return '<span class="conf ' + esc(RISKY.has(risk) ? "low" : "medium") + '">' + esc(risk || "low") + "</span>";
}

// The honest label for where a sentence came from. `machine_inferred` is an AI (or a template)
// talking, and models.py requires it not be rendered with the weight of an observation.
function epistemicBadge(status) {
  const weak = status === "machine_inferred" || status === "unknown" || status === "conflicted";
  return '<span class="epi' + (weak ? " weak" : "") + '">' + esc(status || "unknown") + "</span>";
}

function confidencePicker() {
  return '<label class="fld"><span>how sure are you?</span>' +
    '<select data-field="confidence"><option value="">choose…</option>' +
    '<option value="low">low</option><option value="medium">medium</option><option value="high">high</option>' +
    "</select></label>";
}

function selectField(name, label, options) {
  return '<label class="fld"><span>' + esc(label) + "</span>" +
    '<select data-field="' + name + '"><option value="">choose…</option>' +
    options.map(o => '<option value="' + esc(o) + '">' + esc(o) + "</option>").join("") +
    "</select></label>";
}

function textField(name, label, placeholder) {
  return '<label class="fld"><span>' + esc(label) + "</span>" +
    '<input data-field="' + name + '" type="text" placeholder="' + esc(placeholder || "") + '"></label>';
}

function submitBtn(kind, id, label) {
  return '<button class="primary" data-act="' + esc(kind) + '" data-id="' + esc(id) + '">' + esc(label) + "</button>";
}

function emptyNote(text) { return '<div class="empty">' + esc(text) + "</div>"; }

// --- gate ④ stages ------------------------------------------------------------

function decisionStage(d) {
  const cards = d.decision_cards || [];
  const text = {};
  (d.statements || []).forEach(s => { text[s.id] = s; });
  const answered = {};
  (d.decisions || []).forEach(a => { answered[a.card_id] = a; });
  const cardsHtml = cards.length
    ? cards.map(card => {
      const prior = answered[card.id];
      const opts = (card.options || []).map(o => {
        const st = text[o.statement_id] || {};
        return '<label class="opt"><input type="radio" name="dc-' + esc(card.id) + '" data-field="choice" value="' +
          esc(o.id) + '"' + (prior && prior.choice === o.id ? " checked" : "") + "> <b>" + esc(o.id) + ".</b> " +
          esc(st.text || o.statement_id) + "</label>";
      }).join("");
      const domains = (card.requires_domains || []).length
        ? '<div class="warn">Needs familiarity with: ' + esc(card.requires_domains.join(", ")) + "</div>" : "";
      const priorLine = prior
        ? '<div class="okline">recorded: ' + esc(prior.choice) + " (confidence " + esc(prior.confidence) + ")" +
          (prior.reason ? " — " + esc(prior.reason) : "") + "</div>"
        : "";
      return '<div class="card asks" data-scope="' + esc(card.id) + '">' +
        '<div class="subhead">Decision ' + esc(card.id) + " " + riskBadge(card.risk) + "</div>" +
        "<p>" + esc(card.question) + "</p>" + domains + priorLine +
        evidenceHtml(card.evidence) +
        '<div class="opts">' + opts + "</div>" +
        confidencePicker() +
        textField("reason", "why", "") +
        '<div class="row">' + submitBtn("dc", card.id, prior ? "Change the decision" : "Record the decision") +
        "</div></div>";
      }).join("")
    : '<div class="okline">✓ nothing in this review needs a decision.</div>';
  return cardsHtml + riskBriefStage(d) + securityStage(d);
}

// A card's evidence — the Expected the plan states and the Actual a reviewer that never saw the
// plan read. Shown with the card, always. It used to be stripped until the reviewer had recorded an
// unprimed guess about the card, which meant the one screen asking for a judgement withheld the
// material for making it.
function evidenceHtml(evidence) {
  if (!evidence || typeof evidence !== "object") return "";
  const rows = Object.keys(evidence).map(k => {
    const v = evidence[k];
    const text = typeof v === "string" ? v : JSON.stringify(v);
    return "<tr><td>" + esc(k.replace(/_/g, " ")) + "</td><td>" + esc(text) + "</td></tr>";
  }).join("");
  if (!rows) return "";
  return '<div class="subhead" style="margin-top:.6rem">Evidence</div><div class="scroll"><table>' +
    rows + "</table></div>";
}

// The three axes, side by side and never merged. models.py: there is no single `verified` field,
// because integrity is a fact, semantic support is somebody's judgement, and conformance is an
// observation — and `machine_assessed` is an AI's opinion, which must not be drawn like the others.
function claimAxes(claim) {
  const sem = claim.semantic_support || {}, integ = claim.integrity || {}, conf = claim.conformance || {};
  const opinion = sem.assessment_basis === "machine_assessed";
  const lane = (label, status, note, cls) =>
    '<div class="axis ' + (cls || "") + '"><div class="axlabel">' + esc(label) + "</div>" +
    '<div class="axval">' + esc(status || "unknown") + "</div>" +
    (note ? '<div class="axnote">' + esc(note) + "</div>" : "") + "</div>";
  return '<div class="axes">' +
    lane("integrity · fact", integ.status, integ.code_anchor_digest ? "anchored" : "") +
    lane("semantic support · judgement", sem.status, sem.assessment_basis, opinion ? "opinion" : "") +
    lane("conformance · observation", conf.status, (conf.scope || []).join(", ")) +
    "</div>" +
    (opinion ? '<p class="note">The middle lane is an AI\'s assessment, not an observation.</p>' : "");
}

function expectedActualStage(d) {
  const claims = d.claims || [];
  if (!claims.length) return emptyNote("the comparison produced no claim results.");
  const actual = {};
  (d.actual_extraction || []).forEach(a => { actual[a.id] = a; });
  return claims.map(c => {
    const cited = (c.actual_statement_ids || []).map(id =>
      '<li>' + esc(id) + ": " + esc((actual[id] || {}).statement || "(not in this extraction)") + "</li>").join("");
    return '<div class="card"><div class="subhead">' + esc(c.claim_id) +
      ' <span class="conf ' + (c.verdict === "aligned" ? "high" : "low") + '">' + esc(c.verdict) + "</span></div>" +
      "<p>" + esc((c.expected || {}).statement || "") + "</p>" +
      claimAxes(c) +
      (cited ? '<div class="subhead" style="margin-top:.6rem">Observed</div><ul class="note">' + cited + "</ul>" : "") +
      ((c.unknowns || []).length ? '<div class="warn">Unknown: ' + esc(c.unknowns.join("; ")) + "</div>" : "") +
      "</div>";
  }).join("");
}

const DISPOSITIONS = ["revise_implementation", "revise_design", "revise_requirement",
  "run_experiment", "request_expert", "reduce_scope", "dispute_finding"];

// The findings the cards were derived from, shown under them. A gap's disposition form lives here
// and nowhere else — recording what happens to a gap is a judgement the schema, the API and the
// blocker list all expect, and until this section was reachable the pane offered no way to make it.
// Returns "" when there is nothing: this is a sub-section, not a stage, and an "all clear" line
// under a stack of open decisions would be reassuring about the wrong thing.
function riskBriefStage(d) {
  const gaps = d.gaps || [], extras = d.extra_behaviors || [];
  if (!gaps.length && !extras.length && !(d.statements || []).length) return "";
  let html = '<div class="subhead" style="margin-top:1.2rem">What raised these</div>';
  gaps.forEach(g => {
    html += '<div class="card asks" data-scope="disp:' + esc(g.id) + '">' +
      '<div class="subhead">' + esc(g.id) + " " + esc(g.kind || "") + " " + riskBadge(g.risk) +
      (g.blocking === true ? ' <span class="conf low">blocking</span>' : "") + "</div>" +
      selectField("action", "what happens to it", DISPOSITIONS) +
      textField("note", "note", "") +
      '<div class="row">' + submitBtn("disp", g.id, "Record the disposition") + "</div></div>";
  });
  extras.forEach(e => {
    html += '<div class="card"><div class="subhead">' + esc(e.id) + " " + esc(e.category || "") + " " +
      riskBadge(e.risk) + "</div>" +
      '<p class="note">' + (e.grounded ? "Grounded in a requirement." : "No requirement asked for this.") +
      "</p></div>";
  });
  if ((d.statements || []).length) {
    html += '<div class="subhead" style="margin-top:1.2rem">Statements</div>' +
      d.statements.map(s => '<div class="stmt">' + epistemicBadge(s.epistemic_status) + " " + esc(s.text) +
        "</div>").join("");
  }
  return html;
}

function securityStage(d) {
  const findings = d.security_findings || [];
  if (!findings.length) return "";
  return '<div class="subhead" style="margin-top:1.2rem">Security review</div>' + findings.map(f =>
    '<div class="card"><div class="subhead">' + esc(f.id) + " " + esc(f.category || "") + " " +
    riskBadge(f.severity) +
    (f.status === "resolved" ? ' <span class="conf high">resolved</span>'
      : f.blocking === true ? ' <span class="conf low">blocking</span>' : "") + "</div>" +
    "<p>" + esc(f.attack_scenario || "") + "</p>" +
    (f.status === "resolved"
      ? '<p class="note">The code this finding anchored to is gone at ' +
        esc((f.resolved_at && f.resolved_at.subject_head_sha || "").slice(0, 12)) + ".</p>"
      : "") +
    (f.recommended_fix ? '<p class="note">Suggested fix: ' + esc(f.recommended_fix) + "</p>" : "") +
    "</div>").join("");
}

function expertiseBlock() {
  const gaps = session.expertise_gaps || [];
  if (!gaps.length) return "";
  return gaps.map(g =>
    '<div class="card asks" data-scope="exp:' + esc(g.domain) + '">' +
    '<div class="subhead">Domain ' + esc(g.domain) + " (" + esc(g.level) + ")</div>" +
    "<p>High or critical work here outruns what you have declared. Declare familiarity, or route it " +
    "to someone who has it — a general reviewer's risk acceptance is not enough.</p>" +
    selectField("level", "your familiarity", ["familiar", "partial", "unfamiliar"]) +
    textField("reason", "reason (if requesting an expert)", "") +
    '<div class="row">' + submitBtn("exp", g.domain, "Declare") +
    '<button data-act="expert" data-id="' + esc(g.domain) + '">Request an expert</button>' +
    "</div></div>").join("");
}

function freezeStage(d) {
  const blockers = d.completion_blockers || session.completion_blockers || [];
  let html = expertiseBlock();
  if (blockers.length) {
    html += '<div class="warn"><b>The human review cannot be frozen yet.</b><ul>' +
      blockers.map(b => "<li>" + esc(b) + "</li>").join("") + "</ul></div>";
  } else {
    html += '<div class="okline">✓ every blocker is clear.</div>';
  }
  const frozen = session.human_status === "frozen";
  html += '<div class="row" style="margin-top:.8rem">' + (frozen
    ? '<span class="okline">✓ the human review is ' + esc(session.human_status) + "</span>"
    : '<button class="primary" data-act="freeze" data-id="-"' + (blockers.length ? " disabled" : "") +
      ">Freeze the human review</button>") + "</div>";
  html += '<p class="note">Freezing records your review. Opening the gate is a separate act, and it ' +
    "is the one below.</p>";
  return html;
}

// --- orient: what was built, and under what conditions -------------------------
//
// The stage that asks for nothing and exists so the decision stage can ask for less. Everything
// here was derived at generation time (brief.derive) and stored in the machine half, so it
// describes the same commit range as the claims beside it. Nothing on this screen is a sentence
// the tool wrote: ids, paths, commands and image references, plus reviewer prose reached by id.

function briefTable(rows) {
  return '<div class="scroll"><table>' + rows.join("") + "</table></div>";
}

function pathsCell(paths) {
  return (paths || []).map(esc).join("<br>") || "—";
}

// The one part of the orientation that can change an approval, so it is the one part ordered by
// decision value: what nobody declared, then what was declared and never read out, then a count of
// the ones that went as foreseen. A table of expected rows is where the first two go to hide.
function asBuiltPanel() {
  if (!asBuilt || stage !== "orient") return "";
  return '<div class="subhead" style="margin-top:1.2rem">As built — ' + esc(asBuilt.path) +
    ' <span class="mono">@' + esc((asBuilt.commit || "").slice(0, 12)) + "</span></div>" +
    '<pre class="blob">' + esc(asBuilt.content || "") + "</pre>" +
    '<p class="note">The file as it ends up at the commit this review is bound to — not the ' +
    "diff, and not your working tree.</p>";
}

function requirementsOnPeople(section) {
  if (!section) return "";
  let html = '<div class="subhead" style="margin-top:1.2rem">What this change now requires of a person</div>';

  const undeclared = section.undeclared || [];
  if (undeclared.length) {
    html += briefTable([("<tr><th>nobody declared this</th><th>read out of the code</th><th>where</th></tr>")].concat(
      undeclared.map(u =>
        "<tr><td>" + esc(u.category.replace(/_/g, " ")) + " " + confBadge(u.confidence) +
        '</td><td>' + esc(u.statement) + '</td><td class="mono">' + pathsCell(u.paths) + "</td></tr>"))) +
      '<div class="warn">No task declared these at gate ③, so nobody decided they would be ' +
      "somebody's job. That is what this row is: not a defect, a decision that has not been made.</div>";
  }

  const unobserved = section.unobserved || [];
  if (unobserved.length) {
    html += '<div class="subhead" style="margin-top:.8rem">Declared, nothing read out</div>' +
      briefTable(unobserved.map(u => surfaceRow(u)));
  }

  const declared = section.as_declared;
  if (declared) {
    html += '<div class="subhead" style="margin-top:.8rem">As declared</div>' +
      briefTable(["<tr><td>foreseen at gate ③ and present</td><td>" + esc(declared.count) + "</td></tr>"]);
    if ((declared.entries || []).length) {
      html += "<details><summary>show them</summary>" +
        briefTable(declared.entries.map(u => surfaceRow(u))) + "</details>";
    }
  }
  return html;
}

// One declared surface. `as_built` is a link rather than a body: what a person operates is the file
// as it ends up, which no diff shows — and holding it in the review would make the document a copy
// of the repository.
function surfaceRow(u) {
  const built = (u.as_built || []).map(a =>
    '<button class="link" data-act="asbuilt" data-id="' + esc(a.path) + '">' + esc(a.path) + "</button>").join(" ");
  return '<tr><td class="mono">' + esc(u.task_id) + "</td><td>" + esc(u.kind.replace(/_/g, " ")) +
    "</td><td>" + esc(u.name) + "</td><td>" + esc(u.adr || "—") + "</td><td>" +
    (built || pathsCell(u.paths)) + "</td></tr>";
}

function confBadge(level) {
  return level ? '<span class="conf ' + esc(level) + '">' + esc(level) + "</span>" : "";
}

// The as-built body, fetched from the commit the review is bound to. The server refuses any path
// the stored brief did not publish, so this cannot become a way to read the repository.
async function showAsBuilt(path) {
  try {
    const res = await fetch("/api/review/as-built/" + encodeURIComponent(path));
    const payload = await res.json();
    if (payload.error) { toast(payload.error, "err"); return; }
    if (payload.too_large) {
      toast(path + " is " + payload.bytes + " bytes, over the " + payload.limit +
        " this pane shows — read it at " + payload.commit.slice(0, 12) + " instead", "err");
      return;
    }
    asBuilt = payload;
    paint();
  } catch (e) { toast("request failed: " + e, "err"); }
}

function orientStage(d) {
  const b = d.brief || {};
  let html = "";

  if (b.delivered) {
    html += '<div class="subhead">Delivered</div>' + briefTable(b.delivered.map(t =>
      "<tr><td class=\"mono\">" + esc(t.task_id) + "</td><td>" + esc(t.title || "") + "</td><td>" +
      esc(t.kind || "") + " " + riskBadge(t.risk) + "</td><td>" + esc(t.status) + "</td><td class=\"mono\">" +
      esc((t.claim_ids || []).join(" ")) + "</td></tr>"));
  }

  if (b.execution_boundary) {
    html += '<div class="subhead" style="margin-top:1.2rem">Where the quality gate ran</div>' +
      briefTable([("<tr><th>step</th><th>sandbox</th><th>image</th><th>network</th><th>command</th></tr>")].concat(
        b.execution_boundary.map(s =>
          "<tr><td>" + esc(s.step) + "</td><td>" + esc(s.sandbox || "—") + '</td><td class="mono">' +
          esc(s.image || "—") + "</td><td>" +
          (s.network === "unconfined" ? '<span class="conf low">unconfined</span>' : esc(s.network || "—")) +
          '</td><td class="mono">' + esc((s.command || []).join(" ") || s.agent_role || "—") +
          "</td></tr>"))) +
      '<p class="note">`none` is what the executor enforced, not what the config asked for: a ' +
      "sandboxed step is refused at run time unless its network profile is none. A host step has no " +
      "boundary to report, which is what unconfined says.</p>";
  }

  if (b.environment_drift) {
    html += '<div class="subhead" style="margin-top:1.2rem">The sandbox moved since gate ③</div>' +
      briefTable([
        '<tr><td>approved at gate ③</td><td class="mono">' + esc(b.environment_drift.approved_at_gate_three) + "</td></tr>",
        '<tr><td>evidence produced in</td><td class="mono">' + esc(b.environment_drift.evidence_produced_in) + "</td></tr>",
      ]) +
      '<p class="note">Allowed, and not a blocker: gate ③ freezes config.yaml without its ' +
      "image pins, so a task that adds a dependency can have its sandbox rebuilt without re-approving " +
      "a plan nothing changed. You are approving over evidence produced in the later one.</p>";
  }

  if (b.stack || b.data) {
    const rows = [];
    const s = b.stack || {}, dt = b.data || {};
    if (s.dependency_files) rows.push("<tr><td>dependency manifests</td><td>" + pathsCell(s.dependency_files) + "</td></tr>");
    if (s.generated_files) rows.push("<tr><td>generated files</td><td>" + pathsCell(s.generated_files) + "</td></tr>");
    if (dt.migrations) rows.push("<tr><td>migrations</td><td>" + pathsCell(dt.migrations) + "</td></tr>");
    html += '<div class="subhead" style="margin-top:1.2rem">What moved underneath the code</div>' + briefTable(rows);
  }

  html += requirementsOnPeople(b.requirements_on_people);

  if (b.verification || b.operations) {
    const v = b.verification || {}, nothing = v.established_for_nothing || [];
    const rows = [
      "<tr><td>steps in the quality gate</td><td>" + esc(v.steps == null ? "—" : v.steps) + "</td></tr>",
    ];
    if (nothing.length) {
      rows.push('<tr><td>established for no task</td><td class="mono">' + esc(nothing.join(" ")) + "</td></tr>");
    }
    html += '<div class="subhead" style="margin-top:1.2rem">What the gate established</div>' + briefTable(rows);
    if (nothing.length) {
      html += '<div class="warn">Those steps ran for nothing: every task\'s diff missed their paths, or ' +
        "the run never got that far.</div>";
    }
    // Three states, not two. A missing launch step and a launch step that ran nothing are
    // different facts, and the shipped placeholder is the third of them: it has a command, and
    // that command cannot fail.
    const ops = b.operations;
    const consequence = "Tests can be green while packaging, the entry point or dependency " +
      "resolution is broken.";
    if (!ops) {
      html += '<div class="warn">No quality-gate step is named <span class="mono">smoke</span>, so ' +
        "nothing declares which step launches the deliverable — and nothing in this run started it. " +
        consequence + "</div>";
    } else if (!(ops.command || []).length) {
      html += '<div class="warn">The smoke step has no command: nothing in this run ever started the ' +
        "deliverable. " + consequence + "</div>";
    } else if (ops.placeholder) {
      html += '<div class="warn">The smoke step is still the placeholder (<span class="mono">' +
        esc((ops.command || []).join(" ")) + "</span>): it exits zero without starting anything, so " +
        "nothing in this run launched the deliverable. " + consequence + "</div>";
    }
  }

  const r = b.residuals || {};
  const residualRows = [];
  ["awaiting_evidence", "blocked", "unstarted"].forEach(k => {
    if (r[k]) residualRows.push("<tr><td>" + esc(k.replace(/_/g, " ")) + '</td><td class="mono">' +
      esc(r[k].join(" ")) + "</td></tr>");
  });
  if (r.open_change_requests) residualRows.push('<tr><td>open change requests</td><td class="mono">' +
    esc(r.open_change_requests.join(" ")) + "</td></tr>");
  if (residualRows.length) {
    html += '<div class="subhead" style="margin-top:1.2rem">Still open</div>' + briefTable(residualRows);
  }

  if ((r.accounts || []).length) {
    html += '<div class="subhead" style="margin-top:1.2rem">What the implementer said about them</div>' +
      briefTable(r.accounts.map(a =>
        '<tr><td class="mono">' + esc(a.task_id) + "</td><td>" + esc(a.outcome || "") + "</td><td>" +
        esc(a.summary) + "</td></tr>")) +
      '<p class="claim">A claim by the agent that did the work, not a finding: nothing independent ' +
      "checked it. It is here because these tasks are the ones you are being asked to approve around.</p>";
  }

  const findings = d.residual_findings || [];
  if (findings.length) {
    html += '<div class="subhead" style="margin-top:1.2rem">Unresolved review findings</div>' +
      findings.map(f =>
        '<div class="card"><div class="subhead">' + esc(f.task_id) + " " + riskBadge(
          f.severity === "must_fix" ? "high" : "low") + "</div>" +
        "<p>" + esc(f.statement) + "</p>" +
        '<div class="empty">' + esc(f.anchor || "no anchor") + " · observed against " +
        esc((f.observed_commit || "an unrecorded commit").slice(0, 9)) + ", not the reviewed HEAD</div></div>").join("") +
      '<p class="note">These were written by each task\'s own reviewer against that task\'s tree at ' +
      "that moment. The merged tree may have moved since — that is why the commit is printed beside " +
      "each one rather than presented as an observation about this review.</p>";
  }

  // Always last and always present: the claims the comparator settled have no card, so a reviewer
  // reading cards alone would see only what the review could not conclude. `expectedActualStage`
  // says so itself when there are none, which is why there is no fallback around this.
  return html + asBuiltPanel() +
    '<div class="subhead" style="margin-top:1.2rem">Expected vs actual</div>' + expectedActualStage(d);
}

// --- scope: what this review speaks for, and what it does not ------------------
//
// The first stage. An approval covers a boundary, so the boundary is stated before anything else
// rather than being reconstructible from review.yaml afterwards. It is deliberately the numbers
// only — what was actually built is the orient stage's job, and reading them in that order is what
// stops a reviewer weighing "11 files" without knowing which eleven.

function kv(label, value, cls) {
  return '<div class="axis' + (cls ? " " + cls : "") + '"><div class="axlabel">' + esc(label) +
    '</div><div class="axval">' + esc(value) + "</div></div>";
}

function bytesText(n) {
  if (!n) return "0 B";
  return n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
}

function scopeStage(d) {
  const s = d.scope || {};
  const cov = s.coverage || {};
  const c = s.counts || {};
  const short = sha => (sha ? String(sha).slice(0, 9) : "—");

  let html = '<div class="card"><div class="subhead">This review covers</div><div class="axes">' +
    kv("range", short(s.base) + " … " + short(s.head)) +
    kv("effective risk", s.effective_risk || "unknown", s.effective_risk === "critical" ? "opinion" : "") +
    kv("read", cov.analyzed_files + " file(s) / " + cov.analyzed_hunks + " hunk(s) / " +
      bytesText(cov.analyzed_bytes)) +
    kv("claims", c.claims + " · gaps " + c.gaps + " · scenarios " + c.scenarios +
      " · decision cards " + c.decision_cards + " · security " + c.security_findings) +
    kv("you will be asked", c.decision_cards + " decision card(s), " + s.decisions_required +
      " of them blocking") +
    "</div>";
  // Staleness is only assertable when both ends are known. "Generated against — but HEAD is now —"
  // is the shape of a check that did not run being printed as a check that failed.
  if (!s.fresh && s.head && s.repo_head) {
    html += '<div class="warn">This review was generated against ' + esc(short(s.head)) +
      ", but HEAD is now " + esc(short(s.repo_head)) +
      ". A commit made after the review leaves it stale — regenerate before deciding anything.</div>";
  } else if (!s.fresh) {
    html += '<div class="warn">This review records no commit to check itself against' +
      (s.repo_head ? "" : ", and HEAD could not be read") +
      ". Whether it still describes the working tree is unknown, not confirmed.</div>";
  }
  html += "</div>";

  // The uncovered side is a path list, never a count: nobody can act on "eleven files were fine",
  // and everybody can act on being told which file was never parsed.
  html += '<div class="card"><div class="subhead">This review does not cover</div>';
  const un = cov.unsupported_files || [];
  if (un.length) {
    html += '<div class="scroll"><table><tr><th>Path</th><th>Why</th><th>Detail</th></tr>' +
      un.map(u => '<tr><td class="mono">' + esc(u.path) + "</td><td>" + esc(u.reason) + "</td><td>" +
        esc(u.detail || "-") + "</td></tr>").join("") + "</table></div>";
  }
  if ((cov.generated_files || []).length) {
    html += '<div class="row" style="margin-top:.5rem">generated: <span class="mono">' +
      esc((cov.generated_files || []).join(", ")) + "</span></div>";
  }
  if (cov.coverage_status !== "sufficient") {
    html += '<div class="warn">Coverage is ' + esc(cov.coverage_status) +
      ". Extra behaviour is <em>undeterminable</em> for this change, not zero — a count of 0 is only " +
      "shown by a manifest that earned it.</div>";
  } else if (!un.length && !(cov.generated_files || []).length) {
    html += '<div class="okline">✓ every changed file was parsed.</div>';
  }
  html += "</div>";

  const budget = s.budget || [];
  const blown = s.scope_split_required || [];
  html += '<div class="card"><div class="subhead">Review budget</div>' +
    '<div class="scroll"><table><tr><th>Budget</th><th>Limit</th><th>Actual</th></tr>' +
    budget.map(b => '<tr><td class="mono">' + esc(b.name) + "</td><td>" + esc(b.limit) +
      '</td><td class="mono' + (b.exceeded ? " over" : "") + '">' + esc(b.actual) + "</td></tr>").join("") +
    "</table></div>" +
    (blown.length
      ? '<div class="warn">Over budget: ' + esc(blown.join(", ")) +
        ". A blown budget splits the scope; it never lengthens this screen. The freeze stays blocked " +
        "until the scope is reduced or the limit is deliberately raised in <code>review_policy.budgets</code>.</div>"
      : '<div class="okline">✓ this change fits one review session.</div>') +
    "</div>";
  return html;
}

function stageBody() {
  const d = stageData;
  if (!d) return emptyNote("loading…");
  if (d.error) return '<div class="warn">' + esc(d.error) + "</div>";
  if (d.generated === false) {
    return '<div class="warn">No machine review has been generated. Gate ④ approves a grounded review, ' +
      "not a green test run — run <code>rein review generate</code> first.</div>";
  }
  // Real stage names come from the server (models.REVIEW_STAGE_ORDER: scope, orient, decision,
  // diff, freeze) — these case labels must match those verbatim, or a stage silently falls through
  // to the default "nothing to show" and its form becomes unreachable from the dashboard.
  switch (d.stage) {
    case "scope": return scopeStage(d);
    case "orient": return orientStage(d);
    case "decision": return decisionStage(d);
    case "diff": return diffHtml((d.diff || {}), review.review_meta);
    case "freeze": return freezeStage(d);
    default: return emptyNote("nothing to show.");
  }
}

// The stage rail. A tick means a judgement is on record (human_review.stage_settled); a dot means
// the stage records nothing, so no claim is made about it either way.
function stageListHtml() {
  return '<div class="subhead">Review stages</div>' + stages().map(s => {
    const mark = s.settled === true ? '<span class="rv-read settled">✓</span>'
      : s.settled === false ? '<span class="rv-read open">◆</span>'
      : '<span class="rv-read">·</span>';
    return '<button type="button" class="rv-item' + (s.name === stage ? " active" : "") +
      '" data-stage="' + esc(s.name) + '">' + mark + esc(s.name.replace(/_/g, " ")) + "</button>";
  }).join("") +
    '<div class="empty" style="margin-top:.6rem">✓ decided · ◆ waiting on you · · nothing to record</div>';
}

// --- deliverable review (gates ①②③⑤) ------------------------------------------

function mainEntries() {
  if (!review || review.error) return [];
  const items = [];
  if (review.diff && !isBuild())
    items.push({ id: DIFF_ID, label: "change set (git diff)", exists: !review.diff.error });
  return items.concat(review.deliverables || []);
}

function listHtml() {
  const opened = openedSet();
  const item = (e, isCtx) =>
    '<button type="button" class="rv-item' + (e.id === selected ? " active" : "") +
    (e.exists === false ? " missing" : "") + '" data-deliverable="' + esc(e.id) + '">' +
    (isCtx ? "" : '<span class="rv-read">' + (opened.has(e.id) ? "○" : "·") + "</span>") +
    esc(e.label) + (e.exists === false ? " (missing)" : "") + "</button>";
  let html = '<div class="subhead">Deliverables</div>' + mainEntries().map(e => item(e, false)).join("");
  if ((review.context || []).length)
    html += '<div class="subhead" style="margin-top:.8rem">Context</div>' +
      review.context.map(e => item(e, true)).join("");
  html += '<div class="empty" style="margin-top:.6rem">○ opened in this pane — a memory aid, not a record</div>';
  return html;
}

function saHtml(sa) {
  if (!sa) return "";
  const conf = sa.confidence
    ? '<span class="conf ' + esc(sa.confidence) + '">' + esc(sa.confidence) + "</span>"
    : '<span class="conf unset">unset</span>';
  // The phase agent's own account of its confidence: a claim, drawn as one.
  return '<div class="sa claim"><div class="subhead">Self-assessment ' + conf + "</div>" + sa.html + "</div>";
}

function diffHtml(diff, meta) {
  if (diff.error) return '<div class="warn">' + esc(diff.error) + "</div>";
  let badge = "";
  if (meta) {
    badge = meta.fresh
      ? '<div class="okline">✓ the machine review is bound to this HEAD (' + esc((meta.head || "").slice(0, 12)) + ")</div>"
      : '<div class="warn">The machine review is missing or stale (reviewed: ' +
        esc(meta.reviewed_head ? meta.reviewed_head.slice(0, 12) : "none") + ", HEAD: " +
        esc((meta.head || "").slice(0, 12)) + ") — regenerate it before approving.</div>";
  }
  if (diff.log)
    return badge + '<p class="note">' + esc(diff.note || "") + '</p><pre class="patch">' +
      diff.log.map(esc).join("\n") + "</pre>";
  const files = (diff.name_status || []).map(r =>
    '<tr><td class="mono">' + esc(r[0]) + '</td><td class="mono">' + esc(r[1]) + "</td></tr>").join("");
  const patch = (diff.patch || "").split("\n").map(line => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "file"
      : line.startsWith("@@") ? "hunk"
      : line.startsWith("+") ? "add"
      : line.startsWith("-") ? "del" : "";
    return '<span class="dl ' + cls + '">' + esc(line) + "</span>";
  }).join("");  // .dl spans are display:block — a "\n" separator would double the line height
  return badge +
    '<div class="subhead">Files (base ' + esc((diff.base || "").slice(0, 12)) + " on " + esc(diff.base_ref || "") +
    ')</div><div class="scroll"><table>' + files + "</table></div>" +
    (diff.truncated ? '<div class="warn">Patch truncated at 200KB — read the rest in your editor.</div>' : "") +
    '<div class="subhead" style="margin-top:.8rem">Patch</div><pre class="patch">' + patch + "</pre>";
}

function deliverableBody() {
  if (selected === DIFF_ID) return diffHtml(review.diff, review.review_meta);
  const e = mainEntries().concat(review.context || []).find(x => x.id === selected);
  if (!e) return emptyNote("Select a deliverable.");
  if (e.exists === false)
    return '<div class="warn">' + esc(e.label) + " does not exist yet — the phase has not produced it.</div>";
  return saHtml(e.self_assessment) +
    (e.truncated ? '<div class="warn">Truncated at 300KB — open the file for the rest.</div>' : "") +
    '<div class="md">' + e.html + "</div>" +
    (e.mtime ? '<div class="empty" style="margin-top:.8rem">last modified ' + esc(e.mtime) + "</div>" : "");
}

// --- chrome -------------------------------------------------------------------

// The gate's identity line. The spine says which gate waits on you; this says what you are reading
// and, when it is already open, which recorded approval opened it.
function headHtml() {
  const gate = currentGate();
  const g = ((state.data || {}).gates || []).find(x => x.name === gate) || {};
  const where = g.status === "approved"
    ? "opened by approval " + (g.approval_id || "(receipt unreadable)")
    : (review && review.is_awaiting)
      ? "waiting on you"
      : "not the gate under decision — awaiting " + ((review || {}).awaiting || "none");
  return '<div class="gatehead"><span class="gtitle">Gate ' + circled(g.index || gateIndex()) +
    " · " + esc(gate || "") + '</span><span class="gstate">' + esc(where) + "</span></div>";
}

function footerHtml() {
  if (READ_ONLY)
    return '<span class="note">Read-only page. Open the launch link `rein ui` printed to decide here, ' +
      "or run <code>rein approve " + esc(currentGate() || "&lt;gate&gt;") + "</code> at a terminal.</span>";
  if (review.status === "approved") return '<span class="okline">✓ gate ' + circled(review.index) + " already open</span>";
  if (!review.is_awaiting)
    return '<span class="note">Not the gate under decision.</span>';
  let warn = "";
  if (review.gate === "release" && review.open_escalations)
    warn = '<span class="warn">' + review.open_escalations +
      " open escalation(s) — resolve before the release decision</span>";
  if (isBuild() && session && !session.error && session.generated !== false && !session.can_freeze) {
    const n = (session.completion_blockers || []).length;
    return warn + '<span class="warn">The human review is not frozen — ' + n +
      " blocker(s).</span><button class=\"primary\" disabled>Approve gate " + circled(review.index) + "</button>";
  }
  return warn + '<button class="primary" data-act="approve" data-id="-">Approve gate ' + circled(review.index) +
    '</button> <button data-act="changes" data-id="-">Request changes</button>';
}

// The pane is repainted in two grains. The heading and the approval footer are cheap and track the
// status poll, since the awaiting/approved marks move underneath the human. `paint` additionally
// rebuilds the body, which holds a form the reviewer may be half way through — so that happens only
// when the content itself can have changed: a fetch landing, a stage switch, or a recorded answer.
// An open footer panel suppresses its own repaint for the same reason.
function paintFooter() {
  const foot = document.getElementById("rvFoot");
  foot.innerHTML = (review && !review.error && loaded === currentGate())
    ? '<div class="approvebar">' + footerHtml() + "</div>" + panelHtml() : "";
}

function paintChrome() {
  document.getElementById("rvBar").innerHTML = state.data ? headHtml() : "";
  if (!panel) paintFooter();
}

function paint() {
  paintChrome();
  const main = document.getElementById("rvMain");
  if (!state.data) { main.innerHTML = emptyNote("waiting for status…"); return; }
  if (!review || loaded !== currentGate()) { main.innerHTML = emptyNote("loading…"); return; }
  if (review.error) { main.innerHTML = '<div class="warn">' + esc(review.error) + "</div>"; return; }
  const buildMode = isBuild() && session && !session.error && session.generated !== false;
  main.innerHTML =
    '<div class="rv-grid"><aside class="rv-list">' + (buildMode ? stageListHtml() : listHtml()) + "</aside>" +
    '<div class="rv-body">' + (buildMode ? stageBody() : deliverableBody()) + "</div></div>";
}

// Status polls repaint only the chrome; content refetches only on entering the room, changing
// gate, or after a write — never on the poll, so a form survives.
export function renderReview() {
  if (!tabVisible) return;
  const gate = currentGate();
  if (!gate) return;
  if (loaded !== gate || reviewProject !== ((state.data || {}).project || null)) {
    fetchReview();
    return;
  }
  paintChrome();
}

document.addEventListener("rein:view", e => {
  tabVisible = e.detail.view === "gate";
  if (tabVisible) fetchReview();
});

// One delegated listener for the whole pane. Every id travels as an escaped data attribute and is
// read back with getAttribute — no server-supplied string ever becomes code on the page that holds
// the approval token.
const ACTIONS = {
  dc: answerDecision,
  disp: recordDisposition,
  exp: declareExpertise,
  expert: requestExpert,
  freeze: openFreeze,
  "freeze-go": confirmFreeze,
  approve: openApproval,
  "approve-go": confirmApproval,
  changes: openChanges,
  "changes-go": submitChanges,
  cancel: closePanel,
  asbuilt: showAsBuilt,
};

document.addEventListener("click", e => {
  const close = sel => e.target.closest && e.target.closest(sel);
  const st = close("[data-stage]");
  if (st) { selectStage(st.getAttribute("data-stage")); return; }
  const del = close("[data-deliverable]");
  if (del) { selectDeliverable(del.getAttribute("data-deliverable")); return; }
  const act = close("[data-act]");
  if (act && !act.disabled) {
    const fn = ACTIONS[act.getAttribute("data-act")];
    if (fn) fn(act.getAttribute("data-id"));
  }
});
