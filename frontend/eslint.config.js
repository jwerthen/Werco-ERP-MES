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

      // Icon-only / unnamed controls must carry an accessible name — burned
      // down to zero; now ENFORCED. Enforced with the plugin's RECOMMENDED
      // options (the bare rule's empty `ignoreElements` spuriously flags inputs,
      // textareas, and table rows that already get a name elsewhere). Genuine
      // controls (icon buttons, selects) get an `aria-label`.
      "jsx-a11y/control-has-associated-label": [
        "error",
        {
          ignoreElements: ["audio", "canvas", "embed", "input", "textarea", "tr", "video"],
          ignoreRoles: [
            "grid",
            "listbox",
            "menu",
            "menubar",
            "radiogroup",
            "row",
            "tablist",
            "toolbar",
            "tree",
            "treegrid",
          ],
          includeRoles: ["alert", "dialog"],
        },
      ],

      // Form <label>s associated with their control — burned down to zero; now
      // ENFORCED. Use the <FormField> primitive (label↔control id wiring) for
      // create/edit form fields, or htmlFor+id for inline/filter controls.
      "jsx-a11y/label-has-associated-control": "error",

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
