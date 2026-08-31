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
  gate: "requirements",
  index: 1,
  status: "pending",
  is_awaiting: true,
  awaiting: "requirements",
  deliverables: [
    { id: "req", label: "docs/10-requirements.md", exists: true, path: "docs/10-requirements.md", html: "<p>B.</p>" },
  ],
  context: [],
};

async function readingRoom({ readiness = { ok: true, covers: { plan: "sha256:aa", tasks: "sha256:bb" } } } = {}) {
  const posts = [];
  const app = await boot({
    hash: "#gate/requirements",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push({ url, body: JSON.parse(options.body) });
        return { ok: true, gate: "requirements", approval_id: "GA-1" };
      }
      if (url.endsWith("/readiness")) return readiness;
      if (url.startsWith("/api/review/")) return REVIEW;
      return undefined;
    }),
  });
  app.open();
  app.push("status", AWAITING);
  await app.settle();
  return { app, posts };
}

test("the footer offers the decision, and the panel says what it would bind", async () => {
  const { app } = await readingRoom();
  assert.match(app.html("rvFoot"), /data-act="approve"/);

  app.click('[data-act="approve"]');
  await app.settle();
  const panel = app.html("rvFoot");
  assert.match(panel, /sha256:aa/);
  assert.match(panel, /sha256:bb/);
  assert.match(panel, /Not opened in this pane yet/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("a status push does not wipe an open panel", async () => {
  const { app } = await readingRoom();
  app.click('[data-act="approve"]');
  await app.settle();

  app.push("status", { ...AWAITING, generated_at: "2026-08-30T12:00:00" });
  await app.settle();
  assert.ok(
    app.window.document.querySelector("#rvFoot .confirm"),
    "the digests must survive the server speaking while they are being read",
  );

  app.click('[data-act="cancel"]');
  await app.settle();
  assert.equal(app.window.document.querySelector("#rvFoot .confirm"), null);
});

test("an unready gate is refused in the pane that asked, with its blockers", async () => {
  const { app, posts } = await readingRoom({
    readiness: { ok: false, blockers: ["tasks not done: T-002", "no machine review"] },
  });
  app.click('[data-act="approve"]');
  await app.settle();
  const panel = app.html("rvFoot");
  assert.match(panel, /will not open yet/);
  assert.match(panel, /tasks not done: T-002/);
  assert.equal(posts.length, 0, "a refusal must not have recorded anything");
});

test("approving posts the digests that were on screen, and echoes nothing to the console", async () => {
  const { app, posts } = await readingRoom();
  app.click('[data-act="approve"]');
  await app.settle();
  app.click('[data-act="approve-go"]');
  await app.settle();

  assert.deepEqual(posts.map((p) => p.url), ["/api/gate/approve"]);
  assert.deepEqual(posts[0].body, { gate: "requirements", covers: { plan: "sha256:aa", tasks: "sha256:bb" } });
  assert.equal(app.window.document.getElementById("out").hidden, true, "a decision is not a command's output");
});

test("requesting changes is a form, prefilled with what is being read", async () => {
  const { app, posts } = await readingRoom();
  app.click('[data-act="changes"]');
  await app.settle();
  const target = app.window.document.querySelector('[data-field="target"]');
  assert.equal(target.value, "docs/10-requirements.md");

  app.click('[data-act="changes-go"]');
  await app.settle();
  assert.equal(posts.length, 0, "an empty reason must not be sent");

  target.value = "docs/10-requirements.md#R-3";
  app.window.document.querySelector('[data-field="reason"]').value = "R-3 has no acceptance criterion.";
  app.click('[data-act="changes-go"]');
  await app.settle();
  assert.deepEqual(posts[0], {
    url: "/api/changes",
    body: { gate: "requirements", target: "docs/10-requirements.md#R-3", reason: "R-3 has no acceptance criterion." },
  });
});

test("the console states a roll-back's consequence above the button that runs it", async () => {
  const posts = [];
  const app = await boot({
    hash: "#console",
    routes: baseRoutes((url, options) => {
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return { action: "revise", argv: ["make", "revise"], exit_code: 0, stdout: "ok", stderr: "" };
      }
      return undefined;
    }),
  });
  app.open();
  app.push("status", STATUS);
  await app.settle();

  app.click('[data-ops="revise"]');
  await app.settle();
  assert.equal(app.window.document.getElementById("opsConfirm").hidden, true, "no reason, no dialog");

  app.window.document.getElementById("revReason").value = "the auth model is wrong";
  app.click('[data-ops="revise"]');
  await app.settle();
  const confirm = app.html("opsConfirm");
  assert.match(confirm, /Gates reset in a chain/);
  assert.match(confirm, /the auth model is wrong/);

  app.click('[data-confirm="go"]');
  await app.settle();
  assert.deepEqual(posts, [
    { action: "revise", params: { phase: "requirements", reason: "the auth model is wrong" } },
  ]);
  assert.equal(app.window.document.getElementById("out").hidden, false, "a command's output is the result");
});
