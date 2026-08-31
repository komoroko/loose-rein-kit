// Every screen renders from a real status payload, and the spine says one thing.

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

async function dashboard(hash = "#now") {
  const app = await boot({ hash, routes: baseRoutes((url) => (url.startsWith("/api/review/") ? REVIEW : undefined)) });
  await app.open();
  await app.push("status", STATUS);
  return app;
}

test("the page holds one stream and asks for nothing on a timer", async () => {
  const app = await dashboard();
  assert.ok(app.calls.includes("/api/stream"), "the page must open the status stream");
  assert.equal(app.calls.filter((c) => c === "/api/status").length, 0, "there is no status endpoint to poll");
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("the spine marks exactly one gate as the one waiting on you", async () => {
  const app = await dashboard();
  const spine = app.html("stepper");
  assert.equal((spine.match(/station awaiting/g) || []).length, 1, "one inverted block, never two");
  assert.match(spine, /href="#gate\/build"/);
  assert.match(spine, /station approved[^>]*href="#gate\/requirements"/);
  assert.equal((spine.match(/class="station /g) || []).length, STATUS.gates.length);
});

test("Now names the gate it is clearing the way for", async () => {
  const app = await dashboard();
  assert.match(app.text("attentionHead"), /In the way of gate ④ build/);
  assert.match(app.html("next"), /class="cmd"/);
  assert.match(app.html("attention"), /waiting on you/);
});

test("the Board's status pills cover every status the payload counts", async () => {
  const app = await dashboard("#board");
  const pills = app.html("tasks");
  for (const status of Object.keys(STATUS.tasks.counts)) {
    assert.match(pills, new RegExp(`chip ${status}">${status} `), `${status} is counted but not shown`);
  }
  // The regression this locks: awaiting-evidence was in `total` and in no pill at all.
  const shown = Object.keys(STATUS.tasks.counts).reduce(
    (sum, s) => sum + Number((pills.match(new RegExp(`chip ${s}">${s} (\\d+)`)) || [0, 0])[1]),
    0,
  );
  assert.equal(shown, STATUS.tasks.total, "the pills must add up to the total beside them");
});

test("the graph draws direction and says what its lines mean", async () => {
  const app = await dashboard("#board");
  const board = app.html("tasks");
  assert.match(board, /marker-end="url\(#dagarw/);
  assert.match(board, /execution layers/);
  assert.match(board, /critical path/);
});

test("each route mounts one view and no other", async () => {
  const app = await dashboard();
  const views = ["now", "gate", "board", "record", "console"];
  for (const [hash, shown] of [
    ["#now", "now"],
    ["#board", "board"],
    ["#record", "record"],
    ["#console", "console"],
    ["#gate/build", "gate"],
  ]) {
    await app.go(hash);
    for (const view of views) {
      const present = Boolean(app.window.document.getElementById("view-" + view));
      assert.equal(present, view === shown, `${hash} should mount only ${shown}`);
    }
  }
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("an unknown hash lands on the screen that says what to do", async () => {
  const app = await dashboard();
  await app.go("#nonsense");
  assert.ok(app.window.document.getElementById("view-now"), "an unroutable hash falls back to Now");
});

test("the Record screen fetches only when the log moved and someone is looking", async () => {
  const app = await dashboard();
  const feeds = () => app.calls.filter((c) => c.startsWith("/api/events")).length;
  await app.push("record", { revision: "1-1" });
  assert.equal(feeds(), 0, "nobody is on the Record screen yet");

  await app.go("#record");
  assert.equal(feeds(), 1, "opening it catches up on what was missed");

  await app.push("record", { revision: "2-2" });
  assert.equal(feeds(), 2, "a log that moved while it is open refetches");

  await app.go("#now");
  await app.push("record", { revision: "3-3" });
  assert.equal(feeds(), 2, "a log that moves while nobody is looking asks for nothing");
});
