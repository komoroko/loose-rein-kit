// The states a dashboard is actually found in: a link to a gate the server refuses, a read-only
// page, and a connection that dropped.

import assert from "node:assert/strict";
import test from "node:test";
import { STATUS, baseRoutes, boot, withStatus } from "./_harness.mjs";

test("a link to a gate the server refuses settles, and says why", async () => {
  const app = await boot({
    hash: "#gate/bogus",
    routes: baseRoutes((url) =>
      url === "/api/review/bogus" ? withStatus(404, { error: "unknown gate: bogus" }) : undefined,
    ),
  });
  app.open();
  app.push("status", STATUS);
  await app.settle();

  assert.match(app.html("rvMain"), /unknown gate: bogus/);
  const before = app.calls.filter((c) => c.startsWith("/api/review/")).length;

  // The regression this locks: the pane decided "is this payload mine?" from the gate the response
  // echoes, which a refusal does not carry — so every push refetched, forever.
  for (let i = 0; i < 3; i++) {
    app.push("status", { ...STATUS, generated_at: `2026-08-30T12:00:0${i}` });
    await app.settle();
  }
  assert.equal(
    app.calls.filter((c) => c.startsWith("/api/review/")).length,
    before,
    "a refused gate must not be re-asked on every push",
  );
});

test("a read-only page offers no way to write and says where the authority is", async () => {
  const app = await boot({
    hash: "#gate/build",
    readOnly: true,
    routes: baseRoutes((url) =>
      url.startsWith("/api/review/")
        ? { gate: "build", index: 4, status: "pending", is_awaiting: true, deliverables: [], context: [] }
        : undefined,
    ),
  });
  app.open();
  app.push("status", STATUS);
  await app.settle();

  assert.doesNotMatch(app.html("rvFoot"), /data-act="approve"/);
  assert.match(app.html("rvFoot"), /rein approve build/);
  assert.match(app.html("ops"), /nothing here can be run/);
  assert.equal(app.window.document.getElementById("projectSelect").hidden, true);
});

test("the page says whether it is still being told", async () => {
  const app = await boot({ routes: baseRoutes() });
  assert.equal(app.text("live"), "connecting…");

  app.open();
  assert.equal(app.text("live"), "live");
  assert.equal(app.window.document.getElementById("dot").classList.contains("off"), false);

  const es = app.window.EventSource.last;
  es.onerror();
  assert.equal(app.text("live"), "reconnecting…");
  assert.equal(app.window.document.getElementById("dot").classList.contains("off"), true);
});

test("a status payload carrying an error is reported, not rendered as an empty board", async () => {
  const app = await boot({ routes: baseRoutes() });
  app.open();
  app.push("status", { error: "DocumentError: state.yaml is not valid" });
  await app.settle();
  assert.match(app.text("meta"), /state\.yaml is not valid/);
  assert.equal(app.errors.length, 0, app.errors.join("\n"));
});
