// The decision surface: what an approval binds is shown where the reviewer asked for it, a form
// replaces every OS dialog, and a status push never wipes an open panel out from under a reader.

import assert from "node:assert/strict";
import test from "node:test";
import { STATUS, baseRoutes, boot } from "./_harness.mjs";

const AWAITING = {
  ...STATUS,
  gates: STATUS.gates.map((g) => ({ ...g, status: "pending", approval_id: null })),
};

const REVIEW = {
  gate: "mandate",
  status: "pending",
  is_awaiting: true,
  awaiting: "mandate",
  deliverables: [
    { id: "req", label: "docs/10-requirements.md", exists: true, path: "docs/10-requirements.md", html: "<p>B.</p>" },
  ],
  context: [],
};

async function readingRoom({ readiness = { ok: true, covers: { plan: "sha256:aa", tasks: "sha256:bb" } } } = {}) {
  const posts = [];
  const app = await boot({
    hash: "#gate/mandate",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push({ url, body: JSON.parse(options.body) });
        return { ok: true, gate: "mandate", approval_id: "GA-1" };
      }
      if (url.endsWith("/readiness")) return readiness;
      if (url.startsWith("/api/review/")) return REVIEW;
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", AWAITING);
  return { app, posts };
}

const APPROVE = { text: "Approve gate mandate" };

// A lens list that has to be opened before anything can be dropped has "keep them all" as its
// default — the always-on set the class system replaced, re-entering through a closed disclosure
// rather than through an empty `when:`.
const lensRow = (status) => ({
  id: `L-CODE-${status.toUpperCase()}`,
  stage: "code",
  status,
  attack: "interleave the change against itself",
  applies_when: "the change introduces shared mutable state; the diff says, the paths do not",
});

const withLenses = (...rows) => ({
  ok: true,
  covers: { plan: "sha256:aa" },
  naming: { unasked: [], overrule_cost: "", crossing: [], lenses: rows },
});

// The mirror of `test_every_list_the_naming_layer_carries_reaches_the_terminal`. The defect ran in
// this direction — a list `naming` built and only one route rendered — so checking the terminal
// alone would have passed while it was live.
test("every list the naming layer carries reaches this route too", async () => {
  const { app } = await readingRoom({
    readiness: {
      ok: true,
      covers: { plan: "sha256:aa" },
      naming: {
        unasked: [{ id: "D-001", subject: "which store", answer: "the chain", rationale: "one task" }],
        overrule_cost: "Overruling one now costs a task.",
        lenses: [lensRow("proposed")],
        crossing: [{ task_id: "T-001", title: "cut over", name: "the old table", detail: "dropped" }],
      },
    },
  });

  await app.click(APPROVE);

  const shown = app.text("rvFoot");
  assert.match(shown, /D-001/, "the decisions the loop settled without asking");
  assert.match(shown, /Overruling one now costs a task/, "what overruling one costs");
  assert.match(shown, /L-CODE-PROPOSED/, "the selection this mandate would freeze");
  assert.match(shown, /T-001/, "the stops this mandate creates");
});

test("the lens list opens itself when one of them is the human's to drop", async () => {
  const { app } = await readingRoom({ readiness: withLenses(lensRow("proposed")) });

  await app.click(APPROVE);

  const details = app.window.document.querySelector("#rvFoot details");
  assert.ok(details, "the selection this mandate would freeze belongs on the panel");
  assert.equal(details.open, true, "nothing can be dropped from a list nobody opened");
  assert.match(app.text("rvFoot"), /1 of them yours to keep or drop/);
});

test("with nothing to decide, the lens list stays folded", async () => {
  const { app } = await readingRoom({ readiness: withLenses(lensRow("applied")) });

  await app.click(APPROVE);

  const details = app.window.document.querySelector("#rvFoot details");
  assert.equal(details.open, false, "applied lenses are not a question — nobody is being asked");
  assert.doesNotMatch(app.text("rvFoot"), /yours to keep or drop/);
});

test("the footer offers the decision, and the panel says what it would bind", async () => {
  const { app } = await readingRoom();
  assert.match(app.text("rvFoot"), /Approve gate mandate/);

  await app.click(APPROVE);
  const panel = app.text("rvFoot");
  assert.match(panel, /sha256:aa/);
  assert.match(panel, /sha256:bb/);
  assert.match(panel, /Not opened in this pane yet/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("a status push does not wipe an open panel", async () => {
  const { app } = await readingRoom();
  await app.click(APPROVE);

  await app.push("status", { ...AWAITING, generated_at: "2026-08-30T12:00:00" });
  assert.ok(
    app.window.document.querySelector("#rvFoot .confirm"),
    "the digests must survive the server speaking while they are being read",
  );

  await app.click({ text: "Cancel" });
  assert.equal(app.window.document.querySelector("#rvFoot .confirm"), null);
});

test("an unready gate is refused in the pane that asked, with its blockers", async () => {
  const { app, posts } = await readingRoom({
    readiness: { ok: false, blockers: ["tasks not done: T-002", "no machine review"] },
  });
  await app.click(APPROVE);
  const panel = app.text("rvFoot");
  assert.match(panel, /will not open yet/);
  assert.match(panel, /tasks not done: T-002/);
  assert.equal(posts.length, 0, "a refusal must not have recorded anything");
});

test("approving posts the digests that were on screen, and echoes nothing to the console", async () => {
  const { app, posts } = await readingRoom();
  await app.click(APPROVE); // the footer's button opens the panel
  await app.click("#rvFoot .confirm button.primary"); // the panel's confirms in it

  assert.deepEqual(posts.map((p) => p.url), ["/api/gate/approve"]);
  assert.deepEqual(posts[0].body, { gate: "mandate", covers: { plan: "sha256:aa", tasks: "sha256:bb" } });
  await app.go("#console");
  assert.equal(app.window.document.getElementById("out"), null, "a decision is not a command's output");
});

test("requesting changes is a form, prefilled with what is being read", async () => {
  const { app, posts } = await readingRoom();
  await app.click({ text: "Request changes" });
  assert.equal(app.window.document.querySelector("#rvFoot .confirm input").value, "docs/10-requirements.md");

  await app.click({ text: "Request the change" });
  assert.equal(posts.length, 0, "an empty reason must not be sent");

  await app.type("#rvFoot .confirm input", "docs/10-requirements.md#R-3");
  await app.type("#rvFoot .confirm textarea", "R-3 has no acceptance criterion.");
  await app.click({ text: "Request the change" });
  assert.deepEqual(posts[0], {
    url: "/api/changes",
    body: { gate: "mandate", target: "docs/10-requirements.md#R-3", reason: "R-3 has no acceptance criterion." },
  });
});

test("the console states a roll-back's consequence above the button that runs it", async () => {
  const posts = [];
  const app = await boot({
    hash: "#console",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return { action: "revise", argv: ["rein", "revise"], exit_code: 0, stdout: "ok", stderr: "" };
      }
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", STATUS);

  await app.click({ text: "rein revise" });
  assert.equal(app.window.document.getElementById("opsConfirm"), null, "no reason, no dialog");

  await app.type("#revReason", "the auth model is wrong");
  await app.click({ text: "rein revise" });
  const confirm = app.text("opsConfirm");
  assert.match(confirm, /Gates reset in a chain/);
  assert.match(confirm, /the auth model is wrong/);

  await app.click({ text: "Yes, do it" });
  assert.deepEqual(posts, [
    { action: "revise", params: { gate: "mandate", reason: "the auth model is wrong" } },
  ]);
  assert.match(app.text("out"), /exit 0/, "a command's output is the result");
});

test("a half-typed reason survives the server speaking", async () => {
  const app = await boot({ hash: "#console", routes: baseRoutes() });
  await app.open();
  await app.push("status", STATUS);

  await app.type("#revReason", "half a thou");
  await app.push("status", { ...STATUS, generated_at: "2026-08-31T09:00:00" });
  assert.equal(app.window.document.getElementById("revReason").value, "half a thou");
});

test("the console names the agent behind each role and switches one without a confirm", async () => {
  // Pointing a role at another CLI is not a mandate decision — `agents` sits outside the config
  // digest the freeze covers — so it takes no roll-back dialog, unlike the two commands above it.
  const posts = [];
  const app = await boot({
    hash: "#console",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return { action: "agent", argv: ["rein", "agent", "copilot"], exit_code: 0, stdout: "", stderr: "" };
      }
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", STATUS);

  const roles = app.text("agentRoles");
  for (const role of ["implementer", "code_reviewer", "actual_extractor", "comparator", "security_reviewer"]) {
    assert.match(roles, new RegExp(role), "every role the config declares is on the page");
  }
  // The independence verdict comes from the payload, so the page cannot disagree with `rein agent
  // --show` about whether the extractor and the comparator are a real second opinion.
  assert.match(app.text("agentIndependence"), /WARN/);

  await app.select("#agent-implementer-adapter", "copilot");
  await app.type("#agent-implementer-model", "gpt-5.2");
  await app.click({ text: "Apply" });

  assert.equal(app.window.document.getElementById("opsConfirm"), null, "a switch rewinds nothing");
  assert.deepEqual(posts, [
    { action: "agent", params: { role: "implementer", adapter: "copilot", model: "gpt-5.2" } },
  ]);
});


// An irreversible point is a gate this cycle grew, so the dashboard has to be able to open it: a
// route that cannot is the same gap as a panel that omits what the gate requires, one step out.
const CROSSING = {
  ...REVIEW,
  gate: "T-001",
  awaiting: "T-001",
};

const CROSSING_READINESS = {
  ok: true,
  covers: { plan: "sha256:aa" },
  naming: {
    unasked: [],
    lenses: [],
    overrule_cost: "",
    crossing: [
      { task_id: "T-001", title: "migrate users", kind: "persistence", name: "the users table", adr: "ADR-007" },
    ],
  },
};

test("a gate named after a task opens in the same pane, and says what it cannot take back", async () => {
  const posts = [];
  const app = await boot({
    hash: "#gate/T-001",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push({ url, body: JSON.parse(options.body) });
        return { ok: true, gate: "T-001", approval_id: "GA-2" };
      }
      if (url.endsWith("/readiness")) return CROSSING_READINESS;
      if (url.startsWith("/api/review/")) return CROSSING;
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", {
    ...AWAITING,
    gates: [
      { name: "mandate", status: "approved", approval_id: "GA-0" },
      { name: "T-001", status: "pending", approval_id: null },
      { name: "acceptance", status: "pending", approval_id: null },
    ],
  });

  assert.match(app.text("rvFoot"), /Approve gate T-001/);
  await app.click({ text: "Approve gate T-001" });
  const panel = app.text("rvFoot");
  assert.match(panel, /cannot undo/);
  assert.match(panel, /the users table/);
  assert.match(panel, /ADR-007/);
});
