// Boot the dashboard's real modules against a jsdom document and a scripted server.
//
// The page is ES modules served from `/assets/`, which nothing outside a browser resolves — so the
// modules are copied to a temp directory with that prefix rewritten to a relative one and imported
// from there. Nothing else about them is altered: these tests run the shipped code.
//
// The server is scripted rather than started. What these tests are about is the page — which
// screen renders, which panel opens, which request goes out — and the Python suite already owns
// whether `/api/stream` says the right things. Splitting it that way keeps a frontend test from
// needing a repository, a git history and a working tree to assert that a button opens a form.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(HERE, "../../src/rein/ui_assets");

/** A route answering with a status other than 200 (409 is a real answer on the review writes). */
export const withStatus = (status, body) => ({ __status: status, __body: body });

export const STATUS = JSON.parse(fs.readFileSync(path.join(HERE, "fixtures/status.json"), "utf8"));

/** The shipped modules, with `/assets/x.js` rewritten to `./x.js` so node can import them. */
function stageModules() {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "rein-ui-"));
  for (const name of fs.readdirSync(ASSETS)) {
    if (!name.endsWith(".js")) continue;
    const src = fs.readFileSync(path.join(ASSETS, name), "utf8");
    fs.writeFileSync(path.join(out, name), src.replaceAll('"/assets/', '"./'));
  }
  return out;
}

/**
 * Load the page and its modules.
 *
 * `routes(url, options)` returns the JSON body for a request, or a {status, body} pair.
 * `stream` receives a `push(event, data)` function, so a test can make the server speak.
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
  // jsdom has no canvas without a native package, and notify.js already handles a null context
  // (the tab title still carries the state). Returning null is that path, without the noise.
  w.HTMLCanvasElement.prototype.getContext = () => null;
  for (const key of ["window", "document", "location", "localStorage", "CustomEvent", "Event", "CSS"]) {
    if (w[key] === undefined) continue;
    try {
      globalThis[key] = w[key];
    } catch {
      /* a few of these are read-only node globals; the modules reach them through window anyway */
    }
  }
  globalThis.CSS = globalThis.CSS || {};
  if (!globalThis.CSS.escape) globalThis.CSS.escape = (s) => String(s).replace(/[^\w-]/g, (c) => "\\" + c);
  w.TOKEN = readOnly ? "" : "tok";
  w.READ_ONLY = readOnly;

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

  await import(pathToFileURL(path.join(stageModules(), "app.js")).href);

  const push = (name, data) => {
    for (const fn of listeners[name] || []) fn({ data: JSON.stringify(data) });
  };
  const open = () => {
    const es = FakeEventSource.last;
    if (es?.onopen) es.onopen();
  };

  return {
    window: w,
    calls,
    errors,
    push,
    open,
    /** Let queued microtasks and any awaited fetch settle. */
    settle: (ms = 40) => new Promise((r) => setTimeout(r, ms)),
    /** Navigate the hash router the way a click on the spine does. */
    go(nextHash) {
      w.location.hash = nextHash;
      w.dispatchEvent(new w.Event("hashchange"));
    },
    click(selector) {
      const el = w.document.querySelector(selector);
      assert.ok(el, "no such element: " + selector);
      el.dispatchEvent(new w.Event("click", { bubbles: true }));
      return el;
    },
    html(id) {
      const el = w.document.getElementById(id);
      assert.ok(el, "no such element: #" + id);
      return el.innerHTML;
    },
    text(id) {
      return w.document.getElementById(id).textContent;
    },
  };
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
