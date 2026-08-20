/**
 * The `?raw` import suffix, declared for TypeScript.
 *
 * Vite (and therefore Vitest) resolves `import source from "./x.tsx?raw"` to the file's
 * text. TypeScript knows nothing about the suffix, so without this declaration
 * `pnpm typecheck` fails on a line that runs correctly — which is the worst kind of red,
 * because the honest fix looks like deleting the test.
 *
 * It is used by exactly one test: `safe-html.test.tsx` reads its own component's source
 * to assert there is no `innerHTML` escape hatch anywhere in the file. Every behavioural
 * test in that suite could pass on a version with one `dangerouslySetInnerHTML` in a
 * branch nobody exercised, so the source itself is asserted.
 *
 * Typed as `string` and nothing else. This must never become a general-purpose module
 * shim — a wildcard that swallows unknown imports would turn a genuine typo into a
 * silently-typed `any`.
 */

declare module "*?raw" {
  const source: string;
  export default source;
}
