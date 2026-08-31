// Dev-only lint for the dashboard's frontend. Two rules carry most of the value here and both have
// already earned it: `no-unused-vars` (a dead `RISKY` set shipped in v0.3.10, declared and never
// read) and `no-undef` (module scope is not global scope, so a helper an inline handler names has
// to be published on `window` explicitly — forgetting reads as a button that does nothing).

import js from "@eslint/js";

export default [
  {
    ...js.configs.recommended,
    files: ["src/rein/ui_assets/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: {
        // The browser surface these modules actually use — listed rather than pulled from a
        // globals package, so what the page is allowed to reach is reviewable here.
        window: "readonly",
        document: "readonly",
        location: "readonly",
        localStorage: "readonly",
        navigator: "readonly",
        fetch: "readonly",
        EventSource: "readonly",
        CustomEvent: "readonly",
        Notification: "readonly",
        CSS: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        getComputedStyle: "readonly",
      },
    },
    rules: {
      ...js.configs.recommended.rules,
      eqeqeq: ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  {
    ...js.configs.recommended,
    files: ["tests/ui/*.mjs", "eslint.config.mjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { process: "readonly", globalThis: "readonly", setTimeout: "readonly" },
    },
    rules: { ...js.configs.recommended.rules, "no-empty": ["error", { allowEmptyCatch: true }] },
  },
];
