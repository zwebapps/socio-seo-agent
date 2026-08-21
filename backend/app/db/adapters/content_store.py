"""Persistence for produced artifacts. Today that means the landing page.

A landing page is a ``content_pieces`` row and not a table of its own. The column
set already carries everything one needs -- ``surface`` says what kind of artifact
it is, ``title``/``slug``/``body_md`` are the human-readable content, ``meta`` is
JSONB for the structured spec, and ``status`` is the vocabulary the approval flow
and the public form endpoint already read (``LIVE_FORM_STATUSES``). A second table
would have split "a produced artifact" across two places and given the lead loop two
foreign keys to choose between, when ``leads.content_piece_id`` and
``short_links.content_piece_id`` already point here.

**The structured spec lives in ``meta['landing']``, and the rendered HTML is not
stored at all.** The page is re-rendered from the spec on every request by the pure
``engines.landing.render_landing_page``, so a template fix improves every page that
was ever generated instead of only the ones generated afterwards. ``body_md`` holds a
plain-text rendition for export and for the review screen, because the column is
``NOT NULL`` and because a page nobody can read outside a browser is a page nobody
can edit.

Every method here is business-scoped through
:func:`~backend.app.db.session.business_session`, **with one deliberate exception**:
:meth:`resolve_landing_page`, which serves the public page. That one is the third
member of the ``SECURITY DEFINER`` family added by migrations ``7c1e4a90b2d5`` and
``4d2b7f9c1e83`` -- see the module docstring of ``lead_store`` for the full argument,
which applies here unchanged: the visitor is anonymous, the id in the URL is the only
thing that names the tenant, and ``content_pieces`` has FORCE RLS so the restricted
role reads zero rows without a scope. No privileged connection is opened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import text

from backend.app.db.adapters.lead_store import UnknownContentPieceError
from backend.app.db.session import business_session, session

__all__ = [
    "LANDING_SPEC_KEY",
    "LANDING_SURFACE",
    "ContentPieceRecord",
    "LandingPageTarget",
    "PostgresContentStore",
    "UnknownContentPieceError",
]

#: ``content_pieces.surface`` for a landing page. The other surfaces in the product
#: are the article and the per-channel renderings.
LANDING_SURFACE: Final = "landing_page"

#: The article surface. `content_pieces.surface` is a plain string, and this is the
#: second value the product writes into it — named rather than inlined so the writer and
#: any future reader cannot disagree about the spelling.
ARTICLE_SURFACE: Final = "article"

#: Where the structured spec sits inside ``content_pieces.meta``. Namespaced rather
#: than spread across the top level so that another artifact kind can put its own
#: shape in the same column without colliding.
LANDING_SPEC_KEY: Final = "landing"


@dataclass(frozen=True, slots=True)
class ContentPieceRecord:
    """One ``content_pieces`` row, as the rest of the application sees it."""

    id: UUID
    business_id: UUID
    surface: str
    title: str
    slug: str | None
    status: str


@dataclass(frozen=True, slots=True)
class LandingPageTarget:
    """What the public page route needs to render, resolved from the id alone.

    ``status`` and ``surface`` are returned rather than judged. "Is this live?" and
    "is this even a landing page?" are rules about the endpoint, and the endpoint is
    where a reader will look for them -- and it needs to be able to answer all the
    refusals identically, which it cannot do if the store has already collapsed them
    into ``None``.
    """

    business_id: UUID
    content_piece_id: UUID
    status: str
    surface: str
    title: str
    slug: str | None
    spec: dict[str, Any]
    business_name: str
    locale: str


_INSERT_PIECE = text(
    """
    INSERT INTO content_pieces
        (id, business_id, opportunity_id, run_id, surface, title, slug, body_md, meta, status)
    VALUES
        (:id, :business_id, :opportunity_id, :run_id, :surface, :title, :slug, :body_md,
         (:meta)::text::jsonb, :status)
    """
)

#: The SECURITY DEFINER resolver, not a bare SELECT: ``content_pieces`` has FORCE
#: RLS and a public visitor has no business id to scope by -- the lookup is what
#: produces one. See migration ``4d2b7f9c1e83``.
_RESOLVE_LANDING = text("SELECT * FROM resolve_landing_page(:piece_id)")

_SET_STATUS = text(
    "UPDATE content_pieces SET status = :status, updated_at = now() WHERE id = :piece_id"
)

_PIECE_VISIBLE = text("SELECT 1 FROM content_pieces WHERE id = :piece_id")


class PostgresContentStore:
    """Content-piece persistence. Stateless, so one instance is shared safely."""

    async def create_landing_page(
        self,
        business_id: UUID,
        *,
        title: str,
        slug: str | None,
        body_md: str,
        spec: dict[str, Any],
        run_id: UUID | None = None,
        opportunity_id: UUID | None = None,
        status: str = "draft",
    ) -> ContentPieceRecord:
        """Store one landing page and return it.

        ``status`` defaults to ``draft`` deliberately: a generated page has not been
        approved, and both the public page route and the public form endpoint refuse
        anything that is not ``approved`` or ``published``. Publishing an unreviewed
        page by default would make the approval gate optional.
        """
        piece_id = uuid4()
        async with business_session(business_id) as db:
            await db.execute(
                _INSERT_PIECE,
                {
                    "id": piece_id,
                    "business_id": business_id,
                    "opportunity_id": opportunity_id,
                    "run_id": run_id,
                    "surface": LANDING_SURFACE,
                    "title": title,
                    "slug": slug,
                    "body_md": body_md,
                    "meta": json.dumps({LANDING_SPEC_KEY: spec}),
                    "status": status,
                },
            )
        return ContentPieceRecord(
            id=piece_id,
            business_id=business_id,
            surface=LANDING_SURFACE,
            title=title,
            slug=slug,
            status=status,
        )

    async def create_article_piece(
        self,
        business_id: UUID,
        *,
        title: str,
        body_md: str,
        run_id: UUID | None = None,
        status: str = "approved",
    ) -> ContentPieceRecord:
        """Store the ARTICLE a run wrote, as the piece everything else attributes to.

        The anchor row, and the reason it has to exist: a tracked short link and a
        queued social post both point at a `content_pieces` id, and until now the only
        thing that ever created one was the landing-page actuator. So under the
        founder's ruling that we host no landing page, nothing would have created the
        row that attribution hangs off — the clicks and the posts would have had
        nowhere to attach.

        `status` defaults to `approved` rather than `draft`, and that is safe here in a
        way it is not for a landing page: a landing page is SERVED, so an unreviewed
        one being live would make the approval gate optional, which is why
        `create_landing_page` defaults the other way. An article piece is served
        nowhere — it is a record of what the run produced and the anchor a post hangs
        off — and it is only ever created after REVIEW has been passed.

        No slug: nothing routes to an article. A slug on a row nothing serves is a
        value that would eventually be treated as a URL.
        """
        piece_id = uuid4()
        async with business_session(business_id) as db:
            await db.execute(
                _INSERT_PIECE,
                {
                    "id": piece_id,
                    "business_id": business_id,
                    "opportunity_id": None,
                    "run_id": run_id,
                    "surface": ARTICLE_SURFACE,
                    "title": title,
                    "slug": None,
                    "body_md": body_md,
                    "meta": json.dumps({}),
                    "status": status,
                },
            )
        return ContentPieceRecord(
            id=piece_id,
            business_id=business_id,
            surface=ARTICLE_SURFACE,
            title=title,
            slug=None,
            status=status,
        )

    async def article_for_run(self, business_id: UUID, run_id: UUID) -> UUID | None:
        """The article piece this run already has, if any.

        So queueing a run's posts twice reuses the anchor rather than creating a second
        one — two anchors would split the same run's clicks across two pieces and make
        "which content earned this" unanswerable.
        """
        async with business_session(business_id) as db:
            found = (
                await db.execute(
                    text(
                        "SELECT id FROM content_pieces "
                        "WHERE run_id = :run_id AND surface = :surface "
                        "ORDER BY created_at ASC LIMIT 1"
                    ),
                    {"run_id": run_id, "surface": ARTICLE_SURFACE},
                )
            ).scalar_one_or_none()
        return found

    async def resolve_landing_page(self, piece_id: UUID) -> LandingPageTarget | None:
        """Find a landing page by its id, with no tenant context.

        The unscoped read, and it is unscoped by DESIGN rather than by privilege --
        see the module docstring. Everything the caller does with the result runs
        under the business id returned here.
        """
        async with session() as db:
            row = (await db.execute(_RESOLVE_LANDING, {"piece_id": piece_id})).mappings().first()

        if row is None:
            return None
        meta = _as_json_dict(row["meta"])
        spec = meta.get(LANDING_SPEC_KEY)
        return LandingPageTarget(
            business_id=row["business_id"],
            content_piece_id=row["content_piece_id"],
            status=str(row["status"]),
            surface=str(row["surface"]),
            title=str(row["title"]),
            slug=row["slug"],
            spec=dict(spec) if isinstance(spec, dict) else {},
            business_name=str(row["business_name"]),
            locale=str(row["locale"] or "de"),
        )

    async def set_status(self, business_id: UUID, piece_id: UUID, status: str) -> None:
        """Move one piece's status, under this business's own scope.

        An RLS-blocked update is zero rows rather than an error, so a mismatched
        business id would report success having changed nothing. The caller is told
        instead -- with ``lead_store``'s exception rather than a second one of our
        own, because it is the same condition and one type per condition is what lets
        a caller handle it without knowing which adapter reported it.
        """
        async with business_session(business_id) as db:
            if (await db.execute(_PIECE_VISIBLE, {"piece_id": piece_id})).first() is None:
                raise UnknownContentPieceError(piece_id, business_id)
            await db.execute(_SET_STATUS, {"piece_id": piece_id, "status": status})


def _as_json_dict(value: Any) -> dict[str, Any]:
    """JSONB comes back as a dict, or as text when no codec is registered."""
    if isinstance(value, str):
        decoded: Any = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}
