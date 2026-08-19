"""``ProbeStore`` over ``geo_prompts`` + ``geo_results``.

Implements the port declared in ``backend.app.services.geo_service``. Two
questions decide everything in this module, and both have a wrong answer that
still looks like it works.

**What is a run?** ``geo_results`` carries no run id, so a run is defined by its
``probed_at`` stamp: one save writes one timestamp across every row of the batch.
That makes "the latest run" a single indexed lookup, and it makes a *retry*
identifiable -- a re-send of the same (prompt, model) pairs shortly after the
original folds back onto the rows it is retrying instead of appending a second
copy. Without that, a worker that died after writing half a run and then re-ran
would report twelve answers to six questions, and share of voice would be
computed from a sample that never happened. The window
(:data:`RUN_DEDUPE_WINDOW`) is the honest cost of having no run id: it is far
longer than a probe run takes and far shorter than the probing cadence, and a
caller that knows better can pass ``probed_at`` explicitly.

**Who owns the arithmetic?** Not this module. ``latest_share_of_voice`` rebuilds
``ProbeOutcome`` rows and hands them to the engine's own ``share_of_voice``, so
the rule that ``no_answer`` is excluded from the denominator is the *same code*
on the way in and on the way out, rather than two implementations that agree
until one of them is edited. That rule is the difference between a measurement
and a fabrication (docs/ROADMAP.md section 9), which is exactly the kind of thing
that must not exist twice.

One reconstruction detail worth stating, because it is the module's only
approximation: ``ProbeOutcome.prompt_id`` is a content hash over
``version|locale|category|text``, and ``geo_prompts`` stores no locale. It is
therefore recomputed from the business's own locale, which is what
``build_prompt_set`` used. If a business changes locale between runs the
fingerprint stops matching and ``diff_share_of_voice`` reports "not comparable" --
which is the correct answer anyway, because the questions really did change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Result, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.session import business_session
from backend.app.engines.geo import (
    BRAND_NAMING_CATEGORIES,
    ProbeOutcome,
    ShareOfVoice,
    prompt_id_for,
    resolve_locale,
    share_of_voice,
)

__all__ = ["RUN_DEDUPE_WINDOW", "PostgresProbeStore"]

#: How recently a run must have been written for a re-send to count as a retry of
#: it rather than as a new run. Generous against a slow run (120 probes at
#: concurrency 4, plus provider retries) and small against any sane probing
#: cadence, which is daily at the fastest. A caller with a real run identity
#: passes ``probed_at`` and none of this applies.
RUN_DEDUPE_WINDOW: Final = timedelta(hours=6)

#: Fallback when a business row somehow carries no locale. ``de`` matches the
#: column default, and the value only affects the reconstructed fingerprint.
_DEFAULT_LOCALE: Final = "de"

_UPSERT_PROMPT = text(
    """
    INSERT INTO geo_prompts (id, business_id, prompt, category, set_version, ordinal, active)
    VALUES (:id, :business_id, :prompt, :category, :set_version, :ordinal, true)
    ON CONFLICT (business_id, set_version, prompt) DO UPDATE SET
        category = EXCLUDED.category,
        ordinal = EXCLUDED.ordinal,
        updated_at = now()
    RETURNING id
    """
)

#: The timestamp of the most recent run that overlaps this batch, if it is recent
#: enough to be the run this batch is retrying.
_RECENT_RUN = text(
    """
    SELECT max(probed_at) AS probed_at
    FROM geo_results
    WHERE probed_at IS NOT NULL
      AND probed_at >= :floor
      AND geo_prompt_id IN :prompt_ids
    """
).bindparams(bindparam("prompt_ids", expanding=True))

_UPDATE_RESULT = text(
    """
    UPDATE geo_results SET
        mentioned = :mentioned,
        cited = :cited,
        no_answer = :no_answer,
        answer_excerpt = :answer_excerpt,
        competitors_seen = (:competitors_seen)::text::jsonb,
        error = :error,
        updated_at = now()
    WHERE geo_prompt_id = :geo_prompt_id
      AND provider = :provider
      AND model = :model
      AND probed_at = :probed_at
    """
)

_INSERT_RESULT = text(
    """
    INSERT INTO geo_results
        (id, business_id, geo_prompt_id, provider, model, mentioned, cited, no_answer,
         answer_excerpt, competitors_seen, error, probed_at)
    VALUES
        (:id, :business_id, :geo_prompt_id, :provider, :model, :mentioned, :cited, :no_answer,
         :answer_excerpt, (:competitors_seen)::text::jsonb, :error, :probed_at)
    """
)

_LATEST_RUN_AT = text("SELECT max(probed_at) AS probed_at FROM geo_results")

_RUN_ROWS = text(
    """
    SELECT
        p.prompt,
        p.category,
        p.set_version,
        r.provider,
        r.model,
        r.mentioned,
        r.cited,
        r.no_answer,
        r.answer_excerpt,
        r.competitors_seen,
        r.error
    FROM geo_results AS r
    JOIN geo_prompts AS p ON p.id = r.geo_prompt_id
    WHERE r.probed_at = :probed_at
    ORDER BY p.ordinal, p.prompt, r.provider, r.model
    """
)

_BUSINESS_LOCALE = text("SELECT locale FROM businesses WHERE id = :business_id")


class PostgresProbeStore:
    """Probe persistence, scoped to one business per call.

    Satisfies ``backend.app.services.geo_service.ProbeStore``. Stateless, so one
    instance is shared safely; each method opens its own tenant-scoped
    transaction.
    """

    async def save_outcomes(
        self,
        business_id: UUID,
        outcomes: Sequence[ProbeOutcome],
        *,
        probed_at: datetime | None = None,
    ) -> int:
        """Persist one run's outcomes, and return how many rows were written.

        Idempotent per (prompt, model, run): re-sending a run within
        :data:`RUN_DEDUPE_WINDOW` updates the rows it already wrote and adds only
        the ones that are missing, so a retried run neither duplicates rows nor
        splits itself into two runs.

        ``probed_at`` is an optional override for a caller that owns a real run
        identity, and for tests that need two runs a week apart. Everything else
        about the signature matches the ``ProbeStore`` port.

        A batch is expected to hold at most one probe per (prompt, provider,
        model); a repeat inside one batch lands on the same row, which is the same
        thing the retry rule does deliberately.
        """
        if not outcomes:
            return 0

        async with business_session(business_id) as db:
            prompt_ids = await self._resolve_prompts(db, business_id, outcomes)
            run_at = probed_at or await self._run_timestamp(db, list(prompt_ids.values()))

            written = 0
            for outcome in outcomes:
                params = _result_params(
                    business_id=business_id,
                    geo_prompt_id=prompt_ids[_prompt_key(outcome)],
                    outcome=outcome,
                    probed_at=run_at,
                )
                updated = await db.execute(_UPDATE_RESULT, params)
                if _rowcount(updated) == 0:
                    await db.execute(_INSERT_RESULT, params)
                written += 1
            return written

    async def latest_share_of_voice(self, business_id: UUID) -> ShareOfVoice | None:
        """The most recent run's score, or ``None`` if there is none.

        ``None`` means "never probed", which is a first run rather than an error --
        ``diff_share_of_voice`` renders it as a baseline. Returning a zeroed score
        instead would be a claim that the brand is never mentioned, measured from
        nothing.
        """
        async with business_session(business_id) as db:
            run_at = (await db.execute(_LATEST_RUN_AT)).scalar_one_or_none()
            if run_at is None:
                return None

            rows = (await db.execute(_RUN_ROWS, {"probed_at": run_at})).mappings().all()
            if not rows:
                return None

            locale = (
                await db.execute(_BUSINESS_LOCALE, {"business_id": business_id})
            ).scalar_one_or_none()

        language = resolve_locale(str(locale) if locale else _DEFAULT_LOCALE)
        return share_of_voice([_outcome_from_row(row, locale=language) for row in rows])

    async def _resolve_prompts(
        self,
        db: AsyncSession,
        business_id: UUID,
        outcomes: Sequence[ProbeOutcome],
    ) -> dict[tuple[str, str], UUID]:
        """Ensure a ``geo_prompts`` row exists per question, and map it by key.

        Prompts are deactivated rather than deleted and are unique on
        (business, set_version, prompt), so the same question across runs is the
        same row -- which is what keeps a stored history interpretable.
        """
        ids: dict[tuple[str, str], UUID] = {}
        for ordinal, outcome in enumerate(outcomes):
            key = _prompt_key(outcome)
            if key in ids:
                continue
            result = await db.execute(
                _UPSERT_PROMPT,
                {
                    "id": uuid4(),
                    "business_id": business_id,
                    "prompt": outcome.prompt_text,
                    "category": outcome.category,
                    "set_version": outcome.set_version,
                    "ordinal": ordinal,
                },
            )
            ids[key] = result.scalar_one()
        return ids

    async def _run_timestamp(self, db: AsyncSession, prompt_ids: Sequence[UUID]) -> datetime:
        """The run this batch belongs to: a recent overlapping one, or a new one."""
        now = datetime.now(UTC)
        recent = (
            await db.execute(
                _RECENT_RUN,
                {"floor": now - RUN_DEDUPE_WINDOW, "prompt_ids": list(prompt_ids)},
            )
        ).scalar_one_or_none()
        if recent is None:
            return now
        return recent if isinstance(recent, datetime) else now


def _rowcount(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    ``AsyncSession.execute`` is typed as returning ``Result``, but a DML statement
    always yields a ``CursorResult``; the cast is a typing detail, not a runtime
    assumption.
    """
    return int(cast("CursorResult[Any]", result).rowcount)


def _prompt_key(outcome: ProbeOutcome) -> tuple[str, str]:
    """A question's identity in storage: its set version and its text."""
    return (outcome.set_version, outcome.prompt_text)


def _result_params(
    *,
    business_id: UUID,
    geo_prompt_id: UUID,
    outcome: ProbeOutcome,
    probed_at: datetime,
) -> dict[str, Any]:
    """One row's parameters, for the update and the insert alike.

    ``competitors_seen`` holds the names seen in the answer, mentioned or cited.
    The column cannot express the distinction (it is one list), so a reconstructed
    outcome reports them as mentions -- see the module docstring on what
    reconstruction is and is not exact about.
    """
    answered = outcome.status == "answered"
    seen = list(dict.fromkeys([*outcome.competitors_mentioned, *outcome.competitors_cited]))
    return {
        "id": uuid4(),
        "business_id": business_id,
        "geo_prompt_id": geo_prompt_id,
        "provider": outcome.provider,
        "model": outcome.model,
        "mentioned": outcome.mentioned,
        "cited": outcome.cited,
        "no_answer": not answered,
        "answer_excerpt": outcome.answer_excerpt or None,
        "competitors_seen": _json_array(seen),
        "error": outcome.error,
        "probed_at": probed_at,
    }


def _json_array(values: Sequence[str]) -> str:
    return json.dumps(list(values))


def _outcome_from_row(row: Any, *, locale: str) -> ProbeOutcome:
    """Rebuild one ``ProbeOutcome`` from a stored row.

    ``usd`` and ``latency_ms`` are not stored on ``geo_results`` (the cost ledger
    is ``model_usage``), so they come back as zero. Neither takes part in share of
    voice, so the reconstructed score is unaffected.
    """
    no_answer = bool(row["no_answer"])
    category = str(row["category"])
    competitors = _as_names(row["competitors_seen"]) if not no_answer else []

    return ProbeOutcome(
        prompt_id=prompt_id_for(
            set_version=str(row["set_version"]),
            locale=locale,
            category=category,
            text=str(row["prompt"]),
        ),
        prompt_text=str(row["prompt"]),
        category=category,  # type: ignore[arg-type]  # validated by the model
        set_version=str(row["set_version"]),
        prompt_contains_brand=category in BRAND_NAMING_CATEGORIES,
        provider=str(row["provider"]),
        model=str(row["model"]),
        status="no_answer" if no_answer else "answered",
        mentioned=bool(row["mentioned"]) and not no_answer,
        cited=bool(row["cited"]) and not no_answer,
        competitors_mentioned=competitors,
        answer_excerpt=str(row["answer_excerpt"] or ""),
        error=row["error"],
        usd=Decimal(0),
        latency_ms=0,
    )


def _as_names(value: Any) -> list[str]:
    """JSONB comes back as a list, or as text when no codec is registered."""
    if isinstance(value, str):
        decoded: Any = json.loads(value)
        return [str(name) for name in decoded] if isinstance(decoded, list) else []
    return [str(name) for name in value] if isinstance(value, list) else []


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check
    from backend.app.services.geo_service import ProbeStore

    def _satisfies_port(store: PostgresProbeStore) -> ProbeStore:
        """Fails type checking the moment this class drifts from the port."""
        return store
