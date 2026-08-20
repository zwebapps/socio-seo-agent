"use client";

/**
 * The `/developer` section nav, and the "you are here" it was missing.
 *
 * Every one of these four pills rendered identically, whichever screen you were on:
 * no `aria-current`, no weight change, no colour. So the nav told you where you could
 * go and never where you were — which is the one thing a section nav exists for, and is
 * why this is a client component while the layout around it stays on the server. The
 * layout's comment used to say the nav "needs no state"; knowing which page you are on
 * IS state, and `usePathname` is the smallest way to have it.
 *
 * **Three signals for the current section, not one.** `aria-current="page"` for a screen
 * reader; the recessed `soft-sunken` treatment plus a heavier weight for everyone; and
 * an accent bar under the label. Colour alone would fail WCAG 1.4.1, and on this
 * neumorphic palette a background swap between `--surface-raised` and `--bg` is a couple
 * of percent of lightness — technically a difference and not a perceptible one, which is
 * the same complaint the review tabs earn.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

const SECTIONS = [
  { href: "/developer/models", label: "Model routing" },
  { href: "/developer/runtime", label: "Sampling & prompts" },
  { href: "/developer/tools", label: "Tool access" },
  { href: "/developer/cost", label: "Cost" },
] as const;

export function DeveloperNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Developer settings" className="mx-auto max-w-5xl px-6 pt-10">
      <ul className="flex flex-wrap gap-2">
        {SECTIONS.map((section) => {
          // `startsWith` rather than equality, so a future nested route
          // (`/developer/tools/harvest`) keeps its parent highlighted instead of
          // silently un-highlighting the whole nav.
          const current = pathname === section.href || pathname.startsWith(`${section.href}/`);
          return (
            <li key={section.href}>
              <Link
                href={section.href}
                
                aria-current={current ? "page" : undefined}
                className={`soft-edge soft-press relative inline-block px-4 py-2 text-sm ${
                  current ? "soft-sunken font-semibold" : "soft-raised font-medium"
                }`}
                style={{
                  borderRadius: "var(--r-pill)",
                  color: current ? "var(--text)" : "var(--text-muted)",
                }}
              >
                {section.label}
                {current && (
                  <span
                    aria-hidden
                    className="absolute bottom-[3px] left-1/2 h-[2px] w-5 -translate-x-1/2"
                    style={{ background: "var(--accent)", borderRadius: 2 }}
                  />
                )}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
