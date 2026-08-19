"""The public lead form, and the owner's list of what it captured.

Two routers, deliberately two objects rather than one with mixed decorators:

* :data:`public_router` carries ``POST /public/forms/{form_id}`` -- an
  **unauthenticated write**, and therefore the most exposed surface in the product;
* :data:`router` carries ``GET /api/v1/leads``, which returns a tenant's customer
  contact details and is behind the session cookie.

Keeping them separate means "which of these is public?" is answered at the mount
site instead of by reading every decorator in the file.

The public write
----------------

It has to be anonymous: the whole point of docs/CHANNELS.md section 5 is that a
stranger who came from an Instagram bio can convert without an account. So the
protections are layered, in this order, and the order is part of the design:

1. **Rate limit, first.** Before the size check and before any database work,
   because the form lookup is a privileged unscoped read (see
   ``db/adapters/lead_store.py``) and a flood must not get to run it at will. It
   reuses ``core.rate_limit``, which already has the Redis-with-local-fallback
   behaviour, HMAC'd counter keys, and the deliberate decision to trust
   ``request.client.host`` rather than ``X-Forwarded-For``.
2. **A bounded body.** ``Content-Length`` is checked before the stream is read, and
   a request that declares no length is refused rather than read hopefully. The
   header is caller-supplied, so the length is re-checked after reading -- at
   application level that is the best available, and a hard byte cap belongs in the
   proxy as well.
3. **A closed field schema.** ``extra="forbid"``: an unexpected key is refused, not
   swallowed. ``leads.fields`` is JSONB, so an open schema would let an anonymous
   caller decide what we store, which is a free key-value store with our name on
   the bill.
4. **A honeypot, answered exactly like a success.** A 400 would tell the bot which
   field to leave alone next time. The refusal is that nothing is written.

It accepts **two body encodings**, and the second one is not a convenience. A
generated landing page has to work with JavaScript off (see ``api/pages.py``), and a
plain HTML form posts ``application/x-www-form-urlencoded`` -- so a JSON-only endpoint
would mean every no-script visitor's lead is refused after they had already typed it
in. The urlencoded path is folded into the SAME schema, the same honeypot, the same
size cap and the same rate limit rather than getting its own: two implementations of
one set of protections is how one of them ends up weaker.

The only difference is what comes back. A JSON caller gets the constant 202; a form
POST gets a **303 back to the page it came from**, with ``?sent=1`` or ``?error=1``,
so the browser lands on a confirmation instead of on a JSON body. The target is built
from the resolved content piece, never from anything in the request -- a redirect
target taken from a parameter is an open redirect.

And two rules about what comes back out:

**Nothing is reflected.** Not in the 202, not in the 422 -- the validation error
carries field locations and a fixed message, never a submitted value. This module
builds that response itself rather than relying on the app-wide redaction in
``main.py``, because it parses the body by hand in order to enforce the size cap
before reading.

**Every refusal that concerns a form is a 404.** Unknown id, malformed id, and a
draft page all answer identically: a 403 on the draft would confirm that
unpublished work exists at that id, and the submitter can act on none of the
distinctions.

No IP and no user agent are ever stored, on any path -- the same rule as the click,
and this is the path most likely to break it, because ``fields`` is a JSONB blob
that would accept them without complaint. The blob is built from the declared
schema only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from backend.app.api.auth import CurrentUser
from backend.app.core.config import get_settings
from backend.app.core.rate_limit import (
    DIMENSION_IP,
    FixedWindowRateLimiter,
    InMemoryWindowCounter,
    RateLimitRule,
    RedisWindowCounter,
)
from backend.app.db.adapters.lead_store import (
    DEFAULT_LEAD_LIMIT,
    MAX_LEAD_LIMIT,
    FormTarget,
    LeadRecord,
    PostgresLeadStore,
    ShortLinkRecord,
)
from backend.app.services.landing_service import landing_page_path
from backend.app.services.link_service import MAX_CODE_LENGTH

public_router = APIRouter(tags=["leads"])
router = APIRouter(tags=["leads"])

#: 8 KiB. A name, an email, a phone number and a two-thousand-character message fit
#: several times over, and an anonymous caller has no business sending more.
MAX_FORM_BODY_BYTES: Final = 8 * 1024

#: What a browser sends when a plain HTML form is submitted. The generated landing page
#: has no JavaScript, so this is the encoding a real visitor's lead arrives in.
FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"

#: The statuses whose landing page may accept a submission. A draft page's form must
#: not take leads -- it has not been approved, and its copy may promise something the
#: business would not.
LIVE_FORM_STATUSES: Final = frozenset({"approved", "published"})

#: The five real UTM parameters. Anything else in the submitted ``utm`` object is
#: dropped rather than refused: a marketing tool may append its own tracking keys to
#: a URL, and that is not the visitor's fault -- but this field is a measurement,
#: not a place to keep arbitrary caller-supplied data.
KNOWN_UTM_KEYS: Final = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}
)
MAX_UTM_VALUE_LENGTH: Final = 120

#: Five submissions per ten minutes from one address. A person filling in one form
#: sends one; a person who mistypes their email and resubmits sends three. Six is
#: not a customer.
FORM_RULES: Final[dict[str, RateLimitRule]] = {
    DIMENSION_IP: RateLimitRule(limit=5, window_seconds=600)
}


class LeadStore(Protocol):
    """What these two routes need from persistence, and nothing more."""

    async def resolve_form(self, content_piece_id: UUID) -> FormTarget | None: ...

    async def resolve(self, code: str) -> ShortLinkRecord | None: ...

    async def create_lead(
        self,
        business_id: UUID,
        *,
        fields: dict[str, Any],
        utm: dict[str, Any],
        short_link_id: UUID | None = ...,
        content_piece_id: UUID | None = ...,
        source: str = ...,
    ) -> LeadRecord: ...

    async def list_leads(self, business_id: UUID, *, limit: int = ...) -> list[LeadRecord]: ...

    async def business_for_owner(self, user_id: UUID) -> UUID | None: ...


def get_store() -> LeadStore:
    """The persistence adapter. Overridden in tests, which is why it is a function."""
    return PostgresLeadStore()


@lru_cache(maxsize=1)
def _shipped_form_limiter() -> FixedWindowRateLimiter:
    settings = get_settings()
    return FixedWindowRateLimiter(
        rules=FORM_RULES,
        counter=RedisWindowCounter(settings.redis_url),
        fallback=InMemoryWindowCounter(),
        namespace="sma:rl:lead-form",
        secret=settings.session_secret,
    )


def get_form_limiter() -> FixedWindowRateLimiter:
    """The form throttle.

    Cached process-wide, because a limiter built per request would count nothing --
    and a dependency rather than a direct call, because a test must be able to swap
    in an isolated one or the suite's own submissions become each other's budget.
    """
    return _shipped_form_limiter()


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LeadSubmission(BaseModel):
    """The only shape a public submission may take.

    ``extra="forbid"`` is the load-bearing line: see the module docstring.

    ``homepage2`` is the honeypot. The name is chosen to be invisible to browser
    autofill -- a field called ``website``, ``url`` or ``nickname`` is a field
    Chrome may fill in for a real person, and a false positive here silently
    discards a genuine lead. It is rendered hidden, so anything in it came from
    something that reads the DOM rather than the page.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    message: str = Field(default="", max_length=2000)
    #: Recorded, and required. Storing contact details with no evidence of consent
    #: is the compliance problem this product would otherwise hand to every customer
    #: it has, all at once.
    consent: bool = False
    #: The short-link code the landing page was reached by, if any.
    ref: str = Field(default="", max_length=MAX_CODE_LENGTH)
    utm: dict[str, str] = Field(default_factory=dict)
    homepage2: str = Field(default="", max_length=200)

    @field_validator("utm", mode="before")
    @classmethod
    def _keep_only_real_utm_keys(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, raw in value.items():
            if str(key) in KNOWN_UTM_KEYS and isinstance(raw, str):
                cleaned[str(key)] = raw.strip()[:MAX_UTM_VALUE_LENGTH]
        return cleaned

    @model_validator(mode="after")
    def _must_be_answerable_and_consented(self) -> LeadSubmission:
        if not self.consent:
            raise ValueError("consent is required")
        if not self.email and not self.phone:
            raise ValueError("an email address or a phone number is required")
        if self.email and not _looks_like_an_email(self.email):
            raise ValueError("email is not a usable address")
        return self


class ReceivedResponse(CamelModel):
    """The one answer a submitter ever gets.

    Deliberately constant. It has to be byte-identical whether the lead was stored
    or silently dropped by the honeypot, so it can carry no id, no count and no
    echo.
    """

    status: str = "received"


class LeadOut(CamelModel):
    id: UUID
    content_piece_id: UUID | None
    short_link_id: UUID | None
    source: str
    status: str
    utm: dict[str, Any]
    fields: dict[str, Any]
    created_at: Any


class LeadListResponse(CamelModel):
    leads: list[LeadOut]


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


#: One answer for every reason a form will not take a submission. See the module
#: docstring: a 403 on a draft would confirm the draft exists.
_NO_SUCH_FORM: Final = _error("not_found", "That form is not available.")
_TOO_MANY: Final = _error("too_many_requests", "Too many submissions. Please try again later.")
_TOO_LARGE: Final = _error("payload_too_large", "That submission is too large.")
_NO_LENGTH: Final = _error("length_required", "A declared content length is required.")


def _looks_like_an_email(value: str) -> bool:
    """A deliberately loose shape check.

    Anything approaching RFC 5322 rejects addresses that work, and the only
    consequence of a false accept is one undeliverable reply -- while a false reject
    is a lost customer. So: one ``@``, something either side, a dot in the domain,
    no whitespace.
    """
    if any(character.isspace() for character in value) or value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".")


def _field_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Field locations and a fixed message. Never a value, never pydantic's own text.

    Pydantic's messages can quote the input, and this endpoint's whole contract is
    that nothing submitted comes back out. The location is kept because a legitimate
    caller still needs to know WHICH field was wrong.
    """
    return [
        {"loc": [str(part) for part in error.get("loc", ())], "msg": "This value is not accepted."}
        for error in exc.errors()[:10]
    ]


async def _read_body_within_cap(request: Request) -> bytes:
    """Read the request body, refusing anything over :data:`MAX_FORM_BODY_BYTES`.

    ``Content-Length`` is consulted before the stream is touched, so the ordinary
    oversized request costs nothing. A request that declares no length -- a chunked
    upload -- is refused rather than read hopefully, because there is no way to
    bound it in advance. The length is then re-checked against what actually
    arrived, since the header is caller-supplied and therefore a hint.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(status.HTTP_411_LENGTH_REQUIRED, detail=_NO_LENGTH)
    if not declared.isdigit() or int(declared) > MAX_FORM_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=_TOO_LARGE)

    body = await request.body()
    if len(body) > MAX_FORM_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=_TOO_LARGE)
    return body


async def _enforce_rate_limit(request: Request, limiter: FixedWindowRateLimiter) -> None:
    """Count this attempt, and refuse it if the window is spent.

    Keyed on ``request.client.host`` only. ``X-Forwarded-For`` is never parsed: on
    an internet-facing route an attacker puts a fresh address in every request and
    the dimension disappears. Behind a proxy, uvicorn's ``--proxy-headers`` with an
    explicit ``--forwarded-allow-ips`` is what makes ``client`` trustworthy.

    The address is used to count and is not stored, logged or returned; the counter
    key is an HMAC digest of it, so it is not recoverable from the counter store
    either.
    """
    client = request.client
    decision = await limiter.check({DIMENSION_IP: client.host if client else "unknown"})
    if decision.allowed:
        return
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_TOO_MANY,
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def _is_form_encoded(request: Request) -> bool:
    """Whether this is a browser form post rather than a JSON call.

    The media type only -- parameters such as ``; charset=utf-8`` are stripped, since a
    browser sends them and an exact string comparison would quietly route every real
    form submission down the JSON path.
    """
    declared = request.headers.get("content-type", "")
    return declared.split(";")[0].strip().lower() == FORM_CONTENT_TYPE


def _submission_from_form(body: bytes) -> LeadSubmission:
    """Parse a urlencoded body into the same closed schema the JSON path uses.

    Two shape differences between an HTML form and our JSON, both handled here so that
    :class:`LeadSubmission` stays one schema:

    * a form cannot send a nested object, so the ``utm`` map arrives as flat
      ``utm_source`` / ``utm_campaign`` hidden inputs and is folded back up. An
      explicit ``utm`` field in the body is overwritten rather than merged: this is the
      only place that key is built, and honouring a caller-supplied one would let a bot
      put arbitrary JSON into ``leads.utm``;
    * an unchecked checkbox is ABSENT rather than false, which the model's default and
      its ``consent is required`` validator already handle correctly.

    ``extra="forbid"`` still applies, so an unexpected field is still refused.
    """
    pairs = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    payload: dict[str, Any] = {}
    utm: dict[str, str] = {}
    for key, value in pairs:
        if key.startswith("utm_"):
            utm[key] = value
        else:
            payload[key] = value
    payload["utm"] = utm
    return LeadSubmission.model_validate(payload)


def _answer(
    *, form_encoded: bool, content_piece_id: UUID, ok: bool = True
) -> ReceivedResponse | RedirectResponse:
    """The one answer a submitter gets, in whichever form they asked for it.

    303 rather than 302, because the request was a POST and the thing to fetch next is
    a GET -- a 302 leaves some clients re-posting. The ``Location`` is a RELATIVE path
    built from the resolved content piece: nothing from the request contributes to it,
    so this cannot become an open redirect, and it needs no trust in ``Host``.

    ``no-store`` on the redirect itself, so a cached 303 cannot pin a visitor to a
    confirmation for a submission they have not made.
    """
    if not form_encoded:
        return ReceivedResponse()
    flag = "sent" if ok else "error"
    return RedirectResponse(
        url=f"{landing_page_path(content_piece_id)}?{flag}=1",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


async def _attributed_link(
    store: LeadStore, business_id: UUID, code: str
) -> ShortLinkRecord | None:
    """The short link named by ``code``, but only if it belongs to this business.

    A mismatch is dropped rather than refused. Attribution is worth less than the
    lead: refusing would throw away a real enquiry because of a bad query
    parameter, and it would also let anyone plant leads in another business's
    report by guessing a code.
    """
    if not code:
        return None
    link = await store.resolve(code)
    if link is None or link.business_id != business_id:
        return None
    return link


@public_router.post(
    "/public/forms/{form_id}",
    response_model=ReceivedResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a landing-page lead form (public, unauthenticated)",
)
async def submit_form(
    form_id: str,
    request: Request,
    store: Annotated[LeadStore, Depends(get_store)],
    limiter: Annotated[FixedWindowRateLimiter, Depends(get_form_limiter)],
) -> ReceivedResponse | RedirectResponse:
    """Accept one lead for the landing page identified by ``form_id``.

    202 rather than 201 for a JSON caller: the response carries no identifier for the
    thing created, on purpose, because it must be indistinguishable from the honeypot's
    answer. A browser form gets a 303 back to the page instead -- see the module
    docstring.

    ``form_id`` is parsed here rather than declared as a ``UUID`` path parameter so
    that a malformed id produces the same 404 as an unknown one instead of a 422
    that describes our schema.
    """
    await _enforce_rate_limit(request, limiter)
    body = await _read_body_within_cap(request)
    form_encoded = _is_form_encoded(request)

    try:
        content_piece_id = UUID(form_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_FORM) from exc

    form = await store.resolve_form(content_piece_id)
    if form is None or form.status not in LIVE_FORM_STATUSES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_FORM)

    try:
        payload = (
            _submission_from_form(body)
            if form_encoded
            else LeadSubmission.model_validate_json(body)
        )
    except ValidationError as exc:
        # A form submitter is sent back to the page with a notice, because a JSON body
        # is not an answer a person in a browser can act on. Still nothing reflected:
        # the flag is one bit, and the page's own copy says what is required.
        if form_encoded:
            return _answer(form_encoded=True, content_piece_id=form.content_piece_id, ok=False)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_field_errors(exc)
        ) from exc

    # The honeypot. Answered exactly like a success -- see the module docstring.
    if payload.homepage2:
        return _answer(form_encoded=form_encoded, content_piece_id=form.content_piece_id)

    link = await _attributed_link(store, form.business_id, payload.ref)

    await store.create_lead(
        form.business_id,
        # Built field by field from the declared schema. Never `payload.model_dump()`
        # wholesale, and never anything off the request: no address, no user agent,
        # and not the honeypot's contents either.
        fields={
            "name": payload.name,
            "email": payload.email,
            "phone": payload.phone,
            "message": payload.message,
            "consent": True,
        },
        utm=dict(payload.utm),
        short_link_id=link.id if link is not None else None,
        content_piece_id=form.content_piece_id,
        source="form",
    )
    return _answer(form_encoded=form_encoded, content_piece_id=form.content_piece_id)


@router.get(
    "/api/v1/leads",
    response_model=LeadListResponse,
    response_model_by_alias=True,
    summary="The signed-in owner's leads, newest first",
)
async def list_leads(
    user: CurrentUser,
    store: Annotated[LeadStore, Depends(get_store)],
    response: Response,
    limit: Annotated[int, Query(ge=1, le=MAX_LEAD_LIMIT)] = DEFAULT_LEAD_LIMIT,
) -> LeadListResponse:
    """List this owner's leads.

    **The business comes from the session, never from the request.** There is no
    ``businessId`` parameter and there must never be one: FastAPI ignores unknown
    query parameters silently, so one that worked would be a complete cross-tenant
    read that no test would notice.

    An owner with no business gets an empty list rather than an error. Signup
    creates the business in the same transaction as the user, so this state means a
    platform-admin account or a removed membership -- not the caller's mistake, and
    either way there are no leads to show.

    ``no-store``, because the body is a list of named people with their phone
    numbers and it must not sit in a shared cache.
    """
    response.headers["Cache-Control"] = "no-store"

    business_id = await store.business_for_owner(user.id)
    if business_id is None:
        return LeadListResponse(leads=[])

    leads = await store.list_leads(business_id, limit=limit)
    return LeadListResponse(
        leads=[
            LeadOut(
                id=lead.id,
                content_piece_id=lead.content_piece_id,
                short_link_id=lead.short_link_id,
                source=lead.source,
                status=lead.status,
                utm=lead.utm,
                fields=lead.fields,
                created_at=lead.created_at,
            )
            for lead in leads
        ]
    )


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check

    def _store_satisfies_port(store: PostgresLeadStore) -> LeadStore:
        """Fails type checking the moment the adapter drifts from what these routes need."""
        return store
