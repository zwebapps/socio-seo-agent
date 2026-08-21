"use client";

/**
 * The sidebar. The navigation this app has never had.
 *
 * Fifteen screens existed and the only way between them was a list of links on the
 * dashboard headed "Elsewhere", plus a "back to dashboard" link on each page. So every
 * journey went through home, nothing indicated where you were, and two screens
 * (`/business`, `/content`) had to be discovered from a paragraph.
 *
 * Three decisions worth naming.
 *
 * **It renders nothing for a visitor.** A nav pointing at fifteen screens that all
 * refuse you is not navigation, it is a list of closed doors — and the login page's job
 * is to be the one thing on screen. Nothing renders while the session is still loading
 * either: a sidebar that appears a beat after the page reads as a layout bug.
 *
 * **The operator section is hidden, not disabled.** `docs/ARCHITECTURE.md` §14 states
 * the rule — the role is checked server-side on every `/api/v1/admin/*` call, and
 * `/developer` renders for anyone while showing a 403 card. So hiding the links is
 * cosmetic by design; it removes four dead ends from an owner's sidebar without
 * pretending to be a security control.
 *
 * **It is a `<nav>` of real links with `aria-current`, not a set of click handlers.**
 * That gives keyboard traversal, open-in-new-tab, and "where am I" to a screen reader
 * for free. The current item is marked semantically as well as visually, because colour
 * alone is not an indicator.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { isOperator, useSession } from "@/app/components/session-context";

type Item = { href: string; label: string; hint: string };
type Group = { title: string; items: Item[] };

/**
 * The groups, in the order an owner works through them.
 *
 * "Do" before "know" before "set up": the dashboard and the two output screens first,
 * because those are visited daily; the material and the wiring below, because those are
 * visited at setup and then rarely.
 */
const GROUPS: readonly Group[] = [
  {
    title: "Work",
    items: [
      { href: "/", label: "Dashboard", hint: "Start a run, see recent ones" },
      { href: "/content", label: "Content", hint: "Posts per channel" },
      { href: "/runs", label: "Runs", hint: "Every run and what it reached" },
      { href: "/leads", label: "Leads", hint: "Who got in touch" },
    ],
  },
  {
    title: "Your business",
    items: [
      { href: "/business", label: "Business profile", hint: "Website, voice, claims" },
      { href: "/documents", label: "Documents", hint: "What the agent may quote" },
      { href: "/memory", label: "Preferences", hint: "Carried into every run" },
      { href: "/connections", label: "Connections", hint: "Platform accounts" },
    ],
  },
];

const OPERATOR: Group = {
  title: "Operator",
  items: [
    { href: "/developer/models", label: "Model routing", hint: "Which model serves each task" },
    { href: "/developer/runtime", label: "Sampling", hint: "Temperature, prompts" },
    { href: "/developer/tools", label: "Tool access", hint: "Per-node kill switches" },
    { href: "/developer/cost", label: "Cost", hint: "Real model spend" },
  ],
};

export function AppNav() {
  const { state } = useSession();
  const pathname = usePathname();

  // Nothing for a visitor, and nothing while the answer is in flight. See the module
  // note: a sidebar that appears a beat late reads as a layout bug, and one full of
  // links that refuse you is not navigation.
  if (state.kind !== "signed-in") return null;

  const groups = isOperator(state) ? [...GROUPS, OPERATOR] : GROUPS;

  return (
    <nav
      aria-label="Sections"
      className="shrink-0 border-r px-4 py-6 lg:w-[15.5rem]"
      style={{ borderColor: "var(--edge)" }}
    >
      <p
        className="px-2 text-[11px] font-semibold uppercase tracking-[0.18em]"
        style={{ color: "var(--accent)" }}
      >
        Growth agent
      </p>

      <div className="mt-6 space-y-6">
        {groups.map((group) => (
          <div key={group.title}>
            <h2
              className="px-2 text-[10px] font-semibold uppercase tracking-wider"
              style={{ color: "var(--text-faint)" }}
            >
              {group.title}
            </h2>
            <ul className="mt-2 space-y-0.5">
              {group.items.map((item) => (
                <li key={item.href}>
                  <NavLink item={item} current={isCurrent(pathname, item.href)} />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}

function NavLink({ item, current }: { item: Item; current: boolean }) {
  return (
    <Link
      href={item.href}
      // Semantic, not just coloured. A screen reader announces "current page" from this;
      // colour alone would tell a screen-reader user nothing and a colour-blind user
      // very little.
      aria-current={current ? "page" : undefined}
      title={item.hint}
      className="block px-2 py-1.5 text-sm"
      style={{
        borderRadius: "var(--r-sm)",
        background: current ? "var(--primary)" : "transparent",
        color: current ? "var(--primary-ink)" : "var(--text)",
        fontWeight: current ? 600 : 400,
      }}
    >
      {item.label}
    </Link>
  );
}

/**
 * Whether `href` is the section the pathname is in.
 *
 * Exported for its own test. The dashboard is the case that needs the special rule: as a
 * prefix, `/` matches every path in the app, so it has to be an exact match or every
 * screen highlights "Dashboard" as well as itself.
 */
export function isCurrent(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  if (href === "/") return pathname === "/";
  // A prefix match, so `/runs/{id}` still highlights "Runs" — a detail page that
  // highlights nothing looks like a screen outside the app.
  return pathname === href || pathname.startsWith(`${href}/`);
}
