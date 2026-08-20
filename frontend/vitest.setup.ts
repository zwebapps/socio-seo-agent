/**
 * Test setup.
 *
 * The jest-dom import is what teaches TypeScript about `toBeInTheDocument` and friends —
 * the `/vitest` entry point augments Vitest's own `Assertion` interface, so the matchers
 * type-check without a `types` entry in `tsconfig.json` and without loosening anything.
 *
 * `cleanup` after every test because these are React components with timers and effects
 * in them: a mounted tree left behind keeps polling into the next test and the failure
 * surfaces somewhere unrelated.
 */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
