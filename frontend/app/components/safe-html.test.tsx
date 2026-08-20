/**
 * The XSS boundary. This is the highest-value test file in the frontend.
 *
 * The draft rendered here is written by a language model whose prompt contains text
 * harvested from pages the business does not control. The agent fences that text as
 * untrusted, but fencing reduces the chance a model complies with an injected
 * instruction — it cannot guarantee it. So the draft is attacker-influenced content, and
 * the screen showing it is the one origin holding the owner's session cookie.
 *
 * `SafeHtml` claims three properties in its docstring. Prose is not a control, so each
 * one is asserted here:
 *
 *   1. markup is never interpreted as markup a second time (no `innerHTML`, ever);
 *   2. every attribute is dropped except a scheme-checked `href` on an anchor;
 *   3. nothing is silently swallowed — an unknown element is unwrapped, so its TEXT
 *      still reaches a reviewer who has to see everything that would be published.
 *
 * The payloads below are real ones, not invented shapes: the classic `img/onerror`, the
 * `javascript:` and `data:` hrefs, the case-varied `<ScRiPt>`, the SVG vector, and the
 * `noscript` mutation payload that defeats naive regex sanitisers.
 */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SafeHtml } from "./safe-html";

declare global {
  // eslint-disable-next-line no-var
  var __xss_fired__: boolean | undefined;
}

beforeEach(() => {
  globalThis.__xss_fired__ = undefined;
});

afterEach(() => {
  delete globalThis.__xss_fired__;
  vi.unstubAllGlobals();
});

/** Did anything in the payload actually run? */
function fired(): boolean {
  return globalThis.__xss_fired__ === true;
}

const FIRE = "globalThis.__xss_fired__ = true";

describe("nothing in the draft executes", () => {
  it("does not run a script tag", () => {
    render(<SafeHtml html={`<p>before</p><script>${FIRE}</script><p>after</p>`} />);

    expect(fired()).toBe(false);
    // Dropped WHOLE, not unwrapped: script text is not content, and rendering it would
    // put the payload on screen for a reader to copy somewhere less careful.
    expect(screen.queryByText(new RegExp(FIRE.slice(0, 20)))).toBeNull();
    expect(screen.getByText("before")).toBeInTheDocument();
    expect(screen.getByText("after")).toBeInTheDocument();
  });

  it("does not run a script whose tag name is case-varied", () => {
    // A sanitiser matching on a lowercase literal misses this. The component uppercases
    // the tag name before every decision, which is why it does not.
    render(<SafeHtml html={`<ScRiPt>${FIRE}</ScRiPt><p>kept</p>`} />);

    expect(fired()).toBe(false);
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("does not run an inline handler on an element it keeps", () => {
    render(<SafeHtml html={`<p onclick="${FIRE}" onmouseover="${FIRE}">click me</p>`} />);

    const paragraph = screen.getByText("click me");
    paragraph.click();

    expect(fired()).toBe(false);
    expect(paragraph.getAttribute("onclick")).toBeNull();
  });

  it("does not run an error handler on an element it unwraps", () => {
    // The classic payload. `img` is not on the allowlist, so it is unwrapped — and the
    // attribute never travels either way, because attributes are dropped rather than
    // filtered.
    render(<SafeHtml html={`<p>a</p><img src="x" onerror="${FIRE}">`} />);

    expect(fired()).toBe(false);
    expect(document.querySelector("img")).toBeNull();
  });

  it("does not run the svg vector", () => {
    render(<SafeHtml html={`<svg><script>${FIRE}</script></svg><p>kept</p>`} />);

    expect(fired()).toBe(false);
    expect(document.querySelector("svg")).toBeNull();
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("survives the noscript mutation payload that defeats regex sanitisers", () => {
    // Inside `noscript`, a parser that has scripting disabled treats the contents as
    // markup while one that does not treats it as text — which is how the `img` escapes
    // in a naive pipeline. This component never re-parses, so the branch does not exist.
    render(
      <SafeHtml
        html={`<noscript><p title="</noscript><img src=x onerror=${FIRE}>">t</p></noscript><p>after</p>`}
      />,
    );

    expect(fired()).toBe(false);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("after")).toBeInTheDocument();
  });

  it("renders already-escaped markup as text", () => {
    // Proof of the underlying mechanism: content reaches the DOM as a React child, and
    // React escapes it. If this ever renders an element, the whole file's claim is void.
    //
    // The braces are load-bearing. Written as a JSX attribute literal —
    // `html="<p>&lt;script&gt;</p>"` — JSX decodes the entities itself, so the component
    // would receive a REAL script tag and this would silently become a duplicate of the
    // first test in this file rather than the entity test it claims to be. It passed that
    // way, for the wrong reason. A JS expression is the only form that delivers the
    // characters `&`, `l`, `t`, `;` to the component.
    render(<SafeHtml html={"<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"} />);

    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });
});

describe("anchors: the one attribute that survives", () => {
  it("keeps an http link and marks it untrusted", () => {
    render(<SafeHtml html='<p><a href="https://example.com/a">link</a></p>' />);

    const link = screen.getByRole("link", { name: "link" });
    expect(link).toHaveAttribute("href", "https://example.com/a");
    // These outbound links point at pages nobody here controls.
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("nofollow");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("keeps a mailto link", () => {
    render(<SafeHtml html='<p><a href="mailto:hi@example.com">mail</a></p>' />);

    expect(screen.getByRole("link", { name: "mail" })).toHaveAttribute(
      "href",
      "mailto:hi@example.com",
    );
  });

  it("refuses a javascript: href and leaves the words behind", () => {
    // Not a dead link: an anchor whose href was refused becomes plain text, so a reader
    // is never invited to click something that goes nowhere.
    render(<SafeHtml html={`<p><a href="javascript:${FIRE}">looks like a link</a></p>`} />);

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("looks like a link")).toBeInTheDocument();
    expect(fired()).toBe(false);
  });

  it("refuses a data: href", () => {
    // `data:text/html` is a same-origin-ish execution vector in some browsers, which is
    // why the check is an allowlist of schemes rather than a blocklist of one.
    render(
      <SafeHtml html='<p><a href="data:text/html,<script>1</script>">d</a></p>' />,
    );

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("d")).toBeInTheDocument();
  });

  it("refuses a scheme smuggled past a naive check", () => {
    // `JaVaScRiPt:` and a leading-whitespace variant both normalise through `new URL`,
    // which is the reason the check resolves the URL instead of string-matching it.
    render(<SafeHtml html={'<p><a href=" JaVaScRiPt:alert(1)">x</a></p>'} />);

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("x")).toBeInTheDocument();
  });

  it("drops every other attribute from an anchor it keeps", () => {
    render(
      <SafeHtml
        html={`<p><a href="https://example.com" id="pwn" style="position:fixed" onclick="${FIRE}">a</a></p>`}
      />,
    );

    const link = screen.getByRole("link", { name: "a" });
    expect(link.getAttribute("id")).toBeNull();
    expect(link.getAttribute("onclick")).toBeNull();
    // `style` is set by the component itself, so assert the INJECTED declaration is gone
    // rather than that there is no style at all.
    expect(link.getAttribute("style") ?? "").not.toContain("position");
  });
});

describe("nothing is silently swallowed", () => {
  it("unwraps an unknown element and keeps its words", () => {
    // The property that matters most on a REVIEW screen: the owner has to see everything
    // that would be published, and content vanishing between the draft and the panel
    // would be the worst failure this component could have.
    render(<SafeHtml html="<section><p>kept text</p></section>" />);

    expect(screen.getByText("kept text")).toBeInTheDocument();
  });

  it("keeps the text of a div nobody allowlisted", () => {
    render(<SafeHtml html="<div>bare words</div>" />);

    expect(screen.getByText("bare words")).toBeInTheDocument();
  });

  it("drops style and template content entirely, because it is not content", () => {
    render(
      <SafeHtml html="<style>.x{color:red}</style><template>hidden</template><p>shown</p>" />,
    );

    expect(screen.queryByText(/color:red/)).toBeNull();
    expect(screen.queryByText("hidden")).toBeNull();
    expect(screen.getByText("shown")).toBeInTheDocument();
  });

  it("drops comments", () => {
    render(<SafeHtml html="<!-- a note --><p>visible</p>" />);

    expect(document.body.textContent).not.toContain("a note");
    expect(screen.getByText("visible")).toBeInTheDocument();
  });
});

describe("the reading shape", () => {
  it("demotes the draft's h1, because the screen already has one", () => {
    // Two h1s on a page is a real a11y defect and the draft cannot know it is being
    // embedded, so the mapping fixes it here.
    render(<SafeHtml html="<h1>Draft title</h1>" />);

    expect(screen.getByRole("heading", { level: 2, name: "Draft title" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  });

  it("normalises b and i onto strong and em", () => {
    render(<SafeHtml html="<p><b>bold</b> <i>italic</i></p>" />);

    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("italic").tagName).toBe("EM");
  });

  it("keeps a list as a list, so structure survives review", () => {
    render(<SafeHtml html="<ul><li>one</li><li>two</li></ul>" />);

    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("does not crash on malformed markup", () => {
    // A model emits unclosed tags. `DOMParser` repairs them; the point of the test is
    // that a repair never becomes an exception on the review screen.
    render(<SafeHtml html="<p>open <strong>bold <em>both</p><ul><li>stray" />);

    expect(screen.getByText(/open/)).toBeInTheDocument();
  });

  it("renders nothing for empty markup rather than throwing", () => {
    const { container } = render(<SafeHtml html="" />);

    expect(container.textContent).toBe("");
  });
});

describe("the no-DOMParser fallback", () => {
  it("shows the markup as escaped text instead of rendering it", () => {
    // Server prerender. The fallback must not become a second injection path, so it is a
    // `<pre>` of plain text — and the payload has to be visible AS TEXT, which is the
    // proof it was not interpreted.
    vi.stubGlobal("DOMParser", undefined);

    render(<SafeHtml html={`<script>${FIRE}</script><p>hi</p>`} />);

    expect(fired()).toBe(false);
    expect(screen.getByText(`<script>${FIRE}</script><p>hi</p>`)).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });
});

describe("the structural guarantee", () => {
  it("contains no innerHTML-style escape hatch", async () => {
    // The file's central claim is that markup is never interpreted as markup a second
    // time. Every behavioural test above could pass on a version that had one
    // `dangerouslySetInnerHTML` in a branch nobody exercised, so the source itself is
    // asserted. Cheap, and it fails the moment somebody reaches for the shortcut.
    const source = await import("./safe-html.tsx?raw").then((m) => m.default as string);

    // Comments are stripped first, because the component's own docstring explains at
    // length why it does NOT use `dangerouslySetInnerHTML` — and a scan that trips over
    // the prose arguing for the property is a scan that can only be satisfied by deleting
    // the explanation. What is asserted is the CODE.
    const code = source
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/.*$/gm, "$1");

    for (const hatch of [
      "dangerouslySetInnerHTML",
      "innerHTML",
      "outerHTML",
      "insertAdjacentHTML",
      "document.write",
    ]) {
      expect(code).not.toContain(hatch);
    }

    // ...and the stripping itself is checked, so this cannot pass by having emptied the
    // string it examines.
    expect(code).toContain("DOMParser");
    expect(code).toContain("SAFE_SCHEMES");
  });
});
