// The accept stage: what approving takes on, and the approval itself.
//
// There used to be a freeze stage here with a button of its own, and the approval was a separate
// act after it. Approving now freezes the answers and integrates the cycle, so this stage shows the
// residue — what the machine could not settle — and offers the one act.

import assert from "node:assert/strict";
import test from "node:test";
import { STATUS, baseRoutes, boot } from "./_harness.mjs";

const REVIEW = {
  gate: "acceptance",
  status: "pending",
  is_awaiting: true,
  awaiting: "acceptance",
  deliverables: [],
  context: [],
};

async function accept(stage, session = {}) {
  const app = await boot({
    hash: "#gate/acceptance",
    routes: baseRoutes((url) => {
      if (url.startsWith("/api/review/stage/accept")) return { stage: "accept", ...stage };
      if (url.endsWith("/api/review/session"))
        return { machine_digest: "sha256:aa", stages: [{ name: "accept", settled: false }], ...session };
      if (url.endsWith("/readiness")) return { ok: false, blockers: [] };
      if (url.startsWith("/api/review/")) return REVIEW;
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", STATUS);
  return app;
}

test("the residue is listed by kind and the approval is offered when nothing blocks it", async () => {
  const app = await accept({
    residue: { inferred: ["C-002: aligned on an AI's reading alone"], not_read: ["no review read the whole change"] },
    completion_blockers: [],
  });
  const html = app.html("rvMain");
  assert.match(html, /claims aligned on an AI&#x27;s reading alone|claims aligned on an AI's reading alone/);
  assert.match(html, /C-002/);
  assert.match(html, /what nobody read/);
  assert.match(html, /Approve acceptance/);
  assert.doesNotMatch(html, /Freeze/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("an empty residue says so, and a blocker keeps the approval shut", async () => {
  const app = await accept({ residue: {}, completion_blockers: ["unanswered high/critical decision cards: DC-001"] });
  const html = app.html("rvMain");
  assert.match(html, /every claim is established/);
  assert.match(html, /DC-001/);
  assert.match(html, /<button[^>]*disabled[^>]*>Approve acceptance/);
});

// A decision an implementer made without asking asks for nothing — approving keeps it — so the one
// act offered is overruling it, and the answer is what it should be instead.
test("a decision made without asking is listed and can be overruled, with what it should be instead", async () => {
  const posts = [];
  const decision = {
    id: "LD-0000000a",
    task: "T-001",
    statement: "retry 3 times",
    rationale: "the API documents transient 503s",
    undo: "T-001: src/upload.py",
    anchor: "src/upload.py:42",
  };
  const app = await boot({
    hash: "#gate/acceptance",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push({ url, body: JSON.parse(options.body) });
        return { ok: true, machine_digest: "sha256:aa" };
      }
      if (url.startsWith("/api/review/stage/decision"))
        return { stage: "decision", decision_cards: [], local_decisions: [decision], overruled_decisions: [] };
      if (url.endsWith("/api/review/session"))
        return { machine_digest: "sha256:aa", stages: [{ name: "decision", settled: null }] };
      if (url.endsWith("/readiness")) return { ok: false, blockers: [] };
      if (url.startsWith("/api/review/")) return REVIEW;
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", STATUS);
  const html = app.html("rvMain");
  assert.match(html, /Decided without asking \(1\)/);
  assert.match(html, /retry 3 times/);
  assert.match(html, /overruling it redoes: T-001: src\/upload.py/);

  await app.type("[data-local-decision] input", "fail fast");
  await app.click({ text: "Overrule" });
  const [post] = posts.filter((p) => p.url.endsWith("/api/review/disposition"));
  assert.equal(post.body.subject_id, "LD-0000000a");
  assert.equal(post.body.action, "revise_implementation");
  assert.equal(post.body.note, "fail fast");
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});
