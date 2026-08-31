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
  await app.open();
  await app.push("status", AWAITING);
  return { app, posts };
}

const APPROVE = { text: "Approve gate ①" };

test("the footer offers the decision, and the panel says what it would bind", async () => {
  const { app } = await readingRoom();
  assert.match(app.text("rvFoot"), /Approve gate ①/);

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
  assert.deepEqual(posts[0].body, { gate: "requirements", covers: { plan: "sha256:aa", tasks: "sha256:bb" } });
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
    { action: "revise", params: { phase: "requirements", reason: "the auth model is wrong" } },
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
