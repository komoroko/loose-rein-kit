// Build the dashboard's frontend: `ui/*.jsx` → the single module `rein ui` serves.
//
// The output is COMMITTED. `rein` is a Python package installed from a wheel, and nobody who runs
// the CLI has node — so the artifact has to be in the tree, the same way `.rein/prompts` is
// materialized rather than generated on demand. `--check` is what keeps a committed artifact
// honest: it rebuilds into memory and compares, so a source edit that was never rebuilt fails the
// quality gate instead of shipping the previous bundle. That is the same bargain `rein sync
// --check` makes for the materialized prompts.
//
// esbuild is pinned in pnpm-lock.yaml, so the same sources produce the same bytes for everyone.

import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "src/rein/ui_assets/app.js");

const result = await build({
  entryPoints: [path.join(ROOT, "ui/main.jsx")],
  bundle: true,
  format: "esm",
  target: "es2022",
  jsx: "automatic",
  minify: true,
  legalComments: "inline",
  define: { "process.env.NODE_ENV": '"production"' },
  write: false,
  outfile: OUT,
});

const built = result.outputFiles[0].text;

if (process.argv.includes("--check")) {
  let current = "";
  try {
    current = readFileSync(OUT, "utf8");
  } catch {
    /* never built: the mismatch below reports it */
  }
  if (current !== built) {
    console.error(
      "build-check: src/rein/ui_assets/app.js does not match a rebuild of ui/.\n" +
      "The committed bundle is what `rein ui` serves, so a stale one ships the previous page.\n" +
      "Run `pnpm run build` and commit the result."
    );
    process.exit(1);
  }
  console.log("build-check: the shipped bundle matches ui/.");
} else {
  writeFileSync(OUT, built);
  console.log(`build: ${OUT} (${(built.length / 1024).toFixed(1)} KB)`);
}
