"""Onboarding routes: a URL in, a draft Business DNA out.

The first thing a stranger touches, so error handling here is the product rather
than an afterthought. Three rules shape this module:

* every failure maps to a status code that says whose problem it is -- a thin site
  is the caller's (422), an unreachable origin is upstream's (502), and neither is
  a 500;
* a refused URL reveals NOTHING about our network. The SSRF guard's own message
  names the address it blocked, which is right for a server log and wrong for an
  HTTP response, so it is deliberately not forwarded;
* the response is camelCase, because a TypeScript client should not have to
  translate field names it did not choose.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from backend.app.api.auth import CurrentUser
from backend.app.api.runs import business_for_user, current_business
from backend.app.db.session import business_session
from backend.app.engines.crawl import fetch_page
from backend.app.engines.crawl.contract import CrawlError, UnsafeUrlError
from backend.app.llm import ModelRouter
from backend.app.services.onboarding_service import (
    BusinessDnaDraft,
    BusinessNotFoundError,
    ThinSiteError,
    draft_dna_from_website,
    read_onboarding_state,
    save_confirmed_dna,
)

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

FetchHtml = Callable[[str], Awaitable[str]]


# --------------------------------------------------------------------------- #
# Dependencies -- overridable in tests, which is why they are functions
# --------------------------------------------------------------------------- #


def get_router() -> ModelRouter:
    """The model router. Overridden in tests with a stub."""
    return ModelRouter()


def get_fetcher() -> FetchHtml:
    """How a page is fetched. Overridden in tests so no network is touched."""

    async def fetch(url: str) -> str:
        result = await fetch_page(url)
        return result.html

    return fetch


# --------------------------------------------------------------------------- #
# Wire types
# --------------------------------------------------------------------------- #


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        """Reject anything that is not http(s) BEFORE it reaches the fetcher.

        The crawl engine refuses these too, but rejecting here means a malformed
        URL never becomes an outbound request at all, and the caller gets a 422
        describing their input rather than a 400 describing our guard.
        """
        candidate = value.strip()
        if not candidate:
            raise ValueError("url must not be empty")
        if not candidate.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if " " in candidate:
            raise ValueError("url must not contain spaces")
        return candidate


class UsageOut(CamelModel):
    tokens_in: int
    tokens_out: int
    usd: float
    latency_ms: int
    model: str


class PreviewResponse(CamelModel):
    dna: BusinessDnaDraft
    source_url: str
    usage: UsageOut
    needs_confirmation: bool
    instruction_like_content: bool
    fact_gaps: list[str]


class ConfirmRequest(CamelModel):
    """The draft as the owner is willing to have it stored.

    The DNA comes from the client rather than being re-crawled, because the owner is
    expected to have corrected it -- `BusinessDnaDraft` says "pending the owner's
    confirmation", and this is that confirmation.
    """

    dna: BusinessDnaDraft
    source_url: str = Field(min_length=1, max_length=2048)


class ConfirmResponse(CamelModel):
    """What was actually stored, echoed back rather than assumed.

    An owner needs to see that the website was recorded, because that key is what makes
    a later run crawl their site at all.
    """

    saved: bool
    website: str
    services: list[str]
    banned_claims: list[str]


def get_business_session_opener() -> Any:
    """The tenant-scoped session opener. Overridden in tests."""
    return business_session


@router.post(
    "/confirm",
    response_model=ConfirmResponse,
    response_model_by_alias=True,
    summary="Store the Business DNA the owner confirmed",
)
async def confirm(
    payload: ConfirmRequest,
    business_id: Annotated[UUID, Depends(current_business)],
    open_session: Annotated[Any, Depends(get_business_session_opener)],
) -> ConfirmResponse:
    """Accept a confirmed draft. THE step that was missing.

    `/preview` drafted a DNA and handed it back, and nothing could accept it -- so the
    draft was shown to the owner and thrown away, and `businesses.dna` stayed `{}` for
    every business ever created. Two consequences, neither cosmetic: the
    regulated-claim guard reads `banned_claims` from there, so a dentist's forbidden
    phrases were never enforced for a real tenant; and HARVEST reads `website`, so no
    run could crawl the site the owner had just pasted in.

    Authenticated, unlike `/preview`. Preview needs no tenant because it stores nothing;
    this one writes to a specific business, and the business comes from the SESSION
    rather than the body -- accepting a business id from the client would be letting the
    client make an authorisation decision.
    """
    async with open_session(business_id) as session:
        try:
            stored = await save_confirmed_dna(
                business_id,
                dna=payload.dna,
                source_url=payload.source_url,
                session=session,
            )
        except BusinessNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_error("business_not_found", "No such business."),
            ) from exc

    return ConfirmResponse(
        saved=True,
        website=str(stored.get("website") or ""),
        services=list(stored.get("services") or []),
        banned_claims=list(stored.get("banned_claims") or []),
    )


class StateResponse(CamelModel):
    """Whether this account has a business, and whether it has been onboarded."""

    #: False when there is no business row at all. Reported SEPARATELY from
    #: `onboarded` because the two states need different screens: a business that has
    #: not been onboarded gets the onboarding form, while an account with NO business
    #: cannot onboard at all -- `POST /onboarding/confirm` writes to a specific
    #: business and has none to write to -- so offering it the form would be offering
    #: a button that 409s.
    has_business: bool
    onboarded: bool
    name: str | None
    website: str | None


@router.get(
    "",
    response_model=StateResponse,
    response_model_by_alias=True,
    summary="Whether this account has a business, and whether it is onboarded",
)
async def state(
    user: CurrentUser,
    open_session: Annotated[Any, Depends(get_business_session_opener)],
) -> StateResponse:
    """The read the dashboard needs in order to lead with the right thing.

    Nothing exposed this, and the consequence was on the first screen every new owner
    sees: the homepage led with "Start a run" and listed "Onboard a business" fifth,
    under a heading called "Elsewhere" -- while a run without a confirmed DNA cannot do
    anything at all. INTAKE exits immediately with "no business profile", by design
    ("ask, never guess"). So the product's own first instruction was the one step it
    could not yet take, and the screen had no way to know.

    **Deliberately NOT behind `current_business`.** That dependency raises 409
    `no_business` for an account with no business row, which made this read fail in
    exactly the state that most needs an answer: a platform admin granted the role by
    `scripts/grant_platform_admin.py`, or an owner whose business was removed, has no
    business -- so the screen asked "should I show the onboarding prompt?", got a 409,
    and showed nothing. This route takes the USER and reports the absence as data.
    """
    from backend.app.db.session import session as plain_session

    async with plain_session() as db:
        business_id = await business_for_user(user.id, session=db)

    if business_id is None:
        return StateResponse(has_business=False, onboarded=False, name=None, website=None)

    async with open_session(business_id) as session:
        result = await read_onboarding_state(business_id, session=session)
    return StateResponse(
        has_business=True,
        onboarded=result.onboarded,
        name=result.name,
        website=result.website,
    )


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.post(
    "/preview",
    response_model=PreviewResponse,
    response_model_by_alias=True,
    summary="Crawl a website and draft a Business DNA for the owner to confirm",
)
async def preview(
    payload: PreviewRequest,
    model_router: Annotated[Any, Depends(get_router)],
    fetch_html: Annotated[FetchHtml, Depends(get_fetcher)],
) -> PreviewResponse:
    try:
        outcome = await draft_dna_from_website(
            payload.url, router=model_router, fetch_html=fetch_html
        )
    except ThinSiteError as exc:
        # The caller's site, not our fault and not a crash. Tell them what to do.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_error(
                "thin_site",
                "There is not enough text on that page to describe the business "
                "reliably. Please complete the short form instead — we would rather "
                "ask than guess.",
            ),
        ) from exc
    except UnsafeUrlError as exc:
        # Deliberately generic: the underlying message names the blocked address,
        # which would turn this endpoint into a probe for our internal network.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_error("unsafe_url", "That address cannot be fetched."),
        ) from exc
    except CrawlError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error(
                "site_unreachable",
                "That website could not be reached just now. Please check the "
                "address, or try again in a moment.",
            ),
        ) from exc
    except ValueError as exc:
        # The model answered in prose instead of calling the tool.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_error(
                "extraction_failed",
                "We could not read that page into a business profile. Please "
                "complete the short form instead.",
            ),
        ) from exc

    return PreviewResponse(
        dna=outcome.dna,
        source_url=outcome.source_url,
        usage=UsageOut(
            tokens_in=outcome.usage.tokens_in,
            tokens_out=outcome.usage.tokens_out,
            usd=float(outcome.usage.usd),
            latency_ms=outcome.usage.latency_ms,
            model=outcome.usage.model,
        ),
        needs_confirmation=outcome.needs_confirmation,
        instruction_like_content=outcome.instruction_like_content,
        fact_gaps=outcome.fact_gaps,
    )
