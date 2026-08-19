"""Deterministic banned-claim matching. Pure: no I/O, no clock, no randomness.

**Why this is an engine and not an agent node.** The project rule is "if the
answer is computable, compute it; only ask a model to decide, interpret, or
write" (`docs/AGENT_RUNTIME.md` section 1). "Does this text contain one of these
phrases" is computable, and three properties follow from computing it that a
model call cannot offer at any price:

* it is **deterministic** -- the same draft always produces the same verdict, so
  the verdict can be stored, compared across runs, and defended to a customer;
* it **cannot be argued out of it** -- the guard sits downstream of the model, so
  text that talked the writer into a forbidden promise cannot also talk the gate
  into allowing it, which is the whole point of a compliance backstop;
* it costs nothing and cannot fail, so it can run on every draft and every social
  rendering rather than on a sample.

**Precision is the design constraint, not recall.** A false positive blocks a
legitimate piece of content, and a gate that cries wolf is a gate somebody
switches off. So every tolerance below is bounded and closed-form, and there is
no fuzzy, edit-distance or embedding matching anywhere in this module.

What is tolerated, and why each one is a real German-web problem:

1. **Case.** ``Schmerzfrei`` at the start of a sentence is the same claim.
2. **Whitespace.** A renderer breaks ``beste\\nZahnarztpraxis`` across a line, so
   internal spacing becomes ``\\s+``.
3. **Markup.** The draft is HTML, so ``bes<b>te</b> Praxis`` reads as
   ``beste Praxis`` to a human and must read that way here too. Inline tags are
   removed without inserting a space (that is how a browser renders them);
   block-level tags become whitespace (so ``</p><p>`` does not fuse two words).
   Text inside an HTML comment or hidden by CSS is still searched -- it ships to
   the client either way -- while ``script`` and ``style`` bodies are dropped,
   because those are code, not copy.
4. **Invisible characters.** A soft hyphen or a zero-width space inside a word is
   invisible on the page and would defeat a literal match, so they are dropped.
   Removing a character nobody can see cannot create a false positive.
5. **Word boundaries, and the hyphen counts as part of the word.** ``frei`` must not
   match inside ``Schmerzfreiheit``, and -- less obvious -- ``ohne Risiko`` must not
   match inside ``ohne Risiko-Aufschlag``, which means the opposite of the claim.
   German builds compounds with hyphens constantly, and a component of a compound is
   a different word from the claim, exactly as a substring is. The cost of that choice
   is stated rather than hidden: ``Schmerzfrei-Garantie`` IS the claim and is NOT
   caught. Precision was chosen over recall deliberately, because the recall miss is
   visible to a reviewer reading the draft while a false positive silently blocks
   publishable copy.
6. **German adjective and noun inflection**, and this is the only tolerance with
   any precision risk, so it is deliberately narrow. Exactly one closed set of
   five endings is interchangeable -- ``-e -en -er -es -em`` -- and only on a word
   long enough that no function word acquires a variant. A word that already
   carries one of those endings is reduced to its stem when the stem is at least
   :data:`MIN_INFLECTION_STEM` characters (``beste`` -> ``best`` + any ending, so
   ``besten`` matches too); a word that carries none may take one when it is at
   least :data:`MIN_SUFFIXABLE_WORD` characters (``schmerzfrei`` also matches
   ``schmerzfreie``, which is how the claim is actually written in copy). Below
   those lengths the word is matched literally, which is what stops the claim word
   ``die`` from matching ``dies`` and ``ohne`` from matching ``ohnen``. No other
   morphology, no stemming library, and no edit distance is used.
7. **Umlaut transliteration.** ``guenstigster`` and ``günstigster`` are the same
   word to a customer typing the claim list, and either spelling can appear in
   copy. The substitutions are symmetric and confined to the four German pairs.

**What is NOT detected, stated plainly because the gate's honesty depends on
it.** This finds a configured claim and its inflected variants. It does not
detect a paraphrase (``voellig ohne Schmerzen`` for ``schmerzfrei``), a synonym,
a claim split across two sentences, or an implication. It is therefore a
*publication gate*, not a compliance certificate: it guarantees that the phrases
the business listed do not appear, and nothing beyond that.
"""

import html as html_module
import re
from typing import Final

from backend.app.engines.claims.contract import (
    ClaimCheckRequest,
    ClaimCheckResult,
    ClaimHit,
)

#: The closed set of German adjective endings the matcher will interchange.
ADJECTIVE_ENDINGS: Final[tuple[str, ...]] = ("en", "er", "es", "em", "e")

#: Minimum stem length before an existing ending may be interchanged. Four is the
#: smallest value at which no short function word ("die", "der", "ohne") acquires
#: a variant: it is what stops the claim word "die" from also matching "dies".
MIN_INFLECTION_STEM: Final = 4

#: Minimum length before a word with NO adjective ending may take one. Five,
#: because at four "Brot" would gain "Brote"/"Brotes" and "Rat" would gain "Rate",
#: which is a different word. Above it the additions are inflections of the same
#: word ("schmerzfrei" -> "schmerzfreie") or nonsense that matches nothing.
MIN_SUFFIXABLE_WORD: Final = 5

#: Symmetric German transliteration pairs. One alternation per pair, applied in a
#: single pass so an inserted alternative can never be rewritten again.
_FOLDS: Final[dict[str, str]] = {
    "ä": "(?:ä|ae)",
    "ö": "(?:ö|oe)",
    "ü": "(?:ü|ue)",
    "ß": "(?:ß|ss)",
    "ae": "(?:ae|ä)",
    "oe": "(?:oe|ö)",
    "ue": "(?:ue|ü)",
    "ss": "(?:ss|ß)",
}
_FOLD_RE: Final = re.compile("|".join(sorted(_FOLDS, key=len, reverse=True)), re.IGNORECASE)

#: `script` and `style` bodies are not visible copy, so their text is dropped
#: entirely rather than stripped down to words that were never on the page.
_SCRIPT_RE: Final = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

#: Only the delimiters go, not the body. A comment is not rendered, but it IS
#: shipped to the client and read by anyone who views source, so a forbidden
#: promise parked in one is still a forbidden promise in the published artefact.
#: The same reasoning covers CSS-hidden text, which is ordinary text to this
#: module because no style is interpreted here.
_COMMENT_DELIM_RE: Final = re.compile(r"<!--|-->")

#: Tags that a browser renders as a break. Everything else is inline and is
#: removed without inserting whitespace, so `bes<b>te</b>` stays one word.
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)  # fmt: skip
_TAG_RE: Final = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>|<[^>]*>")

#: Characters that are invisible in rendered text and would otherwise split a
#: word: soft hyphen, zero-width space/non-joiner/joiner, word joiner, BOM.
_INVISIBLE_RE: Final = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")

#: What counts as "inside a word" for the boundary lookarounds. `\w` plus the
#: hyphen -- see tolerance 5 in the module docstring for why the hyphen is here.
_WORD_CHAR: Final = r"[\w-]"

#: Characters either side of a hit, for the review screen.
CONTEXT_WINDOW: Final = 48

_WHITESPACE_RE: Final = re.compile(r"\s+")


def strip_markup(content: str) -> str:
    """Reduce HTML to the text a reader would actually see.

    Total by construction: unbalanced tags, a stray ``<``, an unterminated
    comment -- none of them raise. A gate that crashes on one malformed draft is
    a gate that lets that draft through.
    """
    text = _COMMENT_DELIM_RE.sub(" ", _SCRIPT_RE.sub(" ", content))

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return " " if name is not None and name.lower() in _BLOCK_TAGS else ""

    text = _TAG_RE.sub(_replace, text)
    # Entities are unescaped AFTER tag removal, so an escaped `&lt;p&gt;` in the
    # copy is treated as the literal text it is, not as a tag to strip.
    return html_module.unescape(text)


def normalise(content: str, *, contains_markup: bool = True) -> str:
    """The exact string the matcher searches. Offsets in a hit refer to this."""
    text = strip_markup(content) if contains_markup else content
    return _INVISIBLE_RE.sub("", text)


def _fold(literal: str) -> str:
    """Escape a literal run, expanding German transliteration pairs."""
    parts: list[str] = []
    cursor = 0
    for match in _FOLD_RE.finditer(literal):
        parts.append(re.escape(literal[cursor : match.start()]))
        parts.append(_FOLDS[match.group(0).lower()])
        cursor = match.end()
    parts.append(re.escape(literal[cursor:]))
    return "".join(parts)


def _inflection_group() -> str:
    """The optional ending, longest alternative first so a greedy match is total."""
    return "(?:" + "|".join(ADJECTIVE_ENDINGS) + ")?"


def _word_pattern(word: str) -> str:
    """One word of a claim, with the bounded inflection tolerance applied.

    Returns a literal (folded) pattern when the word is too short for the
    tolerance to be safe -- see :data:`MIN_INFLECTION_STEM` and
    :data:`MIN_SUFFIXABLE_WORD` for why each threshold is where it is.
    """
    lowered = word.lower()
    for ending in ADJECTIVE_ENDINGS:
        if len(word) > len(ending) and lowered.endswith(ending):
            stem = word[: -len(ending)]
            if len(stem) >= MIN_INFLECTION_STEM:
                return f"{_fold(stem)}{_inflection_group()}"
            break

    if len(word) >= MIN_SUFFIXABLE_WORD:
        return f"{_fold(word)}{_inflection_group()}"
    return _fold(word)


def claim_pattern(claim: str) -> re.Pattern[str]:
    """Compile one banned claim into its matcher.

    Exposed because a compiled pattern is the honest way to test the tolerances
    one at a time, and because a caller checking many drafts against the same
    list should be able to compile once.
    """
    words = [_word_pattern(word) for word in claim.split()]
    body = r"\s+".join(words) if words else re.escape(claim)
    return re.compile(rf"(?<!{_WORD_CHAR}){body}(?!{_WORD_CHAR})", re.IGNORECASE)


def _context(text: str, start: int, end: int) -> str:
    left = max(0, start - CONTEXT_WINDOW)
    right = min(len(text), end + CONTEXT_WINDOW)
    snippet = text[left:right]
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(text) else ""
    return f"{prefix}{_WHITESPACE_RE.sub(' ', snippet).strip()}{suffix}"


def _fix_hint(hits: list[ClaimHit]) -> str:
    """The retry instruction, written for the model and quantitative about what.

    It says *remove*, not *soften*, and it forbids paraphrasing: a paraphrase
    would pass this gate while making the same forbidden promise, which would
    turn the guard into a filter for wording rather than for claims.
    """
    lines = [
        f'- Remove the forbidden claim "{hit.claim}" (it appears as "{hit.matched}"). '
        "Delete the promise entirely; do not paraphrase it, soften it, or move it "
        "to another section."
        for hit in hits
    ]
    return (
        "This business is legally not permitted to make the following claim(s). "
        "The draft cannot be published while any of them is present:\n" + "\n".join(lines)
    )


def check_claims(request: ClaimCheckRequest) -> ClaimCheckResult:
    """Does this content make a claim the business is not allowed to make.

    An empty claim list is a real state -- the business has configured no rule
    yet -- and is reported as `exercised=False` rather than as a pass the gate
    earned.
    """
    claims = [claim for claim in (c.strip() for c in request.banned_claims) if claim]
    if not claims:
        return ClaimCheckResult(
            passed=True,
            exercised=False,
            checked=0,
            detail=(
                "No banned claims are configured for this business, so nothing was "
                "checked. This is not a compliance pass."
            ),
        )

    text = normalise(request.content, contains_markup=request.contains_markup)
    hits: list[ClaimHit] = []
    for claim in claims:
        for match in claim_pattern(claim).finditer(text):
            hits.append(
                ClaimHit(
                    claim=claim,
                    matched=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    context=_context(text, match.start(), match.end()),
                )
            )

    if not hits:
        return ClaimCheckResult(
            passed=True,
            exercised=True,
            checked=len(claims),
            detail=f"None of {len(claims)} banned claim(s) appear in the content.",
        )

    distinct = len({hit.claim for hit in hits})
    return ClaimCheckResult(
        passed=False,
        exercised=True,
        checked=len(claims),
        hits=hits,
        detail=(
            f"{distinct} of {len(claims)} banned claim(s) present, "
            f"{len(hits)} occurrence(s). The content cannot be published."
        ),
        fix_hint=_fix_hint(hits),
    )
