const tsParser = require("@typescript-eslint/parser");
const tsPlugin = require("@typescript-eslint/eslint-plugin");
const reactHooksPlugin = require("eslint-plugin-react-hooks");
const jsxA11yPlugin = require("eslint-plugin-jsx-a11y");

module.exports = [
  {
    ignores: ["node_modules/**", "build/**", "coverage/**", "playwright-report/**"],
  },
  {
    files: ["src/**/*.{js,jsx,ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooksPlugin,
      "jsx-a11y": jsxA11yPlugin,
    },
    rules: {
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "no-console": ["warn", { allow: ["warn", "error"] }],

      // ─── jsx-a11y: lock in accessibility compliance ──────────────────
      // Spread the plugin's recommended ruleset. The currently-clean
      // recommended rules (26 after the overrides below) stay `error` so
      // a11y regressions are blocked in CI. The known high-volume debt
      // families and intentional patterns are overridden below to
      // `warn`/`off` so `npm run lint` stays green while the backlog is
      // burned down in later phases.
      ...jsxA11yPlugin.configs.recommended.rules,

      // Known debt — surfaced as warnings, not CI-blocking errors.
      "jsx-a11y/label-has-associated-control": "warn",
      "jsx-a11y/click-events-have-key-events": "warn",
      "jsx-a11y/no-static-element-interactions": "warn",
      "jsx-a11y/no-noninteractive-element-interactions": "warn",
      // Surfaces icon-only buttons lacking an accessible name (Phase 1).
      "jsx-a11y/control-has-associated-label": "warn",

      // Autofocus on the first field of create/edit forms is intentional
      // (Batch 6) — not an accessibility defect for this app.
      "jsx-a11y/no-autofocus": "off",
    },
  },
];
