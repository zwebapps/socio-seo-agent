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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from backend.app.engines.crawl import fetch_page
from backend.app.engines.crawl.contract import CrawlError, UnsafeUrlError
from backend.app.llm import ModelRouter
from backend.app.services.onboarding_service import (
    BusinessDnaDraft,
    ThinSiteError,
    draft_dna_from_website,
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
