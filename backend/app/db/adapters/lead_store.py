"""Persistence for the lead loop: ``short_links``, ``link_clicks``, ``leads``.

Every method here is business-scoped and scopes itself through
:func:`~backend.app.db.session.business_session` -- the rule this package states in
its ``__init__`` -- **with exactly two deliberate exceptions**, :meth:`resolve` and
:meth:`resolve_form`. Those two are the reason this docstring is long, because a
reviewer should be able to check the argument rather than take it on trust.

The unscoped lookups, and why they cannot be avoided
----------------------------------------------------

``GET /l/{code}`` and ``POST /public/forms/{id}`` are public. A visitor scanning a
QR code or filling in a landing-page form has no session and no business context,
so **there is no business id available to scope the lookup with** -- the lookup is
what produces it. And the tables cannot be read without one:

    ``short_links`` and ``content_pieces`` both have ``ENABLE`` *and* ``FORCE ROW
    LEVEL SECURITY`` with a policy keyed on the transaction-local GUC
    ``app.current_business_id``. With that GUC unset, the restricted application
    role reads **zero rows and raises nothing.**

That is asserted, not assumed: ``tests/db/test_lead_store.py``
::test_the_restricted_role_reads_nothing_from_short_links_unscoped proves it. So
``session()`` -- which uses the same restricted role -- is not an option here; it
would silently 404 every link in the product.

**The right long-term answer is a narrow SECURITY DEFINER function**, matching the
posture the rest of this codebase uses for cross-tenant reads. It needs a
migration, which this module cannot add, so the exact SQL is recorded in
:data:`RESOLVE_MIGRATION_SQL` below and swapping to it is a two-line change to
:meth:`resolve`.

Until then the two lookups run on the privileged (migration-role) connection, and
the privilege is rationed as tightly as it can be:

* **one statement, bound parameter, single row.** No dynamic SQL, no string
  building, and the input is a URL segment, so it is bound rather than
  interpolated.
* **the transaction is ``READ ONLY``**, declared before the query. So even a
  defect in this module cannot write through the privileged connection.
* **nothing else in the request runs unscoped.** The click write, the lead write
  and every read that follows use ``business_session`` with the business id this
  lookup returned -- so RLS is active for all of them, and if the resolve ever
  returned the wrong business the writes would land in the wrong tenant, which is
  why :meth:`resolve`'s answer is tested directly.
* **the code is a credential.** 56**8 is about 46 bits from ``secrets`` (see
  ``link_service.new_code``), the lookup returns at most one row, and the row it
  returns is a redirect target that was created to be handed to the public
  anyway. ``content_pieces`` ids are v4 UUIDs, likewise unguessable.

**The honest caveat**, recorded because it fails only in production: ``FORCE ROW
LEVEL SECURITY`` binds the table owner too, so this path works only where the
privileged role bypasses RLS. It does locally and in CI (the Postgres image makes
that role a superuser). A deployment whose migration role is a plain owner without
``BYPASSRLS`` would see every code 404 while the rows sat in the table.
:meth:`resolver_can_bypass_rls` answers that question explicitly so a deployment
check can fail loudly instead of a customer's printed flyer going dead.

What is deliberately never stored
---------------------------------

No IP address and no user agent, on any path. ``is_bot`` is computed in memory by
``link_service.is_bot`` and only the boolean arrives here; the referrer is reduced
to a host, because a referrer PATH can carry personal data. See ``LinkClick`` in
``backend/app/db/models.py`` for the reasoning, and
``test_no_user_agent_and_no_ip_reach_the_database`` for the assertion -- which
checks both the stored values and the shape of the table, so this cannot quietly
become untrue later.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import get_settings
from backend.app.db.session import business_session, session
from backend.app.services.link_service import (
    KNOWN_CHANNELS,
    apply_utm,
    build_utm,
    new_code,
    require_http_url,
    slugify,
)

__all__ = [
    "RESOLVE_MIGRATION_SQL",
    "CodeExhaustionError",
    "FormTarget",
    "HubCta",
    "LeadRecord",
    "LeadStoreError",
    "PostgresLeadStore",
    "ShortLinkRecord",
    "UnknownContentPieceError",
    "UnknownShortLinkError",
]


#: The migration that would retire the privileged connection in this module.
#:
#: Recorded here rather than in a comment because it is the actual remedy, and
#: because the shape of it is the argument: the function hard-codes its WHERE
#: clause, returns one row, is owned by the migration role, and is executable by
#: the application role only. Nothing about RLS is weakened for the app role --
#: this is the same posture the project already uses for cross-tenant reads.
#:
#: With it applied, :meth:`PostgresLeadStore.resolve` becomes a
#: ``business_session``-free call to ``SELECT * FROM resolve_short_link(:code)``
#: over the ordinary restricted session, and ``_privileged_session`` goes away.
RESOLVE_MIGRATION_SQL: Final = """
CREATE FUNCTION resolve_short_link(p_code varchar)
RETURNS TABLE (
    id uuid, business_id uuid, code varchar, target_url varchar,
    content_piece_id uuid, channel varchar, campaign varchar, click_count integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
STABLE
AS $$
    SELECT id, business_id, code, target_url,
           content_piece_id, channel, campaign, click_count
    FROM short_links
    WHERE code = p_code
    LIMIT 1
$$;

REVOKE ALL ON FUNCTION resolve_short_link(varchar) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_short_link(varchar) TO sma_app;
"""

#: How many fresh codes to try before declaring the generator broken.
#:
#: The unique index on ``short_links.code`` is the real uniqueness guarantee, so a
#: collision is expected-rare and a retry is the correct response. Five attempts,
#: not "until it works": a generator that always returns the same string is a bug,
#: and retrying it forever turns that bug into a hung request.
MAX_CODE_ATTEMPTS: Final = 5

#: The statuses a content piece must be in for its CTA to appear on the public
#: link hub. A draft landing page must not be advertised in an Instagram bio.
HUB_VISIBLE_STATUSES: Final = ("approved", "published")

#: Default page size for :meth:`PostgresLeadStore.list_leads`.
DEFAULT_LEAD_LIMIT: Final = 100
MAX_LEAD_LIMIT: Final = 500


class LeadStoreError(Exception):
    """Base class for failures raised by the lead store."""


class CodeExhaustionError(LeadStoreError):
    """Every generated code collided with an existing one.

    In practice this means the generator is not random -- a genuinely random 46-bit
    code colliding five times in a row is not something that happens to a real
    table.
    """

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"Could not find a free short-link code in {attempts} attempts. The "
            "unique index on short_links.code is doing its job; the generator is "
            "not. Check link_service.new_code."
        )


class UnknownShortLinkError(LeadStoreError):
    """The short link does not exist *for this business*.

    Raised rather than returning quietly because the two ways to get here are both
    bugs worth seeing: a stale id, or an attempt to attach one business's click or
    lead to another business's link. A foreign key does not respect row-level
    security, so nothing in the schema would have stopped the second one.
    """

    def __init__(self, link_id: UUID, business_id: UUID) -> None:
        self.link_id = link_id
        self.business_id = business_id
        super().__init__(
            f"short_link {link_id} is not visible to business {business_id}: it "
            "does not exist, or it belongs to another tenant."
        )


class UnknownContentPieceError(LeadStoreError):
    """The content piece does not exist for this business. Same reasoning as above."""

    def __init__(self, piece_id: UUID, business_id: UUID) -> None:
        self.piece_id = piece_id
        self.business_id = business_id
        super().__init__(
            f"content_piece {piece_id} is not visible to business {business_id}: it "
            "does not exist, or it belongs to another tenant."
        )


@dataclass(frozen=True, slots=True)
class ShortLinkRecord:
    """One row of ``short_links``, as the rest of the application sees it."""

    id: UUID
    business_id: UUID
    code: str
    target_url: str
    content_piece_id: UUID | None
    channel: str | None
    campaign: str | None
    click_count: int


@dataclass(frozen=True, slots=True)
class LeadRecord:
    """One captured lead."""

    id: UUID
    business_id: UUID
    content_piece_id: UUID | None
    short_link_id: UUID | None
    source: str
    utm: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HubCta:
    """One entry on the ``/go/{business}`` link hub.

    ``label`` falls back to the campaign slug when the link has no content piece,
    because there is no ``short_links.label`` column to read a nicer one from. A
    one-column migration would fix that; inventing a label here would not.
    """

    code: str
    label: str
    channel: str | None
    campaign: str | None
    click_count: int


@dataclass(frozen=True, slots=True)
class FormTarget:
    """Which business a public form belongs to, and whether it is live.

    ``status`` is returned rather than judged: "a draft landing page must not
    accept submissions" is a rule about the endpoint, and the endpoint is where a
    reader will look for it.
    """

    business_id: UUID
    content_piece_id: UUID
    status: str
    title: str


# --------------------------------------------------------------------------- #
# The privileged connection -- see the module docstring
# --------------------------------------------------------------------------- #

_privileged_factory: async_sessionmaker[AsyncSession] | None = None


def _get_privileged_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory on the migration role, created on first use.

    Separate from ``db.session``'s engine on purpose: that one is the restricted
    runtime role and must stay the default for everything. A tiny pool, because
    this connection serves exactly two single-row lookups.
    """
    global _privileged_factory
    if _privileged_factory is None:
        engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
        )
        _privileged_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return _privileged_factory


@asynccontextmanager
async def _privileged_session() -> AsyncIterator[AsyncSession]:
    """A read-only transaction on the privileged role.

    ``SET TRANSACTION READ ONLY`` is issued before anything else, so this
    connection cannot write even if a later edit to this module tries to. That is
    the one mitigation available for a privilege that cannot yet be removed.
    """
    factory = _get_privileged_factory()
    async with factory() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        yield db


_RESOLVE_LINK = text(
    """
    SELECT id, business_id, code, target_url,
           content_piece_id, channel, campaign, click_count
    FROM short_links
    WHERE code = :code
    LIMIT 1
    """
)

_RESOLVE_FORM = text(
    """
    SELECT business_id, id AS content_piece_id, status, title
    FROM content_pieces
    WHERE id = :piece_id
    LIMIT 1
    """
)

_CAN_BYPASS_RLS = text(
    """
    SELECT coalesce(bool_or(rolsuper OR rolbypassrls), false)
    FROM pg_roles
    WHERE rolname = current_user
    """
)

_INSERT_LINK = text(
    """
    INSERT INTO short_links
        (id, business_id, code, target_url, content_piece_id, channel, campaign)
    VALUES
        (:id, :business_id, :code, :target_url, :content_piece_id, :channel, :campaign)
    """
)

_LINK_VISIBLE = text("SELECT 1 FROM short_links WHERE id = :link_id")
_PIECE_VISIBLE = text("SELECT 1 FROM content_pieces WHERE id = :piece_id")

_INSERT_CLICK = text(
    """
    INSERT INTO link_clicks (id, business_id, short_link_id, referrer_host, is_bot)
    VALUES (:id, :business_id, :short_link_id, :referrer_host, :is_bot)
    """
)

#: Only a human click moves the counter. The bot row is still written -- "how much
#: of my bio-link traffic is link previewers?" is a real question -- but
#: ``click_count`` is read by an owner as "people who clicked", and a previewer is
#: not a person.
_COUNT_CLICK = text(
    "UPDATE short_links SET click_count = click_count + 1, updated_at = now() WHERE id = :link_id"
)

_INSERT_LEAD = text(
    """
    INSERT INTO leads
        (id, business_id, content_piece_id, short_link_id, source, utm, fields, status)
    VALUES
        (:id, :business_id, :content_piece_id, :short_link_id, :source,
         (:utm)::text::jsonb, (:fields)::text::jsonb, 'new')
    RETURNING created_at
    """
)

_LIST_LEADS = text(
    """
    SELECT id, business_id, content_piece_id, short_link_id, source,
           utm, fields, status, created_at
    FROM leads
    ORDER BY created_at DESC, id DESC
    LIMIT :limit
    """
)

_LIST_HUB = text(
    f"""
    SELECT sl.code,
           coalesce(cp.title, sl.campaign, '') AS label,
           sl.channel,
           sl.campaign,
           sl.click_count
    FROM short_links AS sl
    LEFT JOIN content_pieces AS cp ON cp.id = sl.content_piece_id
    WHERE sl.content_piece_id IS NULL
       OR cp.status IN {HUB_VISIBLE_STATUSES!r}
    ORDER BY cp.published_at DESC NULLS LAST, sl.created_at DESC
    """  # noqa: S608 - HUB_VISIBLE_STATUSES is a module constant, never caller input
)

_BUSINESS_FOR_OWNER = text(
    "SELECT id FROM businesses WHERE owner_id = :owner_id ORDER BY created_at LIMIT 1"
)

_BUSINESS_NAME = text("SELECT name FROM businesses WHERE id = :business_id")


class PostgresLeadStore:
    """The lead loop's persistence. Stateless, so one instance is shared safely."""

    # ----------------------------------------------------------------- links #

    async def create_link(
        self,
        business_id: UUID,
        *,
        target_url: str,
        content_piece_id: UUID | None = None,
        channel: str | None = None,
        campaign: str | None = None,
    ) -> ShortLinkRecord:
        """Create a tracked short link and return it.

        ``target_url`` is validated as an http(s) address before anything is
        written: it becomes the ``Location`` header of a public 302, so
        ``javascript:`` there would be stored XSS on our own domain.

        **The stored target is the tagged, final URL.** When both a channel and a
        campaign are given, the per-channel UTM parameters are merged into it
        (replacing any that were already there, never duplicating). If they were
        only built and not applied, the destination's own analytics would record an
        untagged referral and the channel comparison would exist nowhere but our
        database. A channel with no campaign is stored untagged rather than tagged
        with an invented campaign name.
        """
        clean_target = require_http_url(target_url)
        clean_channel = channel.strip().lower() if channel else None
        if clean_channel is not None and clean_channel not in KNOWN_CHANNELS:
            known = ", ".join(sorted(KNOWN_CHANNELS))
            raise ValueError(f"unknown channel {channel!r}. Known channels: {known}.")

        clean_campaign = slugify(campaign) if campaign else None
        if clean_channel and clean_campaign:
            clean_target = apply_utm(
                clean_target,
                build_utm(channel=clean_channel, campaign=clean_campaign),
            )

        if content_piece_id is not None:
            await self._assert_piece_visible(business_id, content_piece_id)

        for _ in range(MAX_CODE_ATTEMPTS):
            code = new_code()
            link_id = uuid4()
            try:
                async with business_session(business_id) as db:
                    await db.execute(
                        _INSERT_LINK,
                        {
                            "id": link_id,
                            "business_id": business_id,
                            "code": code,
                            "target_url": clean_target,
                            "content_piece_id": content_piece_id,
                            "channel": clean_channel,
                            "campaign": clean_campaign,
                        },
                    )
            except IntegrityError:
                # The unique index on `code` did its job. A fresh code, a fresh
                # transaction -- the failed one is already rolled back.
                continue
            return ShortLinkRecord(
                id=link_id,
                business_id=business_id,
                code=code,
                target_url=clean_target,
                content_piece_id=content_piece_id,
                channel=clean_channel,
                campaign=clean_campaign,
                click_count=0,
            )
        raise CodeExhaustionError(MAX_CODE_ATTEMPTS)

    async def resolve(self, code: str) -> ShortLinkRecord | None:
        """Find a link by its code, with no tenant context. See the module docstring.

        This is the unscoped read. It runs one bound, single-row, read-only
        statement on the privileged connection because ``short_links`` has FORCE
        RLS and a public visitor has no business id to scope by -- the lookup is
        what produces one. Everything the caller does afterwards runs under
        ``business_session`` with the business id returned here.
        """
        async with _privileged_session() as db:
            row = (await db.execute(_RESOLVE_LINK, {"code": code})).mappings().first()

        if row is None:
            return None
        return ShortLinkRecord(
            id=row["id"],
            business_id=row["business_id"],
            code=row["code"],
            target_url=row["target_url"],
            content_piece_id=row["content_piece_id"],
            channel=row["channel"],
            campaign=row["campaign"],
            click_count=int(row["click_count"]),
        )

    async def resolver_can_bypass_rls(self) -> bool:
        """Whether the privileged role can actually see past row-level security.

        A deployment check, not a hot path. ``FORCE ROW LEVEL SECURITY`` binds the
        table owner as well, so if the configured migration role is a plain owner
        without ``BYPASSRLS`` then :meth:`resolve` returns ``None`` for every
        existing code -- a silent, total outage of the redirect that looks exactly
        like "nobody has created any links yet". Locally and in CI the role is a
        superuser, so tests would never catch it. Ask this at startup and refuse to
        pretend.
        """
        async with _privileged_session() as db:
            return bool((await db.execute(_CAN_BYPASS_RLS)).scalar())

    async def record_click(
        self,
        link_id: UUID,
        business_id: UUID,
        *,
        referrer_host: str | None,
        is_bot: bool,
    ) -> None:
        """Record one click, scoped to the business that owns the link.

        ``referrer_host`` is a host, never a full referrer: a referrer path can
        carry a search query or a token. ``is_bot`` arrives already computed, and
        the user agent it was computed from is not passed in at all -- so there is
        no code path on which it could be stored.

        The link is checked for visibility first, under this business's own RLS
        scope. Without that check a mismatched business id would write nothing and
        report success, because an RLS-blocked write is zero rows rather than an
        error.
        """
        async with business_session(business_id) as db:
            visible = (await db.execute(_LINK_VISIBLE, {"link_id": link_id})).first()
            if visible is None:
                raise UnknownShortLinkError(link_id, business_id)

            await db.execute(
                _INSERT_CLICK,
                {
                    "id": uuid4(),
                    "business_id": business_id,
                    "short_link_id": link_id,
                    "referrer_host": referrer_host,
                    "is_bot": is_bot,
                },
            )
            if not is_bot:
                await db.execute(_COUNT_CLICK, {"link_id": link_id})

    # ----------------------------------------------------------------- leads #

    async def create_lead(
        self,
        business_id: UUID,
        *,
        fields: dict[str, Any],
        utm: dict[str, Any],
        short_link_id: UUID | None = None,
        content_piece_id: UUID | None = None,
        source: str = "form",
    ) -> LeadRecord:
        """Store one lead, attributed to the content that produced it.

        Both attribution references are checked under this business's own scope
        first. A foreign key is enforced database-wide and does not respect RLS, so
        without these checks business A could file a lead against business B's
        short link -- and B's attribution report would then contain a lead it never
        earned.
        """
        if short_link_id is not None:
            await self._assert_link_visible(business_id, short_link_id)
        if content_piece_id is not None:
            await self._assert_piece_visible(business_id, content_piece_id)

        lead_id = uuid4()
        async with business_session(business_id) as db:
            result = await db.execute(
                _INSERT_LEAD,
                {
                    "id": lead_id,
                    "business_id": business_id,
                    "content_piece_id": content_piece_id,
                    "short_link_id": short_link_id,
                    "source": source,
                    "utm": json.dumps(utm),
                    "fields": json.dumps(fields),
                },
            )
            created_at = result.scalar_one()

        return LeadRecord(
            id=lead_id,
            business_id=business_id,
            content_piece_id=content_piece_id,
            short_link_id=short_link_id,
            source=source,
            utm=dict(utm),
            fields=dict(fields),
            status="new",
            created_at=created_at,
        )

    async def list_leads(
        self, business_id: UUID, *, limit: int = DEFAULT_LEAD_LIMIT
    ) -> list[LeadRecord]:
        """This business's leads, newest first."""
        if not 1 <= limit <= MAX_LEAD_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LEAD_LIMIT}, got {limit}")

        async with business_session(business_id) as db:
            rows = (await db.execute(_LIST_LEADS, {"limit": limit})).mappings().all()

        return [
            LeadRecord(
                id=row["id"],
                business_id=row["business_id"],
                content_piece_id=row["content_piece_id"],
                short_link_id=row["short_link_id"],
                source=row["source"],
                utm=_as_json_dict(row["utm"]),
                fields=_as_json_dict(row["fields"]),
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def resolve_form(self, content_piece_id: UUID) -> FormTarget | None:
        """Which business a public form belongs to. The second unscoped read.

        Same necessity and same rationing as :meth:`resolve`: the submitter is
        anonymous, so the content piece id in the URL is the only thing that can
        name the tenant. One bound, single-row, read-only statement.
        """
        async with _privileged_session() as db:
            row = (
                (await db.execute(_RESOLVE_FORM, {"piece_id": content_piece_id})).mappings().first()
            )

        if row is None:
            return None
        return FormTarget(
            business_id=row["business_id"],
            content_piece_id=row["content_piece_id"],
            status=row["status"],
            title=row["title"],
        )

    # ------------------------------------------------------------------- hub #

    async def list_hub_ctas(self, business_id: UUID) -> list[HubCta]:
        """The CTAs the public link hub may show, freshest first.

        A link with no content piece is a standing CTA ("call us", "book a slot")
        and is always shown. A link that points at a content piece is shown only
        while that piece is approved or published -- the hub is the bio link for
        Instagram and TikTok, which have no clickable link of their own, so it is
        both the entire conversion path for those channels and the page most likely
        to be seen before anyone meant it to be.

        The join runs under RLS, so a piece belonging to another tenant is
        invisible and its link is filtered out rather than shown label-less.
        """
        async with business_session(business_id) as db:
            rows = (await db.execute(_LIST_HUB)).mappings().all()

        return [
            HubCta(
                code=row["code"],
                label=row["label"] or "",
                channel=row["channel"],
                campaign=row["campaign"],
                click_count=int(row["click_count"]),
            )
            for row in rows
        ]

    # --------------------------------------------------------------- helpers #

    async def business_for_owner(self, user_id: UUID) -> UUID | None:
        """The business this user owns, or ``None``.

        Uses the unscoped session, and that is correct rather than an exception:
        ``businesses`` carries no ``business_id`` and no RLS policy -- it *is* the
        tenant table -- which is the same reasoning ``api.auth.db_session``
        documents for ``users`` and ``businesses``.
        """
        async with session() as db:
            row = (await db.execute(_BUSINESS_FOR_OWNER, {"owner_id": user_id})).first()
        return UUID(str(row[0])) if row is not None else None

    async def business_name(self, business_id: UUID) -> str | None:
        """The business's display name, or ``None`` if there is no such business.

        Read on the unscoped session for the same reason as
        :meth:`business_for_owner`: ``businesses`` is the tenant table itself and
        carries no RLS policy. ``None`` is what turns an unknown hub id into a 404
        rather than an empty page for a business that does not exist.
        """
        async with session() as db:
            row = (await db.execute(_BUSINESS_NAME, {"business_id": business_id})).first()
        return str(row[0]) if row is not None else None

    async def _assert_link_visible(self, business_id: UUID, link_id: UUID) -> None:
        async with business_session(business_id) as db:
            if (await db.execute(_LINK_VISIBLE, {"link_id": link_id})).first() is None:
                raise UnknownShortLinkError(link_id, business_id)

    async def _assert_piece_visible(self, business_id: UUID, piece_id: UUID) -> None:
        async with business_session(business_id) as db:
            if (await db.execute(_PIECE_VISIBLE, {"piece_id": piece_id})).first() is None:
                raise UnknownContentPieceError(piece_id, business_id)


def _as_json_dict(value: Any) -> dict[str, Any]:
    """JSONB comes back as a dict, or as text when no codec is registered."""
    if isinstance(value, str):
        decoded: Any = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}
