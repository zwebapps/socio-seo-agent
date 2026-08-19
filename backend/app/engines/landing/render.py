"""Assemble the landing page's HTML. Pure string building, no I/O, no model.

This is in an engine and not in the route or in a template for one reason: *if the
answer is computable, compute it*. Turning a spec into markup is a total function
of the spec, so it is testable without a browser, without a database and without a
server -- and the escaping, which is the security-critical part, is asserted by
unit tests rather than reviewed by eye.

Four properties the output must have, all of them requirements rather than taste:

**No JavaScript, anywhere.** The form is a plain ``method="post"`` form, so the
page works with scripting disabled, in a preview pane, and in the text browsers
that some corporate mail gateways use to check a link. That is the same reason
``/l/{code}`` is a 302 and not a script redirect.

**No cookie and no third-party asset.** Nothing here emits ``Set-Cookie`` (it
cannot -- it returns a string) and nothing references an external font, script or
image, so a visitor is not tracked by anyone on our behalf and the page needs no
consent banner of its own. A single inline ``<style>`` block is used instead; CSS
is not script.

**Everything interpolated is escaped.** The spec is written by a language model
from crawled pages and uploaded documents -- i.e. from attacker-influenceable text
-- so every value is passed through :func:`html.escape` with ``quote=True``,
including the ones that land in attributes. There is no path on which raw markup
from the spec reaches the output.

**A page that cannot capture a lead is not rendered at all.** No form field, no
button label, or no consent sentence raises :class:`RenderRefusedError`. Serving
such a page would break the CONVERSION link the product is judged on while looking
like a success, and the deterministic check has already said so in
:mod:`backend.app.engines.landing.checks`; this is the second, structural refusal
at the point of no return.
"""

import re
from collections.abc import Mapping
from html import escape
from typing import Final, Literal

from backend.app.engines.landing.contract import FormField, LandingPageSpec

PageState = Literal["form", "sent", "error"]

#: What a short-link code may look like in the ``?ref=`` parameter. Mirrors the
#: SHAPE of ``link_service.CODE_ALPHABET`` without importing it (an engine may not
#: import a service): alphanumeric only, so nothing that reaches the hidden input
#: could ever need escaping in the first place. A value that does not match is
#: DROPPED rather than escaped -- attribution is worth less than not reflecting
#: arbitrary caller input into our own page.
_REF_RE: Final = re.compile(r"^[0-9A-Za-z]{4,16}$")

#: UTM parameters are passed through to the form as hidden inputs so the lead
#: carries the campaign that produced it. Matched by SHAPE, not against a copy of
#: the five known names: the vocabulary belongs to the API layer, and a second copy
#: of it here is how the two would drift.
_UTM_KEY_RE: Final = re.compile(r"^utm_[a-z]{1,20}$")
MAX_UTM_VALUE_CHARS: Final = 120

#: `type` and `autocomplete` per field, so a browser can fill a real person's
#: details in and a password manager never offers to. `message` is a textarea.
_INPUT_TYPES: Final[Mapping[str, str]] = {
    "name": "text",
    "email": "email",
    "phone": "tel",
}
_AUTOCOMPLETE: Final[Mapping[str, str]] = {
    "name": "name",
    "email": "email",
    "phone": "tel",
}

#: The honeypot's field name. It must match the one the endpoint reads, and the
#: name is chosen to be invisible to browser autofill -- a field called `website`
#: or `nickname` is one Chrome may fill in for a real person, and a false positive
#: silently discards a genuine lead.
HONEYPOT_FIELD: Final = "homepage2"

#: Localised chrome. German first, because the product is German-first; anything
#: else falls back to English. Deliberately tiny: this is the only copy on the page
#: that is NOT generated, so it is the only copy that could be wrong in a way the
#: business never sees.
_COPY: Final[Mapping[str, Mapping[str, str]]] = {
    "de": {
        "sent_title": "Danke — wir haben Ihre Anfrage erhalten.",
        "sent_body": "Wir melden uns so schnell wie möglich bei Ihnen.",
        "error_title": "Das hat nicht funktioniert.",
        "error_body": (
            "Bitte prüfen Sie Ihre Angaben: eine E-Mail-Adresse oder eine "
            "Telefonnummer und die Einwilligung sind erforderlich."
        ),
        "required": "Pflichtfeld",
        "proof_source": "Quelle",
        "honeypot_label": "Dieses Feld bitte leer lassen.",
    },
    "en": {
        "sent_title": "Thank you — we have your enquiry.",
        "sent_body": "We will get back to you as soon as we can.",
        "error_title": "That did not go through.",
        "error_body": (
            "Please check your details: an email address or a phone number and the "
            "consent box are required."
        ),
        "required": "required",
        "proof_source": "Source",
        "honeypot_label": "Please leave this field empty.",
    },
}

#: One inline stylesheet. Not a design system -- a readable, single-column,
#: mobile-first page with a visible focus ring and AA-contrast text. Deliberately
#: minimal: the styling of a generated page is not what this task is about, and an
#: elaborate template would be a second thing to keep in step with the spec.
_STYLE: Final = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: #16191d; background: #fff; }
main { max-width: 42rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
h1 { font-size: clamp(1.6rem, 4vw, 2.4rem); line-height: 1.2; margin: 0 0 .5rem; }
.sub { font-size: 1.15rem; color: #3c4149; margin: 0 0 1.5rem; }
.offer { background: #f4f6f8; border-left: 4px solid #16191d; padding: 1rem 1.25rem;
  margin: 0 0 1.5rem; }
ul.proof { list-style: none; padding: 0; margin: 0 0 2rem; }
ul.proof li { padding: .6rem 0; border-bottom: 1px solid #e4e7ea; }
ul.proof .src { display: block; font-size: .8rem; color: #5a616b; }
form { border: 1px solid #d7dbdf; border-radius: .5rem; padding: 1.25rem; }
label { display: block; font-weight: 600; margin: .9rem 0 .25rem; }
input[type=text], input[type=email], input[type=tel], textarea {
  width: 100%; padding: .6rem .7rem; font: inherit; border: 1px solid #9aa1a9;
  border-radius: .35rem; background: #fff; color: inherit; }
textarea { min-height: 6rem; }
.consent { display: flex; gap: .6rem; align-items: flex-start; margin: 1.1rem 0; }
.consent label { font-weight: 400; margin: 0; }
button { width: 100%; padding: .85rem 1rem; font: inherit; font-weight: 700;
  border: 0; border-radius: .35rem; background: #16191d; color: #fff; cursor: pointer; }
:focus-visible { outline: 3px solid #0b5fff; outline-offset: 2px; }
.hp { position: absolute; left: -9999px; width: 1px; height: 1px; overflow: hidden; }
.req { font-weight: 400; color: #5a616b; font-size: .8rem; }
.notice { border: 1px solid #d7dbdf; border-left: 4px solid #16191d; padding: 1rem 1.25rem;
  margin: 0 0 1.5rem; background: #f4f6f8; }
@media (prefers-color-scheme: dark) {
  body { background: #14171a; color: #f2f4f6; }
  .sub { color: #c3c9d0; } .offer, .notice { background: #1e2328; border-left-color: #f2f4f6; }
  ul.proof li { border-bottom-color: #2b3238; } ul.proof .src { color: #a8b0b8; }
  form, .notice { border-color: #2b3238; }
  input[type=text], input[type=email], input[type=tel], textarea {
    background: #14171a; border-color: #6d757e; }
  button { background: #f2f4f6; color: #14171a; }
}
"""


class RenderRefusedError(Exception):
    """The spec cannot become a working landing page.

    Raised rather than rendering a degraded page, because the degradation is
    invisible: a page with no form looks finished, gets traffic, and converts
    nothing. The message names every missing part at once so a caller does not
    have to fix them one render at a time.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(
            "the landing page cannot capture a lead and was not rendered: "
            + "; ".join(missing)
            + ". Fix the spec (the deterministic check in engines/landing/checks.py "
            "reports each of these as an error-severity finding) rather than "
            "relaxing this guard."
        )


def _copy(locale: str) -> Mapping[str, str]:
    return _COPY.get(locale.strip().lower()[:2], _COPY["en"])


def _lang(locale: str) -> str:
    """A safe BCP-47-ish language attribute, or `en`.

    Shape-validated rather than escaped: this value lands in an attribute on the
    root element, and a locale is either a language tag or a bug.
    """
    candidate = locale.strip().lower()
    return candidate if re.fullmatch(r"[a-z]{2}(-[a-z0-9]{2,8})?", candidate) else "en"


def _hidden(name: str, value: str) -> str:
    safe_name = escape(name, quote=True)
    safe_value = escape(value, quote=True)
    return f'<input type="hidden" name="{safe_name}" value="{safe_value}">'


def _field(field: FormField, copy: Mapping[str, str]) -> str:
    name = escape(field.name, quote=True)
    label = escape(field.label.strip() or field.name, quote=True)
    required = " required" if field.required else ""
    hint = f' <span class="req">{escape(copy["required"])}</span>' if field.required else ""
    if field.name == "message":
        control = f'<textarea id="f-{name}" name="{name}"{required}></textarea>'
    else:
        input_type = _INPUT_TYPES.get(field.name, "text")
        autocomplete = _AUTOCOMPLETE.get(field.name)
        auto = f' autocomplete="{autocomplete}"' if autocomplete else ""
        control = f'<input id="f-{name}" type="{input_type}" name="{name}"{auto}{required}>'
    return f'<label for="f-{name}">{label}{hint}</label>{control}'


def _proof(spec: LandingPageSpec, copy: Mapping[str, str]) -> str:
    items = [point for point in spec.proof_points if point.text.strip()]
    if not items:
        return ""
    rows = "".join(
        f"<li>{escape(point.text.strip())}"
        f'<span class="src">{escape(copy["proof_source"])}: {escape(point.source.strip())}</span>'
        "</li>"
        for point in items
    )
    return f'<ul class="proof">{rows}</ul>'


def _form(
    spec: LandingPageSpec,
    *,
    form_action: str,
    ref: str,
    utm: Mapping[str, str],
    copy: Mapping[str, str],
) -> str:
    hidden = [_hidden(key, value) for key, value in _passthrough(utm).items()]
    if ref:
        hidden.append(_hidden("ref", ref))
    fields = "".join(_field(field, copy) for field in spec.form_fields)
    honeypot = (
        f'<div class="hp" aria-hidden="true">'
        f'<label for="f-{HONEYPOT_FIELD}">{escape(copy["honeypot_label"])}</label>'
        f'<input id="f-{HONEYPOT_FIELD}" type="text" name="{HONEYPOT_FIELD}" '
        f'tabindex="-1" autocomplete="off"></div>'
    )
    consent = (
        '<div class="consent">'
        '<input id="f-consent" type="checkbox" name="consent" value="on" required>'
        f'<label for="f-consent">{escape(spec.consent_text.strip())}</label>'
        "</div>"
    )
    return (
        f'<form method="post" action="{escape(form_action, quote=True)}">'
        + "".join(hidden)
        + honeypot
        + fields
        + consent
        + f'<button type="submit">{escape(spec.primary_cta.strip())}</button>'
        + "</form>"
    )


def _passthrough(utm: Mapping[str, str]) -> dict[str, str]:
    """The UTM parameters worth carrying into the submission, by shape.

    Anything not shaped like ``utm_xxx`` is dropped, and every value is truncated,
    so a caller cannot turn the form into a carrier for arbitrary data by putting it
    in the query string.
    """
    return {
        key: value[:MAX_UTM_VALUE_CHARS]
        for key, value in utm.items()
        if _UTM_KEY_RE.fullmatch(key) and value
    }


def _refusals(spec: LandingPageSpec) -> list[str]:
    missing: list[str] = []
    if not [field for field in spec.form_fields if field.name]:
        missing.append("there is no form field, so there is nothing to submit")
    if not spec.primary_cta.strip():
        missing.append("there is no button label, so the page makes no ask")
    if not spec.consent_text.strip():
        missing.append("there is no consent sentence, so the form may not store contact details")
    return missing


def render_landing_page(
    spec: LandingPageSpec,
    *,
    business_name: str,
    form_action: str,
    ref: str = "",
    utm: Mapping[str, str] | None = None,
    locale: str = "de",
    state: PageState = "form",
) -> str:
    """Return the complete HTML document for one landing page.

    `form_action` is where the form posts. It is escaped into the attribute and is
    expected to be a path on our own origin; the caller builds it, because an engine
    has no business knowing the shape of our routes.

    `state` renders the same page in its three no-JavaScript states: the form, the
    confirmation after a successful submission, and the form again with a notice
    after a refused one. All three are the same document, so a submission needs no
    second page and no client-side rendering.
    """
    missing = _refusals(spec)
    if missing:
        raise RenderRefusedError(missing)

    copy = _copy(locale)
    ref_value = ref.strip() if _REF_RE.fullmatch(ref.strip()) else ""
    title = f"{spec.headline.strip()} · {business_name.strip()}".strip(" ·")

    notice = ""
    if state == "error":
        notice = (
            f'<div class="notice" role="alert"><strong>{escape(copy["error_title"])}</strong>'
            f"<p>{escape(copy['error_body'])}</p></div>"
        )

    if state == "sent":
        body = (
            f'<div class="notice" role="status"><strong>{escape(copy["sent_title"])}</strong>'
            f"<p>{escape(copy['sent_body'])}</p></div>"
        )
    else:
        body = notice + _form(
            spec,
            form_action=form_action,
            ref=ref_value,
            utm=utm or {},
            copy=copy,
        )

    subhead = f'<p class="sub">{escape(spec.subhead.strip())}</p>' if spec.subhead.strip() else ""
    offer = f'<div class="offer">{escape(spec.offer.strip())}</div>' if spec.offer.strip() else ""

    description = escape(spec.subhead.strip() or spec.offer.strip(), quote=True)[:300]
    return (
        "<!doctype html>"
        f'<html lang="{_lang(locale)}">'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<meta name="description" content="{description}">'
        f"<style>{_STYLE}</style>"
        "</head>"
        "<body><main>"
        f"<h1>{escape(spec.headline.strip())}</h1>"
        f"{subhead}{offer}"
        f"{_proof(spec, copy)}"
        f"{body}"
        f'<p class="src">{escape(business_name.strip())}</p>'
        "</main></body></html>"
    )


def render_landing_markdown(spec: LandingPageSpec) -> str:
    """The same page as plain Markdown.

    Not decoration: ``content_pieces.body_md`` is ``NOT NULL``, and more importantly a
    page that can only be read inside a browser is a page the owner cannot edit,
    diff, or paste into an email. The HTML is the artifact; this is the text of it.

    Nothing is escaped here, because Markdown is not a markup language a browser
    executes -- this string is stored and displayed as text. The HTML renderer above
    is the one with the escaping obligation.
    """
    lines = [f"# {spec.headline.strip()}"]
    if spec.subhead.strip():
        lines += ["", spec.subhead.strip()]
    if spec.offer.strip():
        lines += ["", f"**{spec.offer.strip()}**"]
    proof = [point for point in spec.proof_points if point.text.strip()]
    if proof:
        lines += ["", "## Warum wir"]
        lines += [f"- {point.text.strip()} _({point.source.strip()})_" for point in proof]
    if spec.form_fields:
        lines += ["", "## Formular"]
        lines += [
            f"- {field.label.strip() or field.name} (`{field.name}`)"
            + (" — Pflichtfeld" if field.required else "")
            for field in spec.form_fields
        ]
    lines += ["", f"**{spec.primary_cta.strip()}**", "", spec.consent_text.strip()]
    if spec.ctas:
        lines += ["", "## CTAs"]
        lines += [
            f"- **{cta.channel}**: {cta.text.strip()}" for cta in spec.ctas if cta.text.strip()
        ]
    return "\n".join(lines).strip() + "\n"
