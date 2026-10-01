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
    residue: { inferred: ["C-002: aligned on an AI's reading alone"], not_read: ["no security reviewer read it"] },
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
