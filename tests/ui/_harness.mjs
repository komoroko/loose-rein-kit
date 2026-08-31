// Boot the dashboard against a jsdom document and a scripted server.
//
// What is imported is the SHIPPED BUNDLE — `src/rein/ui_assets/app.js`, the file `rein ui` serves —
// not the sources beside it. A test over sources that the build could mangle is a test of something
// nobody runs; `make check` separately proves the bundle is a rebuild of `ui/`, so this covers both.
//
// The server is scripted rather than started. What these tests are about is the page — which screen
// renders, which panel opens, which request goes out — and the Python suite already owns whether
// `/api/stream` says the right things. Splitting it that way keeps a frontend test from needing a
// repository, a git history and a working tree to assert that a button opens a form.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(HERE, "../../src/rein/ui_assets");

/** A route answering with a status other than 200 (409 is a real answer on the review writes). */
export const withStatus = (status, body) => ({ __status: status, __body: body });

export const STATUS = JSON.parse(fs.readFileSync(path.join(HERE, "fixtures/status.json"), "utf8"));

/**
 * Load the page and its bundle.
 *
 * `routes(url, options)` returns the JSON body for a request, or a {status, body} pair.
 * `push(event, data)` is the server speaking on the stream.
 */
export async function boot({ hash = "#now", readOnly = false, routes = () => ({}) } = {}) {
  const calls = [];
  const errors = [];
  const listeners = {};

  const html = fs
    .readFileSync(path.join(ASSETS, "index.html"), "utf8")
    .replace("__TOKEN__", readOnly ? "" : "tok")
    .replace("__READ_ONLY__", String(readOnly))
    .replace(/<script type="module"[^>]*><\/script>/, "");

  const dom = new JSDOM(html, { url: "http://localhost/" + hash, pretendToBeVisual: true });
  const w = dom.window;
  // jsdom has no canvas without a native package, and the notifier already handles a null context
  // (the tab title still carries the state). Returning null is that path, without the noise.
  w.HTMLCanvasElement.prototype.getContext = () => null;
  for (const key of ["window", "document", "location", "localStorage", "CustomEvent", "Event", "navigator"]) {
    if (w[key] === undefined) continue;
    try {
      globalThis[key] = w[key];
    } catch {
      /* a few of these are read-only node globals; the bundle reaches them through window anyway */
    }
  }
  w.TOKEN = readOnly ? "" : "tok";
  w.READ_ONLY = readOnly;
  // React schedules with these; jsdom has them, node's globals may not be the same objects.
  globalThis.requestAnimationFrame = w.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
  globalThis.cancelAnimationFrame = w.cancelAnimationFrame = (id) => clearTimeout(id);
  globalThis.IS_REACT_ACT_ENVIRONMENT = false;

  // A scripted EventSource: `push` is the server speaking. The real one reconnects on its own,
  // which is why the page has no retry logic of its own to fake here.
  class FakeEventSource {
    constructor(url) {
      calls.push(url);
      this.url = url;
      FakeEventSource.last = this;
    }
    addEventListener(name, fn) {
      (listeners[name] = listeners[name] || []).push(fn);
    }
    close() {}
  }
  globalThis.EventSource = w.EventSource = FakeEventSource;

  globalThis.fetch = w.fetch = async (url, options) => {
    calls.push((options?.method ? options.method + " " : "") + String(url));
    const answer = routes(String(url), options) ?? { error: "unstubbed " + url };
    const status = answer.__status ?? 200;
    const body = answer.__status ? answer.__body : answer;
    return {
      status,
      headers: { get: () => null },
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  };

  const onError = (e) => errors.push(String(e?.stack || e));
  process.on("uncaughtException", onError);
  process.on("unhandledRejection", onError);

  await import(pathToFileURL(path.join(ASSETS, "app.js")).href + "?t=" + Math.random());

  const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms));
  await settle();

  return {
    window: w,
    calls,
    errors,
    settle,
    push: async (name, data) => {
      for (const fn of listeners[name] || []) fn({ data: JSON.stringify(data) });
      await settle();
    },
    open: async () => {
      const es = FakeEventSource.last;
      if (es?.onopen) es.onopen();
      await settle();
    },
    /** Navigate the hash router the way a click on the spine does. */
    async go(nextHash) {
      w.location.hash = nextHash;
      w.dispatchEvent(new w.Event("hashchange"));
      await settle();
    },
    /** Click the first element matching `selector`, or the first whose text is `selector.text`. */
    async click(selector) {
      const el = typeof selector === "string" ? w.document.querySelector(selector) : byText(w, selector);
      assert.ok(el, "no such element: " + JSON.stringify(selector));
      el.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
      await settle();
      return el;
    },
    /** Type into an input or textarea the way React's onChange sees it. */
    async type(selector, value) {
      const el = w.document.querySelector(selector);
      assert.ok(el, "no such element: " + selector);
      const proto = el.tagName === "TEXTAREA" ? w.HTMLTextAreaElement : w.HTMLInputElement;
      Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
      el.dispatchEvent(new w.Event("input", { bubbles: true }));
      await settle();
      return el;
    },
    async select(selector, value) {
      const el = w.document.querySelector(selector);
      assert.ok(el, "no such element: " + selector);
      Object.getOwnPropertyDescriptor(w.HTMLSelectElement.prototype, "value").set.call(el, value);
      el.dispatchEvent(new w.Event("change", { bubbles: true }));
      await settle();
      return el;
    },
    html(id) {
      const el = w.document.getElementById(id);
      assert.ok(el, "no such element: #" + id);
      return el.innerHTML;
    },
    text(id) {
      const el = w.document.getElementById(id);
      assert.ok(el, "no such element: #" + id);
      return el.textContent;
    },
    body() {
      return w.document.body.textContent;
    },
  };
}

/** `{text: "Approve gate ④"}` — the way a person finds a button. */
function byText(w, { text, tag = "button" }) {
  return [...w.document.querySelectorAll(tag)].find((el) => el.textContent.includes(text)) || null;
}

/** The routes almost every test needs: projects, events, and nothing else claimed. */
export function baseRoutes(extra = () => undefined) {
  return (url, options) => {
    const answer = extra(url, options);
    if (answer !== undefined) return answer;
    if (url === "/api/projects") return { projects: [] };
    if (url.startsWith("/api/events")) return { total: 0, events: [] };
    return undefined;
  };
}
