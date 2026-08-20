"use client";

/**
 * One copy-to-clipboard control, shared by the review tabs and the export pack.
 *
 * Lifted out of `review.tsx` when the export surface needed the same control: two copies
 * of a clipboard button is two places for the "no clipboard" branch to be forgotten, and
 * that branch is not exotic — `navigator.clipboard` is absent on any origin that is not
 * HTTPS or localhost, which is exactly where this app is demoed from.
 *
 * Three details that are not decoration:
 *
 * - **The outcome is announced, not only coloured.** `aria-live="polite"` on the status
 *   text, because a green word appearing next to a button is no signal at all to a
 *   screen-reader user, and "copied" is the only confirmation this control gives.
 * - **A failure says what to do instead.** "select and copy manually" is actionable;
 *   silence looks like a button that does nothing, and the user's next move is to click
 *   it again.
 * - **`label` is required.** Several of these sit on one screen, and a control called
 *   "Copy" five times is five identically named controls to anyone navigating by name.
 */

import { useCallback, useState } from "react";
import { SoftButton } from "../../components/soft";

export function CopyButton({
  text,
  label,
  caption = "Copy",
}: {
  /** Exactly what lands on the clipboard. */
  text: string;
  /** The accessible name — must say WHAT is copied, not just "copy". */
  label: string;
  /** Visible text. Defaults to "Copy" where the surrounding heading already says what. */
  caption?: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  const copy = useCallback(async () => {
    try {
      // Absent over plain HTTP on a non-localhost origin, so its absence is a normal
      // state to handle rather than an error to swallow.
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 2200);
  }, [text]);

  return (
    <span className="flex shrink-0 items-center gap-2">
      <span className="text-[11px] font-semibold" aria-live="polite" style={{ color: "var(--ok)" }}>
        {state === "copied" && "copied"}
        {state === "failed" && (
          <span style={{ color: "var(--warn)" }}>select and copy manually</span>
        )}
      </span>
      <SoftButton onClick={() => void copy()} ariaLabel={label}>
        {caption}
      </SoftButton>
    </span>
  );
}
