import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "e2e/screenshots"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
  {
    // Playwright fixtures call `use`, which is not a React hook.
    files: ["e2e/**/*.ts"],
    languageOptions: { globals: globals.node },
    rules: { "react-hooks/rules-of-hooks": "off", "no-empty-pattern": "off" },
  },
);
