// The reviews screen (CR-51): lanes along the cycle, cards edited in a draft, applied in one write
// with a reason — and the comparison acceptance is decided by, shown and never offered.

import assert from "node:assert/strict";
import test from "node:test";
import { baseRoutes, boot } from "./_harness.mjs";

const DOCUMENT = {
  adversarial: { requirements: true, design: true, tasks: true },
  steps: [{ name: "review", reviews: ["correctness", "simplification"], retries: 1, stage: "both" }],
  updated_at: "2026-09-30T10:00:00",
};

const PAYLOAD = {
  document: DOCUMENT,
  builtin: ["correctness", "simplification", "security"],
  adversarial_stages: ["requirements", "design", "tasks"],
  acceptance: ["actual extraction", "comparison", "security review"],
};

async function reviewsScreen({ readOnly = false } = {}) {
  const posts = [];
  const app = await boot({
    hash: "#reviews",
    readOnly,
    routes: baseRoutes((url, options) => {
      if (url !== "/api/reviews") return undefined;
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return { ok: true, changes: ["step review: no longer reads for simplification"] };
      }
      return PAYLOAD;
    }),
  });
  await app.open();
  return { app, posts };
}

test("a review taken off a step's card is applied as the whole document, with the reason", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const lanes = [...doc.querySelectorAll(".lane")].map((lane) => lane.dataset.lane);
  assert.deepEqual(lanes, ["requirements", "design", "tasks", "build", "acceptance"]);

  await app.click('button[aria-label="stop reading for simplification"]');
  assert.equal(doc.querySelector('.chip[data-review="simplification"]'), null);
  assert.ok(byText(doc, "Apply").disabled, "no reason, no write");

  await app.type('input[aria-label="reason"]', "the team runs its own linter");
  await app.click({ text: "Apply" });

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].document.steps[0].reviews, ["correctness"]);
  assert.equal(posts[0].reason, "the team runs its own linter");
});

test("switching a stage's adversarial review off says what the mandate screen will show", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  await app.click('.lane[data-lane="design"] input[type="checkbox"]');
  assert.match(doc.querySelector('.lane[data-lane="design"]').textContent, /went without one/);
  await app.type('input[aria-label="reason"]', "a one-line fix");
  await app.click({ text: "Apply" });
  assert.equal(posts[0].document.adversarial.design, false);
});

test("comparison is shown in the acceptance lane and offers nothing to remove it with", async () => {
  const { app } = await reviewsScreen();
  const lane = app.window.document.querySelector('.lane[data-lane="acceptance"]');
  assert.match(lane.textContent, /comparison/);
  assert.equal(lane.querySelectorAll("button, input, select").length, 0);
});

test("a read-only page shows the lanes and changes nothing", async () => {
  const { app } = await reviewsScreen({ readOnly: true });
  const doc = app.window.document;
  assert.ok(doc.querySelector('.chip[data-review="correctness"]'));
  assert.equal(byText(doc, "Apply"), null);
  const controls = [...doc.querySelectorAll("#view-reviews button, #view-reviews input, #view-reviews select")];
  assert.ok(controls.length && controls.every((el) => el.disabled), "every control is disabled");
});

function byText(doc, text) {
  return [...doc.querySelectorAll("button")].find((el) => el.textContent.includes(text)) || null;
}
