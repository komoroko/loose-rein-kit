// The orient stage: what was built, and what the evidence behind it is worth.
//
// The row this file exists for is the negative control. `build_loop` records, per task, whether the
// DoD's green could have gone red; until the brief carried it, nothing read that record and a task
// whose green rests on tests nobody wrote for it reached the approver as a silence. Rendering it is
// the last leg of that, so it is pinned here rather than left to a reading of the bundle.

import assert from "node:assert/strict";
import test from "node:test";
import { STATUS, baseRoutes, boot } from "./_harness.mjs";

const REVIEW = {
  gate: "build",
  index: 4,
  status: "pending",
  is_awaiting: true,
  awaiting: "build",
  deliverables: [],
  context: [],
};

async function orient(brief) {
  const app = await boot({
    hash: "#gate/build",
    routes: baseRoutes((url) => {
      if (url.startsWith("/api/review/stage/orient")) return { stage: "orient", brief, residual_findings: [] };
      if (url.endsWith("/api/review/session"))
        return { machine_digest: "sha256:aa", stages: [{ name: "orient", settled: null }] };
      if (url.endsWith("/readiness")) return { ok: false, missing: [] };
      if (url.startsWith("/api/review/")) return REVIEW;
      return undefined;
    }),
  });
  await app.open();
  await app.push("status", STATUS);
  return app;
}

test("a control that answered is a number and one that could not be taken names its task", async () => {
  const app = await orient({
    verification: { steps: 4 },
    control: {
      discriminating: 3,
      no_tests_changed: [{ task_id: "T-004", detail: "the change touched no test path" }],
    },
  });
  const html = app.html("rvMain");
  assert.match(html, /greens shown to be controlled/);
  assert.match(html, /T-004/);
  assert.match(html, /the change touched no test path/);
  // Said plainly, and not as a blocker: work covered by tests that already existed is a real thing.
  assert.match(html, /never shown to be able to go red/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("a landed task carrying no control at all is a row, not a silence", async () => {
  // Only the uncontrolled are listed, so a task the loop never asked about looked exactly like one
  // whose experiment answered. The brief names that absence; this is where a human sees it.
  const app = await orient({
    verification: { steps: 4 },
    control: {
      unrecorded: [{ task_id: "T-009", detail: "this task closed before the negative control existed" }],
    },
  });
  const html = app.html("rvMain");
  assert.match(html, /T-009/);
  assert.match(html, /before the negative control existed/);
  assert.match(html, /never shown to be able to go red/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("every control that answered leaves no warning at all", async () => {
  const app = await orient({ verification: { steps: 4 }, control: { discriminating: 3 } });
  const html = app.html("rvMain");
  assert.match(html, /greens shown to be controlled/);
  assert.doesNotMatch(html, /never shown to be able to go red/);
});

test("a brief with no control section renders the rest of the stage anyway", async () => {
  const app = await orient({ verification: { steps: 4 } });
  assert.match(app.html("rvMain"), /steps in the quality gate/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});
