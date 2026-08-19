"""What models can a user actually pick, per provider.

This feeds an admin model-picker, and that screen has one requirement that
decides every design choice below: **it must never fail and never lie.**

* **Never fail.** No key, no server, an error status, a body in an unexpected
  shape -- all of them return `known_models(provider)` instead of raising. A
  picker that 500s because someone's local Ollama is switched off is a worse
  outcome than a picker showing a slightly shorter list.
* **Never lie.** `priced` says whether the model has a row in `PRICE_TABLE`, and
  therefore whether we can report what a call cost. A model without one is not
  free -- it is unmeasured, and the two must not look alike. Showing "$0.00" for
  real spend would quietly defeat every budget ceiling in the system, so the flag
  travels with the row and the note says why.

Where each list comes from, and why:

* **openrouter** -- the live `GET /api/v1/models`, because the catalogue is
  hundreds of models long and changes weekly. It needs the key; with no key we
  fall back to the price table rather than showing nothing.
* **anthropic** -- derived from `PRICE_TABLE`. There is no usable public list
  endpoint without a key, and inventing one is not an option, so the honest
  source is the models we have actually priced.
* **ollama** -- the local `GET /api/tags`. Nothing local can be known in advance:
  it depends entirely on what the user has pulled.
* **fake** -- the `fake/*` tier ids from the price table, so the picker is never
  empty on a machine with no credentials at all. "Empty" there would read as
  "broken" when the truth is "running for free".

Sorting puts priced models first: those are the ones an admin can hold to a
budget, so they are the ones to reach for.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

import httpx
from pydantic import BaseModel

from backend.app.llm.pricing import PRICE_TABLE, is_priced

OPENROUTER: Final = "openrouter"
ANTHROPIC: Final = "anthropic"
OLLAMA: Final = "ollama"
FAKE: Final = "fake"

OPENROUTER_MODELS_URL: Final = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEY_ENV: Final = "OPENROUTER_API_KEY"

#: A list fetch sits in front of a person waiting on a screen, so it gets a much
#: shorter leash than a generation call.
CATALOGUE_TIMEOUT_S: Final = 5.0

LOCAL_NOTE: Final = "local; cost is not metered"
UNPRICED_NOTE: Final = "not in the price table; cost cannot be reported"
FAKE_NOTE: Final = "synthetic; no model is called and nothing is spent"


class CatalogueModel(BaseModel):
    """One selectable model, in the shape the admin screen renders.

    `id` is the exact string a route stores and an adapter sends, so it is the
    vendor's id verbatim -- never a prettified version, which would be stored and
    then rejected by the provider.
    """

    id: str
    provider: str
    label: str
    priced: bool
    context_tokens: int | None = None
    note: str | None = None


def _read_key(env: Mapping[str, str], name: str) -> str | None:
    """Return a usable key, treating blank and whitespace-only as absent.

    Same rule as the router's, deliberately duplicated rather than imported: this
    module must not import `router`, because `router` may reasonably want to
    import this one and a cycle between them is a startup crash.
    """
    value = env.get(name, "").strip()
    return value or None


def _price_table_provider(model_id: str) -> str:
    """Which provider a `PRICE_TABLE` id belongs to.

    The table's own id conventions carry this, so one rule reads it: a `fake/`
    prefix is the fake provider, any other slashed slug is an OpenRouter slug,
    and a bare id is Anthropic first party. Keeping it a rule rather than a second
    hand-maintained mapping means adding a price row cannot leave a model missing
    from the picker.
    """
    if model_id.startswith(f"{FAKE}/"):
        return FAKE
    if "/" in model_id:
        return OPENROUTER
    return ANTHROPIC


def _sorted(models: list[CatalogueModel]) -> list[CatalogueModel]:
    """Priced models first, then by id so the order is stable across calls."""
    return sorted(models, key=lambda model: (not model.priced, model.id))


def known_models(provider: str) -> list[CatalogueModel]:
    """The models we can price for `provider`, from `PRICE_TABLE`. No I/O.

    This is both the answer for Anthropic and the fallback for everyone else, so
    it must not touch the network or the environment. An unknown provider name
    returns an empty list rather than raising -- see the module docstring.
    """
    models = [
        CatalogueModel(
            id=model_id,
            provider=provider,
            # The id *is* the human-facing name for these: inventing a prettier
            # label than the vendor publishes risks naming two rows the same.
            label=model_id,
            priced=True,
            note=FAKE_NOTE if provider == FAKE else None,
        )
        for model_id in PRICE_TABLE
        if _price_table_provider(model_id) == provider
    ]
    return _sorted(models)


async def list_models(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[CatalogueModel]:
    """Models a user can pick for `provider`, live where a live list exists.

    Never raises. Any failure -- missing key, unreachable host, error status,
    unexpected body -- degrades to `known_models(provider)`.

    `client` exists so tests inject a transport; production passes nothing.
    """
    environ = env if env is not None else os.environ

    if provider == OPENROUTER:
        return await _openrouter_models(environ, client)
    if provider == OLLAMA:
        return await _ollama_models(base_url, client)
    # anthropic (no usable list endpoint), fake, and anything unknown.
    return known_models(provider)


async def _openrouter_models(
    env: Mapping[str, str],
    client: httpx.AsyncClient | None,
) -> list[CatalogueModel]:
    """Read OpenRouter's live catalogue, falling back to the price table."""
    key = _read_key(env, OPENROUTER_KEY_ENV)
    if key is None:
        # No request is even attempted: the endpoint needs the key, and a 401
        # would cost a round trip to learn what we already know.
        return known_models(OPENROUTER)

    payload = await _get_json(
        OPENROUTER_MODELS_URL,
        client=client,
        headers={"Authorization": f"Bearer {key}"},
    )
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return known_models(OPENROUTER)

    models: list[CatalogueModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        priced = is_priced(model_id)
        label = entry.get("name")
        context = entry.get("context_length")
        models.append(
            CatalogueModel(
                id=model_id,
                provider=OPENROUTER,
                label=label if isinstance(label, str) and label else model_id,
                priced=priced,
                context_tokens=context if isinstance(context, int) else None,
                note=None if priced else UNPRICED_NOTE,
            )
        )
    if not models:
        return known_models(OPENROUTER)
    return _sorted(models)


async def _ollama_models(
    base_url: str | None,
    client: httpx.AsyncClient | None,
) -> list[CatalogueModel]:
    """Whatever the user has pulled locally, all of it unpriced.

    Reuses `probe`'s parsing rather than re-reading `/api/tags`, so "what is
    installed" has exactly one implementation. An unreachable server yields an
    empty list -- honest, because nothing local can be known without asking.

    The import is local for the same reason `router.build_providers` does it:
    `ollama_provider` imports the `openai` SDK, and importing this module -- which
    an API route does on every startup -- must not drag a vendor SDK into a
    process that may have no provider configured at all.
    """
    from backend.app.llm.ollama_provider import DEFAULT_OLLAMA_BASE_URL, probe

    status = await probe(
        base_url or DEFAULT_OLLAMA_BASE_URL,
        timeout_s=CATALOGUE_TIMEOUT_S,
        client=client,
    )
    if not status.reachable:
        return known_models(OLLAMA)

    return _sorted(
        [
            CatalogueModel(
                id=name,
                provider=OLLAMA,
                label=name,
                priced=False,
                # No context length is exposed by `/api/tags`; a guess here would
                # be a number an admin might plan a prompt budget around.
                context_tokens=None,
                note=LOCAL_NOTE,
            )
            for name in status.models
        ]
    )


async def _get_json(
    url: str,
    *,
    client: httpx.AsyncClient | None,
    headers: Mapping[str, str] | None = None,
) -> object:
    """GET and parse JSON, returning `None` on any failure at all.

    Deliberately swallowing: every caller's answer to a failure is the same
    fallback, and a screen that renders a shorter list beats one that renders a
    stack trace.
    """
    http = client if client is not None else httpx.AsyncClient(timeout=CATALOGUE_TIMEOUT_S)
    try:
        try:
            response = await http.get(url, headers=dict(headers or {}), timeout=CATALOGUE_TIMEOUT_S)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload: object = response.json()
        except ValueError:  # json.JSONDecodeError subclasses ValueError
            return None
        return payload
    finally:
        if client is None:
            await http.aclose()
