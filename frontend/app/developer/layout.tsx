/**
 * Chrome shared by every `/developer` screen.
 *
 * A server component, deliberately: it renders navigation and nothing else, so it needs
 * no state and no client bundle. The pages themselves are `"use client"` because their
 * API calls MUST run in the browser — the Origin-CSRF middleware refuses a cookie-bearing
 * write with no `Origin` header, and `fetch` from a server component sends none. See the
 * note in `app/lib/admin-api.ts`.
 *
 * The nav is a `<nav>` with an `aria-label`, not a row of styled links: there are two
 * landmark-level link groups on these pages once the error card renders a "sign in"
 * link, and an unlabelled one is announced as just "navigation".
 *
 * No role check here. The gate is server-side on the API — every route under
 * `/api/v1/admin/*` carries `require_admin`, and the pages render the 403 as an
 * explanatory card. Duplicating the check in the frontend would put an authorisation
 * decision in a place that cannot enforce it, and would be a second thing to keep in
 * step with the real one.
 */

import Link from "next/link";

const SECTIONS = [
  { href: "/developer/models", label: "Model routing" },
  { href: "/developer/runtime", label: "Sampling & prompts" },
  { href: "/developer/tools", label: "Tool access" },
  { href: "/developer/cost", label: "Cost" },
] as const;

export default function DeveloperLayout({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <nav aria-label="Developer settings" className="mx-auto max-w-5xl px-6 pt-10">
        <ul className="flex flex-wrap gap-2">
          {SECTIONS.map((section) => (
            <li key={section.href}>
              <Link
                href={section.href}
                className="soft-raised soft-edge soft-press inline-block px-4 py-2 text-sm font-medium"
                style={{ borderRadius: "var(--r-pill)", color: "var(--text)" }}
              >
                {section.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      {children}
    </div>
  );
}
