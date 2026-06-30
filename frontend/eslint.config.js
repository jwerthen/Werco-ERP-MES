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
      // Spread the plugin's recommended ruleset: the currently-clean
      // recommended rules stay `error`, so any NEW a11y regression in those
      // categories fails CI (the regression gate). CI runs lint with
      // `--max-warnings=0` (ci-cd.yml), so there is no non-blocking "warn"
      // tier here — a rule is either an enforced error or `off`. The known
      // high-volume debt families below are therefore `off` (not warn) so
      // CI stays green; they are tracked as a follow-up, burned down by
      // extending the FormField label-association pattern across the forms.
      ...jsxA11yPlugin.configs.recommended.rules,

      // Known pre-existing debt — OFF until burned down (can't be `warn`:
      // CI's --max-warnings=0 would treat warnings as failures).
      // ~480 sites; close via FormField/htmlFor label association.
      "jsx-a11y/label-has-associated-control": "off",
      // ~630 sites; icon-only controls needing an accessible name.
      "jsx-a11y/control-has-associated-label": "off",

      // Clickable non-interactive elements needing keyboard handlers —
      // burned down to zero; now ENFORCED (clickable <div>/<span>/<li> must be a
      // native <button>, carry a literal interactive role + tabIndex + onKeyDown,
      // or be a presentational role="presentation" backdrop).
      "jsx-a11y/click-events-have-key-events": "error",
      "jsx-a11y/no-static-element-interactions": "error",
      "jsx-a11y/no-noninteractive-element-interactions": "error",

      // Autofocus on the first field of create/edit forms is intentional
      // (Batch 6) — not an accessibility defect for this app.
      "jsx-a11y/no-autofocus": "off",
    },
  },
];
