"""The search provider seam.

Same posture as the model router: a missing credential means the FAKE provider and a
status that says so, never a silent call to a paid service and never a crash. The
difference here matters more than it does for a model — a search that quietly
returns invented results would have the agent plan a month of content against
fiction, which is worse than having no search at all.
"""

import hashlib
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from backend.app.engines.serp.contract import (
    SerpConfigStatus,
    SerpPage,
    SerpProviderError,
    SerpResult,
)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_S = 8.0


class SerpProvider(Protocol):
    """One search, one page of results."""

    name: str

    async def search(self, query: str, *, locale: str, limit: int) -> SerpPage: ...


class FakeSerpProvider:
    """Deterministic, offline, and obviously synthetic.

    Hosts are ``example-N.test`` on purpose: a reviewer glancing at output can tell
    at once that no real search happened. A fake that produced plausible real
    domains would be indistinguishable from a live result in a screenshot.
    """

    name = "fake"

    async def search(self, query: str, *, locale: str, limit: int) -> SerpPage:
        digest = hashlib.sha256(f"{query}|{locale}".encode()).hexdigest()
        results = [
            SerpResult(
                position=i + 1,
                url=f"https://example-{digest[i * 2 : i * 2 + 2]}.test/{digest[:8]}",
                title=f"{query} — result {i + 1}",
                snippet=f"Synthetic result {i + 1} for {query!r}. No search was performed.",
                host=f"example-{digest[i * 2 : i * 2 + 2]}.test",
            )
            for i in range(max(0, limit))
        ]
        # Suffixes chosen to exercise every intent branch downstream, so a caller
        # running on the fake still sees a realistically-shaped mix.
        related = [
            f"{query} kosten",
            f"{query} in der naehe",
            f"wie funktioniert {query}",
            f"{query} vergleich",
        ]
        return SerpPage(query=query, locale=locale, results=results, related_queries=related)


class TavilySerpProvider:
    """Real search via Tavily.

    Chosen for the same reason as the rest of the free-tier stack: a usable free
    quota and no contract to sign for a course project. The shape is small enough
    that swapping to Brave or SerpAPI is one adapter, not a refactor.
    """

    name = "tavily"

    def __init__(self, api_key: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def search(self, query: str, *, locale: str, limit: int) -> SerpPage:
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": limit,
            "include_answer": False,
            "search_depth": "basic",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(TAVILY_ENDPOINT, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            # Wrapped, not leaked: the caller degrades on SerpProviderError and must
            # not have to know which HTTP library sits behind this seam.
            raise SerpProviderError(f"search provider unreachable: {exc}") from exc

        results: list[SerpResult] = []
        for i, item in enumerate(body.get("results", [])):
            url = str(item.get("url", ""))
            if not url:
                continue
            results.append(
                SerpResult(
                    position=i + 1,
                    url=url,
                    title=str(item.get("title", "")),
                    snippet=str(item.get("content", ""))[:400],
                    host=httpx.URL(url).host,
                )
            )

        return SerpPage(
            query=query,
            locale=locale,
            results=results,
            related_queries=[str(q) for q in body.get("follow_up_questions") or []],
        )


def _env_key(env: Mapping[str, str] | None) -> str | None:
    source = env if env is not None else os.environ
    key = source.get("TAVILY_API_KEY", "").strip()
    return key or None


def serp_config_status(env: Mapping[str, str] | None = None) -> SerpConfigStatus:
    """Whether searches are real. Reported explicitly, never inferred."""
    key = _env_key(env)
    if key is None:
        return SerpConfigStatus(
            provider="fake",
            using_fake=True,
            message=(
                "No TAVILY_API_KEY is set, so every search is served by the fake "
                "provider: results are synthetic and must not be presented as market "
                "data. Set TAVILY_API_KEY to search for real."
            ),
        )
    return SerpConfigStatus(
        provider="tavily",
        using_fake=False,
        message="Searches run against Tavily.",
    )


def get_serp_provider(env: Mapping[str, str] | None = None) -> SerpProvider:
    """The provider this environment can actually use."""
    key = _env_key(env)
    return FakeSerpProvider() if key is None else TavilySerpProvider(key)
