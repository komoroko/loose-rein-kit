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

// --- a gate-④ generation in flight -------------------------------------------
//
// The line reaches the page on the SSE `status` push and nowhere else. The gate pane fetches
// `/api/review/session` once per gate and never polls, so a progress figure hung off that payload
// would render once and then sit still for the thirteen hours a composed review can take.

const running = (over = {}) => ({ ...STATUS, review_run: { ...STATUS.review_run, ...over } });

test("Now says a review is being generated, and says how far in", async () => {
  const app = await boot({ routes: baseRoutes((url) => (url.startsWith("/api/review/") ? REVIEW : undefined)) });
  await app.open();
  await app.push("status", running());
  const next = app.html("next");
  assert.match(next, /generating the grounded review/);
  assert.match(next, /7\/19 stages/);
  assert.match(next, /actual_extraction \[T-003\]/);
  // Not in "In the way of": that pane lists what waits on the human, and a command already running
  // is what the human is waiting on.
  assert.doesNotMatch(app.html("attention"), /generating the grounded review/);
});

test("a run that stopped reporting is drawn as stopped, not as live", async () => {
  const app = await boot({ routes: baseRoutes((url) => (url.startsWith("/api/review/") ? REVIEW : undefined)) });
  await app.open();
  await app.push("status", running({ stale: true }));
  const next = app.html("next");
  assert.match(next, /stopped reporting/);
  assert.doesNotMatch(next, /generating the grounded review/);
});

test("a finished run leaves no line behind", async () => {
  const app = await boot({ routes: baseRoutes((url) => (url.startsWith("/api/review/") ? REVIEW : undefined)) });
  await app.open();
  await app.push("status", running({ outcome: "generated" }));
  assert.doesNotMatch(app.html("next"), /grounded review/);
  await app.push("status", { ...STATUS, review_run: null });
  assert.doesNotMatch(app.html("next"), /grounded review/);
});

test("gate ④ with no review says a generation is running instead of falling back in silence", async () => {
  const app = await boot({
    hash: "#gate/build",
    routes: baseRoutes((url) => {
      if (url === "/api/review/session") return { generated: false, reason: "no machine review has been generated" };
      return url.startsWith("/api/review/") ? REVIEW : undefined;
    }),
  });
  await app.open();
  await app.push("status", running());
  assert.match(app.html("rvBar"), /generating the grounded review/);
});
