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
  app.open();
  app.push("status", STATUS);
  await app.settle();
  return app;
}

test("the page holds one stream and asks for nothing on a timer", async () => {
  const app = await dashboard();
  assert.ok(app.calls.includes("/api/stream"), "the page must open the status stream");
  const polls = app.calls.filter((c) => c === "/api/status");
  assert.equal(polls.length, 0, "there is no status endpoint to poll any more");
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("the spine marks exactly one gate as the one waiting on you", async () => {
  const app = await dashboard();
  const spine = app.html("stepper");
  const awaiting = spine.match(/class="station awaiting/g) || [];
  assert.equal(awaiting.length, 1, "one inverted block, never two");
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

test("each route shows one view and the others stay hidden", async () => {
  const app = await dashboard();
  for (const [hash, shown] of [
    ["#now", "now"],
    ["#board", "board"],
    ["#record", "record"],
    ["#console", "console"],
    ["#gate/build", "gate"],
  ]) {
    app.go(hash);
    await app.settle();
    for (const view of ["now", "gate", "board", "record", "console"]) {
      assert.equal(
        app.window.document.getElementById("view-" + view).hidden,
        view !== shown,
        `${hash} should show only ${shown}`,
      );
    }
  }
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});

test("the Record screen fetches only when the log moved and someone is looking", async () => {
  const app = await dashboard();
  const feeds = () => app.calls.filter((c) => c.startsWith("/api/events")).length;
  app.push("record", { revision: "1-1" });
  await app.settle();
  assert.equal(feeds(), 0, "nobody is on the Record screen yet");

  app.go("#record");
  await app.settle();
  assert.equal(feeds(), 1, "opening it catches up on what was missed");

  app.push("record", { revision: "2-2" });
  await app.settle();
  assert.equal(feeds(), 2, "a log that moved while it is open refetches");
});
