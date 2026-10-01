// The reviews screen (CR-51): lanes along the cycle, a review dragged onto the lane that runs it and
// taken off with ×, the draft applied in one write with a reason. Acceptance's own readings, the
// blind extraction and the comparison, are switched on and off in place. Nothing is required.

import assert from "node:assert/strict";
import test from "node:test";
import { baseRoutes, boot } from "./_harness.mjs";

const DOCUMENT = {
  adversarial: { requirements: true, design: true, tasks: true },
  steps: [{ name: "review", reviews: ["correctness", "simplification"], retries: 1, stage: "both" }],
  acceptance: { actual_extraction: true, comparison: true },
  whole_change: { security: false },
};

const PAYLOAD = {
  document: DOCUMENT,
  digest: "sha256:served",
  builtin: ["adversarial", "correctness", "simplification", "security"],
  adversarial_stages: ["requirements", "design", "tasks"],
  whole_change: ["security"],
  acceptance: ["actual_extraction", "comparison"],
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
        return { ok: true, changes: ["recorded"] };
      }
      return PAYLOAD;
    }),
  });
  await app.open();
  return { app, posts };
}

async function applyWith(app, reason) {
  await app.type('input[aria-label="reason"]', reason);
  await app.click({ text: "Apply" });
}

const PALETTE = (name) => `.palette .chip[data-review="${name}"]`;
const LANE = (name) => `.lane[data-lane="${name}"]`;

test("the lanes follow the cycle, and every review on them has a ×", async () => {
  const { app } = await reviewsScreen();
  const doc = app.window.document;
  const lanes = [...doc.querySelectorAll(".lane")].map((lane) => lane.dataset.lane);
  assert.deepEqual(lanes, ["requirements", "design", "tasks", "build", "whole-change", "acceptance"]);
  const placed = [...doc.querySelectorAll(".lane .placed, .lane .chip")];
  assert.ok(!placed.some((el) => el.closest('[data-lane="acceptance"]')), "acceptance's readings are switches");
  assert.ok(placed.length);
  for (const review of placed) {
    assert.ok(review.querySelector("button[aria-label]"), `${review.dataset.review} has a ×`);
  }
});

test("a review taken off a step's card is applied as the whole document, with the reason", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  await app.click('button[aria-label="stop reading for simplification"]');
  assert.equal(doc.querySelector('.chip[data-review="simplification"]:not(.palette .chip)'), null);
  assert.ok(byText(doc, "Apply").disabled, "no reason, no write");

  await applyWith(app, "the team runs its own linter");

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].document.steps[0].reviews, ["correctness"]);
  assert.equal(posts[0].reason, "the team runs its own linter");
  // Made against the version the screen was served, so a change that landed meanwhile is refused.
  assert.equal(posts[0].expect, "sha256:served");
});

test("taking off a step's last review takes off the step", async () => {
  const { app, posts } = await reviewsScreen();
  await app.click('button[aria-label="stop reading for simplification"]');
  await app.click('button[aria-label="stop reading for correctness"]');
  assert.equal(app.window.document.querySelector('.step[data-step="review"]'), null);
  await applyWith(app, "no reviewer per batch");
  assert.deepEqual(posts[0].document.steps, []);
});

test("a review dropped on a step is read by it, and dropped on the lane by a step of its own", async () => {
  const { app, posts } = await reviewsScreen();
  assert.ok(await app.drag(PALETTE("adversarial"), '.step[data-step="review"]'));
  assert.ok(await app.drag(PALETTE("security"), LANE("build")));
  await applyWith(app, "refute, and read for security per batch");
  assert.deepEqual(
    posts[0].document.steps.map((s) => [s.name, s.reviews]),
    [
      ["review", ["correctness", "simplification", "adversarial"]],
      ["review-2", ["security"]],
    ],
  );
});

test("a lane refuses a review it cannot run, and a step refuses one it already reads", async () => {
  const { app } = await reviewsScreen();
  assert.equal(await app.drag(PALETTE("correctness"), LANE("design")), false, "drafting runs the adversarial review");
  assert.equal(await app.drag(PALETTE("correctness"), '.step[data-step="review"]'), false, "already read there");
  assert.equal(await app.drag(PALETTE("adversarial"), LANE("design")), false, "already on");
});

test("the adversarial review of a drafting stage is removed with × and dragged back", async () => {
  const { app, posts } = await reviewsScreen();
  const lane = () => app.window.document.querySelector(LANE("design"));
  const remove = () => app.click(`${LANE("design")} button[aria-label="remove adversarial"]`);
  await remove();
  assert.match(lane().textContent, /names this stage, with when and why/);
  assert.ok(await app.drag(PALETTE("adversarial"), LANE("design")));
  assert.ok(lane().querySelector('.placed[data-review="adversarial"]'));

  await remove();
  await applyWith(app, "a one-line fix");
  assert.equal(posts[0].document.adversarial.design, false);
});

test("acceptance's readings are switched in place, and the comparison never runs without the extraction", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const lane = () => doc.querySelector(LANE("acceptance"));
  const toggle = (name) => `${LANE("acceptance")} input[aria-label="${name}"]`;
  assert.equal(doc.querySelector('.palette [data-review="comparison"]'), null, "not a review to drag");
  assert.equal(lane().querySelectorAll("button").length, 0, "switched, not removed with ×");
  assert.equal(await app.drag(PALETTE("security"), LANE("acceptance")), false);

  await app.click(toggle("comparison"));
  assert.match(lane().textContent, /every claim reaches acceptance as a question for you/);
  await app.click(toggle("actual extraction"));
  await app.click(toggle("comparison"));
  assert.ok(doc.querySelector(toggle("actual extraction")).checked, "switching the comparison on takes the extraction");
  await app.click(toggle("actual extraction"));
  assert.equal(doc.querySelector(toggle("comparison")).checked, false, "and switching it off takes the comparison");

  await applyWith(app, "a spike nobody will ship");
  assert.deepEqual(posts[0].document.acceptance, { actual_extraction: false, comparison: false });
});

test("the security reading of the whole change is dragged on and applied with the reason", async () => {
  const { app, posts } = await reviewsScreen();
  const lane = () => app.window.document.querySelector(LANE("whole-change"));
  assert.match(lane().textContent, /no security reading was taken/);
  assert.ok(await app.drag(PALETTE("security"), LANE("whole-change")));
  assert.ok(lane().querySelector('.placed[data-review="security"]'));
  await applyWith(app, "this change touches auth");
  assert.deepEqual(posts[0].document.whole_change, { security: true });
});

test("a custom review joins the palette, and deleting it takes it off every step", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const addPerformance = async () => {
    await app.type('input[aria-label="custom review name"]', "performance");
    await app.type('textarea[aria-label="custom review question"]', "Does every query use an index?");
    await app.click("#view-reviews .block > .rcard-row > button");
  };
  await addPerformance();
  assert.ok(await app.drag(PALETTE("performance"), LANE("build")));
  assert.ok(doc.querySelector('.step .chip[data-review="performance"]'));

  await app.click('button[aria-label="delete performance"]');
  assert.equal(doc.querySelector('[data-review="performance"]'), null, "off the palette and off its step");

  await addPerformance();
  await applyWith(app, "slow pages");
  assert.deepEqual(posts[0].document.custom, [{ name: "performance", question: "Does every query use an index?" }]);
  assert.deepEqual(
    posts[0].document.steps.map((s) => s.name),
    ["review"],
    "the step it alone was read by went with it",
  );
});

test("a read-only page shows the lanes, drags nothing and changes nothing", async () => {
  const { app } = await reviewsScreen({ readOnly: true });
  const doc = app.window.document;
  assert.ok(doc.querySelector('.step .chip[data-review="correctness"]'));
  assert.equal(byText(doc, "Apply"), null);
  assert.ok([...doc.querySelectorAll(".palette .chip")].every((chip) => chip.getAttribute("draggable") === "false"));
  assert.equal(await app.drag(PALETTE("security"), LANE("whole-change")), false);
  const controls = [...doc.querySelectorAll("#view-reviews button, #view-reviews input, #view-reviews select")];
  assert.ok(controls.length && controls.every((el) => el.disabled), "every control is disabled");
});

function byText(doc, text) {
  return [...doc.querySelectorAll("button")].find((el) => el.textContent.includes(text)) || null;
}
