// The reviews screen (CR-51): a lane per stage, each with what belongs to that stage alone, switched
// in place, and the reviews dragged onto it from the palette and taken off with ×. The draft is
// applied in one write with a reason. Nothing is required.

import assert from "node:assert/strict";
import test from "node:test";
import { baseRoutes, boot } from "./_harness.mjs";

const DOCUMENT = {
  requirements: { adversarial: true, reviews: [] },
  design: { adversarial: true, reviews: [] },
  tasks: { adversarial: true, reviews: [] },
  build: { reviews: ["correctness", "simplification"] },
  acceptance: { actual_extraction: true, comparison: true, reviews: [] },
};

const PAYLOAD = {
  document: DOCUMENT,
  digest: "sha256:served",
  builtin: ["adversarial", "correctness", "simplification", "security"],
  stages: [
    { name: "requirements", switches: ["adversarial"] },
    { name: "design", switches: ["adversarial"] },
    { name: "tasks", switches: ["adversarial"] },
    { name: "build", switches: [] },
    { name: "acceptance", switches: ["actual_extraction", "comparison"] },
  ],
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
const TOGGLE = (lane, label) => `${LANE(lane)} input[aria-label="${label}"]`;

test("every stage is a lane, and every review on one has a ×", async () => {
  const { app } = await reviewsScreen();
  const doc = app.window.document;
  const lanes = [...doc.querySelectorAll(".lane")].map((lane) => lane.dataset.lane);
  assert.deepEqual(lanes, ["requirements", "design", "tasks", "build", "acceptance"]);
  const placed = [...doc.querySelectorAll(".lane .chip")];
  assert.ok(placed.length);
  for (const review of placed) assert.ok(review.querySelector("button[aria-label^='remove']"), review.dataset.review);
});

test("a review taken off a lane is applied as the whole document, with the reason", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  await app.click('button[aria-label="remove simplification from build"]');
  assert.equal(doc.querySelector(`${LANE("build")} .chip[data-review="simplification"]`), null);
  assert.ok(byText(doc, "Apply").disabled, "no reason, no write");

  await applyWith(app, "the team runs its own linter");

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].document.build.reviews, ["correctness"]);
  assert.equal(posts[0].reason, "the team runs its own linter");
  // Made against the version the screen was served, so a change that landed meanwhile is refused.
  assert.equal(posts[0].expect, "sha256:served");
});

test("asking a lane's reviews in another order is a change the screen sends", async () => {
  const { app, posts } = await reviewsScreen();
  await app.click(`${LANE("build")} .chip[data-review="simplification"] button[title="ask earlier"]`);
  await applyWith(app, "simplify first");
  assert.deepEqual(posts[0].document.build.reviews, ["simplification", "correctness"]);
});

test("any review dropped on any lane reads what that lane reads", async () => {
  const { app, posts } = await reviewsScreen();
  assert.ok(await app.drag(PALETTE("security"), LANE("design")), "a security reading of the design");
  assert.ok(await app.drag(PALETTE("security"), LANE("acceptance")), "and of the whole change");
  assert.ok(await app.drag(PALETTE("adversarial"), LANE("acceptance")));
  await applyWith(app, "this cycle touches auth");
  assert.deepEqual(posts[0].document.design.reviews, ["security"]);
  assert.deepEqual(posts[0].document.acceptance.reviews, ["security", "adversarial"]);
});

test("a lane refuses a review it already reads, and its own review, which is switched", async () => {
  const { app } = await reviewsScreen();
  assert.equal(await app.drag(PALETTE("correctness"), LANE("build")), false, "already read there");
  assert.equal(await app.drag(PALETTE("adversarial"), LANE("design")), false, "the design's own review");
  assert.ok(await app.drag(PALETTE("adversarial"), LANE("build")), "at build it is a review like any other");
});

test("a drafting stage's adversarial review is switched in place", async () => {
  const { app, posts } = await reviewsScreen();
  const lane = () => app.window.document.querySelector(LANE("design"));
  await app.click(TOGGLE("design", "adversarial review"));
  assert.match(lane().textContent, /names this stage, with when and why/);
  await applyWith(app, "a one-line fix");
  assert.deepEqual(posts[0].document.design, { adversarial: false, reviews: [] });
});

test("acceptance's readings are switched in place, and the comparison never runs without the extraction", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const lane = () => doc.querySelector(LANE("acceptance"));

  await app.click(TOGGLE("acceptance", "comparison"));
  assert.match(lane().textContent, /every claim reaches acceptance as a question for you/);
  await app.click(TOGGLE("acceptance", "actual extraction"));
  await app.click(TOGGLE("acceptance", "comparison"));
  assert.ok(doc.querySelector(TOGGLE("acceptance", "actual extraction")).checked, "the comparison takes the extraction");
  await app.click(TOGGLE("acceptance", "actual extraction"));
  assert.equal(doc.querySelector(TOGGLE("acceptance", "comparison")).checked, false, "and the extraction the comparison");

  await applyWith(app, "a spike nobody will ship");
  assert.deepEqual(posts[0].document.acceptance, { actual_extraction: false, comparison: false, reviews: [] });
});

test("a custom review joins the palette, and deleting it takes it off every lane", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const addPerformance = async () => {
    await app.type('input[aria-label="custom review name"]', "performance");
    await app.type('textarea[aria-label="custom review question"]', "Does every query use an index?");
    await app.click("#view-reviews .block > .rcard-row > button");
  };
  await addPerformance();
  assert.ok(await app.drag(PALETTE("performance"), LANE("build")));
  assert.ok(await app.drag(PALETTE("performance"), LANE("acceptance")));

  await app.click('button[aria-label="delete performance"]');
  assert.equal(doc.querySelector('[data-review="performance"]'), null, "off the palette and off every lane");

  await addPerformance();
  await applyWith(app, "slow pages");
  assert.deepEqual(posts[0].document.custom, [{ name: "performance", question: "Does every query use an index?" }]);
  assert.deepEqual(posts[0].document.build.reviews, ["correctness", "simplification"]);
  assert.deepEqual(posts[0].document.acceptance.reviews, []);
});

test("a read-only page shows the lanes, drags nothing and changes nothing", async () => {
  const { app } = await reviewsScreen({ readOnly: true });
  const doc = app.window.document;
  assert.ok(doc.querySelector(`${LANE("build")} .chip[data-review="correctness"]`));
  assert.equal(byText(doc, "Apply"), null);
  assert.ok([...doc.querySelectorAll(".palette .chip")].every((chip) => chip.getAttribute("draggable") === "false"));
  assert.equal(await app.drag(PALETTE("security"), LANE("acceptance")), false);
  const controls = [...doc.querySelectorAll("#view-reviews button, #view-reviews input, #view-reviews select")];
  assert.ok(controls.length && controls.every((el) => el.disabled), "every control is disabled");
});

function byText(doc, text) {
  return [...doc.querySelectorAll("button")].find((el) => el.textContent.includes(text)) || null;
}
