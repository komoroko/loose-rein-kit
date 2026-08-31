// Dev-only lint for the dashboard's frontend.
//
// `no-unused-vars` has already earned its place here (a dead `RISKY` set shipped in v0.3.10,
// declared and never read). `no-undef` used to earn it too, back when inline handlers reached
// helpers through `window`; JSX passes functions directly, so that whole class is gone and the rule
// now just guards typos. The rules of hooks are the new ones that matter: a conditional hook or a
// missing dependency is a rendering bug that no test reliably catches.

import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import react from "eslint-plugin-react";

// The browser surface the page actually uses — listed rather than pulled from a globals package, so
// what it is allowed to reach stays reviewable here.
const BROWSER = {
  window: "readonly",
  document: "readonly",
  location: "readonly",
  localStorage: "readonly",
  navigator: "readonly",
  fetch: "readonly",
  EventSource: "readonly",
  Notification: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
};

export default [
  {
    files: ["ui/**/*.{js,jsx}"],
    plugins: { "react-hooks": reactHooks, react },
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: BROWSER,
    },
    rules: {
      ...js.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      // Without this, `no-unused-vars` cannot see that JSX uses a component.
      "react/jsx-uses-vars": "error",
      eqeqeq: ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  {
    files: ["tests/ui/*.mjs", "scripts/*.mjs", "eslint.config.mjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: {
        process: "readonly", globalThis: "readonly", console: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly",
      },
    },
    rules: { ...js.configs.recommended.rules, "no-empty": ["error", { allowEmptyCatch: true }] },
  },
];
