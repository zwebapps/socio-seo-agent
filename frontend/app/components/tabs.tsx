"use client";

/**
 * Soft-UI tabs, built to the WAI-ARIA tabs pattern rather than to look like tabs.
 *
 * What a div-that-changes-colour cannot do, and this does:
 *
 * - **Roles and relationships.** `role="tablist"` wraps `role="tab"` buttons, each
 *   pointing at its `role="tabpanel"` through `aria-controls`, and each panel pointing
 *   back through `aria-labelledby`. A screen reader announces "tab 2 of 4, selected"
 *   instead of reading four unlabelled buttons and a slab of text.
 * - **One tab stop, arrows to move.** The tablist is a single stop in the tab order
 *   (roving `tabIndex`), so a keyboard user does not have to press Tab past every tab to
 *   reach the content. Left/Right move and wrap, Home/End jump to the ends. Focus moves
 *   with selection, which is the right choice here because switching panels is free — no
 *   fetch, no work — so automatic activation costs nothing and saves a keypress.
 * - **A focus ring that is visible.** Inherited from the global `:focus-visible` rule:
 *   a 2px `--accent` outline with an offset, on top of whatever the shadow is doing.
 *
 * The neumorphic trap, handled: a soft shadow measures around 1.2:1 against this
 * background, and WCAG 1.4.11 asks 3:1 for the boundary of a UI component. So every
 * UNSELECTED tab carries the `soft-edge` hairline (`--edge`, set to 3:1 against `--bg`)
 * as well as its shadow.
 *
 * **The selected tab is filled, not underlined.** It used to be `--surface-raised`
 * against a `--surface-sunken` track plus a 2px accent bar — and on this palette that
 * background swap is a couple of percent of lightness, so the bar was doing nearly all
 * the work and a glance did not land on it. It is now solid `--primary` with
 * `--primary-ink`, matching `SoftButton`'s primary variant and the `/developer` section
 * nav, measured at 9.22:1. No hairline on it, because a filled surface already has an
 * edge and the hairline would draw a line inside the fill.
 *
 * Two signals survive alongside the colour, because colour alone is not accessible: a
 * heavier font weight, and `aria-selected`, which is the only one a screen reader gets.
 */

import { useCallback, useId, useRef, type ReactNode } from "react";

export type TabSpec = {
  /** Stable key. Also forms the DOM ids, so it must be unique within one tablist. */
  id: string;
  label: string;
  /**
   * A small count beside the label. Rendered as text, never as a bare colour dot: it is
   * a second, non-colour way to see that (say) the SEO tab has 4 problems in it.
   */
  badge?: string | number;
  panel: ReactNode;
};

export function SoftTabs({
  tabs,
  active,
  onActivate,
  label,
}: {
  tabs: TabSpec[];
  active: string;
  onActivate: (id: string) => void;
  /** Names the tablist for a screen reader. Required — an unnamed tablist is a puzzle. */
  label: string;
}) {
  // useId keeps the DOM ids unique if two tablists ever share a page, and keeps them
  // stable between the server and client renders.
  const uid = useId();
  const buttons = useRef(new Map<string, HTMLButtonElement | null>());

  const tabId = (id: string) => `${uid}-tab-${id}`;
  const panelId = (id: string) => `${uid}-panel-${id}`;

  const move = useCallback(
    (to: number) => {
      const next = tabs[(to + tabs.length) % tabs.length];
      if (!next) return;
      onActivate(next.id);
      // Focus follows selection, so the arrow key does not leave focus behind on a tab
      // that is no longer the selected one.
      buttons.current.get(next.id)?.focus();
    },
    [tabs, onActivate],
  );

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const current = tabs.findIndex((tab) => tab.id === active);
      if (current < 0) return;

      switch (event.key) {
        case "ArrowRight":
        case "ArrowDown":
          event.preventDefault();
          move(current + 1);
          break;
        case "ArrowLeft":
        case "ArrowUp":
          event.preventDefault();
          move(current - 1);
          break;
        case "Home":
          event.preventDefault();
          move(0);
          break;
        case "End":
          event.preventDefault();
          move(tabs.length - 1);
          break;
        default:
          break;
      }
    },
    [tabs, active, move],
  );

  const current = tabs.find((tab) => tab.id === active) ?? tabs[0];

  return (
    <div>
      <div
        role="tablist"
        aria-label={label}
        onKeyDown={onKeyDown}
        className="soft-sunken flex flex-wrap gap-1.5 p-1.5"
        style={{ borderRadius: "var(--r-pill)" }}
      >
        {tabs.map((tab) => {
          const selected = tab.id === current?.id;
          return (
            <button
              key={tab.id}
              ref={(node) => {
                buttons.current.set(tab.id, node);
              }}
              type="button"
              role="tab"
              id={tabId(tab.id)}
              aria-selected={selected}
              aria-controls={panelId(tab.id)}
              // Roving tabindex: the tablist is ONE tab stop, arrows do the rest.
              tabIndex={selected ? 0 : -1}
              onClick={() => onActivate(tab.id)}
              // The selected tab is FILLED, matching the `/developer` section nav and
              // `SoftButton`'s primary variant. It used to be `--surface-raised` against
              // a `--surface-sunken` track plus a 2px bar — a couple of percent of
              // lightness, so the bar was doing all the work and a glance did not land
              // on it. Measured: white on `--primary` is 9.22:1.
              className={`soft-press relative flex items-center gap-2 px-3.5 py-2 text-sm ${
                // No hairline on the filled pill: a filled surface already has an edge.
                selected ? "font-semibold" : "soft-edge font-medium"
              }`}
              style={{
                borderRadius: "var(--r-pill)",
                ...(selected
                  ? {
                      background: "var(--primary)",
                      color: "var(--primary-ink)",
                      boxShadow:
                        "-4px -4px 10px var(--shadow-light), 5px 5px 14px var(--shadow-dark)",
                    }
                  : { color: "var(--text-muted)", background: "transparent" }),
              }}
            >
              <span>{tab.label}</span>
              {tab.badge !== undefined && tab.badge !== "" && (
                <span
                  className="tabular soft-flat px-1.5 text-[10px] font-semibold"
                  style={{
                    borderRadius: "var(--r-pill)",
                    // On the filled pill the badge sits on `--primary`, so the accent
                    // orange would be 2.1:1 against it. `--primary-ink` is the only
                    // colour that is legible there.
                    color: selected ? "var(--primary-ink)" : "var(--text-muted)",
                  }}
                >
                  {tab.badge}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={panelId(tab.id)}
          aria-labelledby={tabId(tab.id)}
          hidden={tab.id !== current?.id}
          // Focusable so that Tab out of the tablist lands ON the panel: a panel that
          // starts with plain text has nothing else to receive focus, and a keyboard
          // user would otherwise skip straight past the content to the next control.
          tabIndex={0}
          className="mt-5"
        >
          {tab.id === current?.id ? tab.panel : null}
        </div>
      ))}
    </div>
  );
}
