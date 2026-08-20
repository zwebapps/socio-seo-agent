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
 * **The current section is FILLED, not underlined.** The first version used a recessed
 * surface plus a thin accent bar, and on this neumorphic light palette that was too
 * quiet to read at a glance: `--surface-raised` against `--bg` is a couple of percent of
 * lightness, so the 2px bar was doing nearly all the work. A solid `--primary` fill with
 * `--primary-ink` on it is unmistakable, and it is the same treatment `SoftButton`'s
 * `variant="primary"` already uses for the app's most emphatic control — so "current"
 * looks like something that belongs here rather than like a new idiom.
 *
 * Three signals still, because colour alone fails WCAG 1.4.1: the fill, a heavier
 * weight, and `aria-current="page"` — which is the only one a screen reader gets.
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
                className={`soft-press inline-block px-4 py-2 text-sm ${
                  // No `soft-edge` on the filled pill: a filled surface already has an
                  // edge, and the hairline would draw a line inside it. Same reasoning
                  // as `SoftButton`'s filled variants.
                  current ? "font-semibold" : "soft-raised soft-edge font-medium"
                }`}
                style={{
                  borderRadius: "var(--r-pill)",
                  ...(current
                    ? {
                        background: "var(--primary)",
                        color: "var(--primary-ink)",
                        boxShadow:
                          "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
                      }
                    : { color: "var(--text-muted)" }),
                }}
              >
                {section.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
