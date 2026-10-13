// The reviews screen (CR-51): a lane per stage, each with what belongs to that stage alone, switched
// in place, and the reviews dragged onto it from the palette and taken off with ×. The draft is
// applied in one write, with a reason if one is given. Nothing is required.

import assert from "node:assert/strict";
import test from "node:test";
import { baseRoutes, boot, withStatus } from "./_harness.mjs";

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

async function reviewsScreen({ readOnly = false, preview = () => ({ changes: ["build: no longer reads for simplification"] }) } = {}) {
  const posts = [];
  const previews = [];
  const app = await boot({
    hash: "#reviews",
    readOnly,
    routes: baseRoutes((url, options) => {
      if (url === "/api/reviews/preview") {
        const asked = JSON.parse(options.body);
        previews.push(asked);
        return preview(asked.document);
      }
      if (url !== "/api/reviews") return undefined;
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return { ok: true, changes: ["recorded"] };
      }
      return PAYLOAD;
    }),
  });
  await app.open();
  return { app, posts, previews };
}

// The preview is asked a moment after the last edit, so a burst of edits asks once.
async function previewed(app) {
  const doc = app.window.document;
  for (let i = 0; i < 40; i++) {
    if (doc.querySelector(".pending-list, .pending .warn")) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.fail("no preview of the draft appeared");
}

async function applyWith(app, reason) {
  await previewed(app);
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
  await applyWith(app, "the team runs its own linter");

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].document.build.reviews, ["correctness"]);
  assert.equal(posts[0].reason, "the team runs its own linter");
  // Made against the version the screen was served, so a change that landed meanwhile is refused.
  assert.equal(posts[0].expect, "sha256:served");
});

test("a change is applied without a reason (CR-76)", async () => {
  const { app, posts } = await reviewsScreen();
  await app.click('button[aria-label="remove simplification from build"]');
  await previewed(app);
  await app.click({ text: "Apply" });

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].document.build.reviews, ["correctness"]);
  assert.equal(posts[0].reason, "");
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

test("an edit says what it will change before it is applied", async () => {
  const { app, previews } = await reviewsScreen();
  const doc = app.window.document;
  assert.match(doc.querySelector(".pending").textContent, /No changes/);
  await app.click('button[aria-label="remove simplification from build"]');
  await previewed(app);

  assert.deepEqual(
    [...doc.querySelectorAll(".pending-list li")].map((li) => li.textContent),
    ["build: no longer reads for simplification"],
    "in the words the record will hold, asked of the server",
  );
  assert.deepEqual(previews.at(-1).document.build.reviews, ["correctness"]);
  assert.equal(doc.querySelector(`${LANE("build")}`).dataset.changed, "yes");
  assert.ok(!byText(doc, "Apply").disabled, "a reason is not required");
});

test("a draft the server would refuse says why, and cannot be applied", async () => {
  const { app } = await reviewsScreen({ preview: () => withStatus(400, { error: "comparison needs the extraction" }) });
  const doc = app.window.document;
  await app.click(TOGGLE("design", "adversarial review"));
  await previewed(app);
  assert.match(doc.querySelector(".pending .warn").textContent, /comparison needs the extraction/);
  await app.type('input[aria-label="reason"]', "any");
  assert.ok(byText(doc, "Apply").disabled);
});

test("a review is added to a lane without dragging", async () => {
  const { app, posts } = await reviewsScreen();
  const doc = app.window.document;
  const options = [...doc.querySelectorAll(`${LANE("build")} select option`)].map((o) => o.value).filter(Boolean);
  assert.deepEqual(options, ["adversarial", "security"], "only what the lane does not already read");
  await app.select(`${LANE("build")} select[aria-label="add a review to build"]`, "security");
  await applyWith(app, "security per task");
  assert.deepEqual(posts[0].document.build.reviews, ["correctness", "simplification", "security"]);
});

test("a read-only page shows the lanes, drags nothing and changes nothing", async () => {
  const { app } = await reviewsScreen({ readOnly: true });
  const doc = app.window.document;
  assert.match(doc.querySelector("#view-reviews .warn").textContent, /read-only, so nothing here can be changed/);
  assert.equal(doc.querySelector(".pending"), null);
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
