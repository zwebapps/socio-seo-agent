/**
 * Vitest for the web half.
 *
 * `jsdom`, because everything worth testing here is a client component or a helper a
 * client component calls — `runs-api.ts` says so in its own module note, and a `node`
 * environment would mean testing the rendering code with no DOM to render into.
 *
 * The `@/*` alias is NOT optional: `tsconfig.json` declares it and the app imports
 * through it (`@/app/components/soft`), so without the same mapping here every such
 * import fails to resolve and the suite is red for a reason that has nothing to do with
 * the code under test.
 *
 * `.next` is excluded explicitly. A build leaves `.next/types/validator.ts` and a whole
 * vendored `node_modules` tree behind, and Vitest's default `include` would happily
 * collect from them.
 *
 * `.mts` rather than `.ts` because this file uses `import.meta.url`. Vite still loads a
 * `.ts` config as CommonJS by default and warns that it will not forever; the extension
 * is the documented fix, and a warning printed on every CI run is a warning nobody reads.
 */

import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    include: ["app/**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**"],
    setupFiles: ["./vitest.setup.ts"],
    // Every test must start from a clean DOM and clean mocks. A leaked `fetch` stub is
    // how a test that could never have failed gets written.
    restoreMocks: true,
    coverage: {
      provider: "v8",
      include: ["app/**/*.{ts,tsx}"],
      exclude: ["app/**/*.test.{ts,tsx}"],
    },
  },
});
