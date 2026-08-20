/**
 * The page container, and the reason there are two of them.
 *
 * Every screen was centred at `max-w-3xl` (768px) or `max-w-5xl` (1024px), which on a
 * 1440-or-wider desktop left a third of the window empty on each side — for an
 * operator tool whose content is lists, tables and side-by-side panels, that is wasted
 * space rather than restraint.
 *
 * But "remove the max-width" is the wrong correction, and it is worth being explicit
 * about why: a line of prose 1800px wide is measurably harder to read than one at 70
 * characters, because the eye loses its place on the return sweep. So the fix splits
 * the two jobs the old single container was doing:
 *
 * - `Shell` is the LAYOUT width. It fills the viewport up to 1800px, which is
 *   effectively edge-to-edge on a 1920 screen and stops an ultrawide from stretching a
 *   two-column grid to absurdity. Padding grows with the breakpoint so the content
 *   never touches the edge.
 * - `Prose` is the READING width, about 70 characters. Paragraphs, help text and
 *   explanations go in one of these regardless of how wide the shell is.
 *
 * A page that uses `Shell` and forgets `Prose` gets full-width paragraphs, which is the
 * failure mode to watch for in review.
 */

import type { ReactNode } from "react";

export function Shell({
  children,
  className = "",
  as: Tag = "main",
}: {
  children: ReactNode;
  className?: string;
  /** `main` for a page, `div` for a section inside one. */
  as?: "main" | "div" | "section";
}) {
  return (
    <Tag className={`mx-auto w-full max-w-[1800px] px-6 lg:px-10 xl:px-14 ${className}`}>
      {children}
    </Tag>
  );
}

/**
 * A constrained reading measure. ~70 characters at the body size.
 *
 * `max-w-[70ch]` rather than a pixel width, because it is a statement about the TEXT:
 * it stays right if the font size changes, and a pixel value silently stops being 70
 * characters the moment anyone touches the type scale.
 */
export function Prose({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`max-w-[70ch] ${className}`}>{children}</div>;
}
