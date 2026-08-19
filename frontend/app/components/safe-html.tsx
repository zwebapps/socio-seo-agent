"use client";

/**
 * Renders the draft's markup for READING, without ever handing it to the browser as HTML.
 *
 * Why this exists rather than `dangerouslySetInnerHTML`. The draft is written by a
 * language model, and that model's prompt contains text harvested from web pages the
 * business does not control — the agent fences it as untrusted (see
 * `agents/nodes/prompts.py`) precisely because a crawled page can carry instructions
 * aimed at the model. Prompt fencing reduces the chance the model complies; it cannot
 * guarantee it. So the draft must be treated as attacker-influenced content, and
 * injecting attacker-influenced markup into the admin UI — the one origin holding the
 * owner's session cookie — is stored XSS with extra steps.
 *
 * How it is safe, stated as a property rather than as a promise:
 *
 * 1. The markup is parsed with `DOMParser`, which builds an INERT document. Scripts in a
 *    document created this way never execute, and no node is ever attached to the live
 *    page.
 * 2. That tree is then walked and rebuilt as REACT ELEMENTS from a tag allowlist. There
 *    is no `innerHTML` anywhere in this file, so text can only ever arrive as a React
 *    child — which React escapes. Injected `<script>`, `onerror=`, `<iframe>` and friends
 *    are not "sanitised away"; they have no path to execution because markup is never
 *    interpreted as markup a second time.
 * 3. Every attribute is DROPPED except `href` on an anchor, and that one is checked
 *    against a scheme allowlist, so `javascript:` and `data:` URLs cannot survive. This
 *    is the only place where an allowlist bug could bite, which is why it is three lines
 *    and has a single job.
 *
 * Nothing is silently swallowed: an element outside the allowlist is unwrapped rather
 * than deleted, so its TEXT still reaches the reader. That matters for a review screen —
 * the owner has to see everything that would be published, and content vanishing between
 * the draft and the panel would be the worst possible failure mode here. The exception is
 * the small set of elements whose text is not content (`script`, `style`, `template`),
 * which are dropped whole.
 *
 * A "Source" view sits beside this in the review tab, because for publishing decisions
 * the owner needs the actual markup too. That one is plain escaped text in a `<pre>`.
 */

import { Fragment, useMemo, type ReactNode } from "react";

/** Elements kept as elements. Deliberately small: this is prose, not a page builder. */
const ALLOWED = new Map<string, keyof React.JSX.IntrinsicElements>([
  ["H1", "h2"], // demoted: the panel's own heading is the h1 on this screen
  ["H2", "h3"],
  ["H3", "h4"],
  ["H4", "h5"],
  ["P", "p"],
  ["UL", "ul"],
  ["OL", "ol"],
  ["LI", "li"],
  ["STRONG", "strong"],
  ["B", "strong"],
  ["EM", "em"],
  ["I", "em"],
  ["BLOCKQUOTE", "blockquote"],
  ["CODE", "code"],
  ["BR", "br"],
  ["A", "a"],
]);

/** Elements whose text is not content. Dropped whole, children included. */
const DROP_ENTIRELY = new Set(["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "IFRAME", "OBJECT"]);

/** The only schemes an anchor may carry. Anything else loses its href. */
const SAFE_SCHEMES = ["http:", "https:", "mailto:"];

const CLASS: Partial<Record<string, string>> = {
  h2: "mt-6 mb-2 text-xl font-semibold tracking-tight",
  h3: "mt-5 mb-2 text-base font-semibold",
  h4: "mt-4 mb-1.5 text-sm font-semibold",
  h5: "mt-4 mb-1.5 text-sm font-semibold",
  p: "my-3 text-sm leading-relaxed",
  ul: "my-3 ml-5 list-disc space-y-1 text-sm",
  ol: "my-3 ml-5 list-decimal space-y-1 text-sm",
  li: "leading-relaxed",
  blockquote: "my-3 border-l-2 pl-3 text-sm italic",
  code: "px-1 text-[13px]",
  a: "underline",
};

function safeHref(raw: string | null): string | undefined {
  if (!raw) return undefined;
  try {
    // Resolved against a base so a relative href parses; the scheme check then runs on
    // the resolved URL, which is what a click would actually follow.
    const url = new URL(raw, "https://example.invalid/");
    return SAFE_SCHEMES.includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

function toReact(node: Node, key: number): ReactNode {
  if (node.nodeType === 3) return node.textContent;
  if (node.nodeType !== 1) return null; // comments and everything else: dropped

  const element = node as Element;
  const tag = element.tagName.toUpperCase();

  if (DROP_ENTIRELY.has(tag)) return null;

  const children = Array.from(element.childNodes).map((child, index) => toReact(child, index));
  const mapped = ALLOWED.get(tag);

  // Not on the allowlist: unwrap it. The element goes, the words stay.
  if (!mapped) return <Fragment key={key}>{children}</Fragment>;

  if (mapped === "br") return <br key={key} />;

  if (mapped === "a") {
    const href = safeHref(element.getAttribute("href"));
    // An anchor whose href was refused becomes plain text rather than a dead link, so the
    // reader is not invited to click something that goes nowhere.
    if (!href) return <Fragment key={key}>{children}</Fragment>;
    return (
      <a
        key={key}
        href={href}
        // The draft's outbound links point at pages we do not control.
        rel="noopener noreferrer nofollow ugc"
        target="_blank"
        className={CLASS.a}
        style={{ color: "var(--primary)" }}
      >
        {children}
      </a>
    );
  }

  const Tag = mapped;
  const style =
    mapped === "blockquote"
      ? { borderColor: "var(--edge)", color: "var(--text-muted)" }
      : mapped === "code"
        ? { background: "var(--surface-sunken)", borderRadius: 4 }
        : undefined;

  return (
    <Tag key={key} className={CLASS[mapped]} style={style}>
      {children}
    </Tag>
  );
}

export function SafeHtml({ html }: { html: string }) {
  const rendered = useMemo(() => {
    // `DOMParser` is a browser API. This component only ever receives fetched content, so
    // it cannot run during prerender in practice — the guard is here so that stays true
    // by construction rather than by luck.
    if (typeof DOMParser === "undefined") return null;
    const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
    return Array.from(doc.body.childNodes).map((node, index) => toReact(node, index));
  }, [html]);

  if (rendered === null) {
    return (
      <pre className="overflow-x-auto text-xs" style={{ color: "var(--text-muted)" }}>
        {html}
      </pre>
    );
  }

  return <div>{rendered}</div>;
}
