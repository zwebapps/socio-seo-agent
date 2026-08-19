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

Both lookups go through a narrow ``SECURITY DEFINER`` function --
``resolve_short_link(varchar)`` and ``resolve_form_target(uuid)``, added by
migration ``7c1e4a90b2d5`` -- which is the same posture the rest of this codebase
uses for cross-tenant reads. Each function hard-codes its ``WHERE`` clause,
returns at most one row, is ``STABLE`` so it cannot write, pins its
``search_path``, and is executable by ``sma_app`` alone. **RLS is not weakened for
the application role:** these run on the ordinary restricted session, and every
read and write that follows uses ``business_session`` with the business id the
function returned, so a wrong answer here would land writes in the wrong tenant --
which is why the resolvers are tested directly.

The privilege is rationed by the function's own shape rather than by the caller's
restraint:

* **one statement, bound parameter, single row.** No dynamic SQL, no string
  building, and the input is a URL segment, so it is bound rather than
  interpolated.
* **read-only by declaration.** ``STABLE`` and ``LANGUAGE sql``, so no edit to
  this module can write through it.
* **the code is a credential.** 56**8 is about 46 bits from ``secrets`` (see
  ``link_service.new_code``), the lookup returns at most one row, and the row it
  returns is a redirect target that was created to be handed to the public
  anyway. ``content_pieces`` ids are v4 UUIDs, likewise unguessable.

What this replaced, recorded because the failure mode is instructive
-------------------------------------------------------------------
Until ``7c1e4a90b2d5`` both lookups ran on the **migration-role connection**: a
second, privileged pool, inside a request served to the public. It was rationed
carefully, but it also only worked where that role bypasses RLS -- ``FORCE ROW
LEVEL SECURITY`` binds the table owner too, so a deployment whose migration role
was a plain owner would have 404'd every short link while the rows sat in the
table. Locally and in CI that role is a superuser, so no test could have caught
it. A ``resolver_can_bypass_rls`` self-check existed to make that answerable at
startup; with the privileged connection gone there is no longer a question to
ask, so the check went with it.

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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
    "BusinessHandle",
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


@dataclass(frozen=True, slots=True)
class BusinessHandle:
    """A business identified from its public hub address.

    Carries the ``slug`` as well as the id so the caller can redirect a UUID
    request to the canonical readable address instead of serving two URLs for one
    page forever.
    """

    id: UUID
    name: str
    slug: str


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


#: The SECURITY DEFINER resolver, not a bare SELECT: ``short_links`` has FORCE RLS
#: and a public visitor has no business id to scope by -- the lookup is what
#: produces one. See migration ``7c1e4a90b2d5``.
_RESOLVE_LINK = text("SELECT * FROM resolve_short_link(:code)")

#: Likewise for the anonymous form submitter, whose only handle on the tenant is
#: the content-piece id in the URL.
_RESOLVE_FORM = text("SELECT * FROM resolve_form_target(:piece_id)")

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

#: The hub's lookup: a slug OR the UUID that used to be the only address.
#:
#: One statement rather than two round-trips, and an indexed equality on each side
#: -- ``id`` is the primary key and ``slug`` is UNIQUE -- so this is not the
#: full-table scan that the "derive it from the name at read time" idea would have
#: needed. The UUID cast is guarded by the caller: a handle that does not parse as a
#: UUID arrives as NULL, because ``'not-a-uuid'::uuid`` raises rather than returning
#: no rows.
_BUSINESS_BY_HANDLE = text(
    """
    SELECT id, name, slug
    FROM businesses
    WHERE slug = :handle OR id = :maybe_id
    LIMIT 1
    """
)


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

        This is the unscoped read, and it is unscoped by DESIGN rather than by
        privilege: ``resolve_short_link`` is a SECURITY DEFINER function with a
        hard-coded single-row ``WHERE``, executable only by the application role,
        called here on the ordinary restricted session. Everything the caller does
        afterwards runs under ``business_session`` with the business id returned
        here.
        """
        async with session() as db:
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

        Same necessity and same shape as :meth:`resolve`: the submitter is
        anonymous, so the content piece id in the URL is the only thing that can
        name the tenant, and the read goes through a SECURITY DEFINER function on
        the restricted session.
        """
        async with session() as db:
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

    async def business_by_handle(self, handle: str) -> BusinessHandle | None:
        """Resolve a hub address -- a slug, or the UUID the hub used to take.

        BOTH forms resolve, permanently. ``/go/{uuid}`` is the address that may
        already be printed on a flyer or pasted into an Instagram bio, and for
        Instagram and TikTok the hub IS the conversion path, so retiring that form
        would silently kill live campaigns. The slug is the address we hand out from
        now on; the UUID is the one we promised not to break.

        Read on the unscoped session for the same reason as :meth:`business_name`:
        ``businesses`` is the tenant table itself and carries no RLS policy.
        """
        try:
            maybe_id: UUID | None = UUID(handle)
        except ValueError:
            # Not a UUID: search by slug alone. Passed as NULL rather than omitted,
            # so the statement stays one prepared shape for both cases.
            maybe_id = None

        async with session() as db:
            row = (
                (await db.execute(_BUSINESS_BY_HANDLE, {"handle": handle, "maybe_id": maybe_id}))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return BusinessHandle(id=row["id"], name=str(row["name"]), slug=str(row["slug"]))

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
