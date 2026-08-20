"""ORM models for Phases 1-6 of docs/BUILD_ORDER.md.

The full target schema is drawn in docs/DIAGRAMS.md section 11. Tables arrive
with the phase that needs them rather than all at once, so a migration is never
speculative.

Present here: users, businesses, documents, kb_chunks, crawl_pages, runs,
run_events, model_usage, actions, opportunities, content_pieces, geo_prompts,
geo_results, model_routes, provider_settings, sampling_policies, node_tool_policies,
short_links, link_clicks, leads, feedback, learned_style, platform_connections.
Still to come: keywords, competitors, social_posts, approvals.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base, BusinessScopedMixin, TimestampMixin, UuidPkMixin

# The embedding model's output width. Changing this is a migration, not a config
# edit, which is why it is a named constant rather than a magic number.
EMBEDDING_DIM = 1536


class Role(StrEnum):
    """What a user is allowed to do.

    Two scopes in one column, deliberately:

    * MEMBER and OWNER are BUSINESS-scoped — they describe a person's standing inside
      their own business, and neither can touch anything platform-wide.
    * PLATFORM_ADMIN is PLATFORM-scoped. It exists because model routing and provider
      settings have no business_id: they are the operator's cost-and-quality decisions,
      so a customer must not be able to change them.

    A second column would be tidier in the abstract, but there is exactly one
    platform-scoped capability today and a `role` a reviewer can read in a table row
    beats two columns nobody can hold in their head.
    """

    MEMBER = "member"
    OWNER = "owner"
    PLATFORM_ADMIN = "platform_admin"


class User(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), nullable=False
    )

    #: Defaults to OWNER: signup creates a business, and the person who created it owns
    #: it. PLATFORM_ADMIN is never reachable through signup -- it is granted out of band
    #: by scripts/grant_platform_admin.py, because a self-service path to it would be
    #: privilege escalation by HTTP request.
    role: Mapped[str] = mapped_column(
        String(32), default=Role.OWNER, server_default=Role.OWNER.value, nullable=False
    )

    #: Sessions issued before this instant are refused. The session token is stateless
    #: HMAC, so without this column logout only clears the BROWSER's copy and a stolen
    #: cookie stays valid for its full 30 days. Bumping this is the revocation.
    sessions_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    businesses: Mapped[list["Business"]] = relationship(back_populates="owner")

    __table_args__ = (
        CheckConstraint("role in ('member','owner','platform_admin')", name="role_valid"),
    )


class Business(Base, UuidPkMixin, TimestampMixin):
    """A customer's business. The unit of tenancy for everything else."""

    __tablename__ = "businesses"

    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The readable public address: ``/go/{slug}`` is the Instagram/TikTok bio
    #: link, and a v4 UUID is not something anyone can say out loud. Unique across
    #: the platform, because it is a public identifier -- two customers with the
    #: same name get different slugs (see ``slugify_business_name``).
    #: No server default on purpose -- see migration ``9a4f21c7de83``. A unique
    #: public address has no sensible default, so every writer must choose one.
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    website: Mapped[str | None] = mapped_column(String(2048))
    industry: Mapped[str | None] = mapped_column(String(120))
    locale: Mapped[str] = mapped_column(
        String(10), default="de", server_default="de", nullable=False
    )

    # Business memory: voice, services, audience, banned claims. JSONB because
    # its shape is owned by a Pydantic model that will evolve faster than the
    # schema should -- see BusinessDNA in the agent runtime.
    dna: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="businesses")


class Document(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A file the business uploaded. The source of grounded facts."""

    __tablename__ = "documents"

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # pdf|docx|md|txt|url
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # A scanned PDF yields no text. We flag it and offer OCR rather than
    # silently indexing nothing, which would look like success.
    extraction_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','processing','indexed','failed','no_text')",
            name="status_valid",
        ),
    )


class KbChunk(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One retrievable passage, with its embedding."""

    __tablename__ = "kb_chunks"

    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 of the chunk text. Identical text is never embedded twice -- the
    # cheapest cost saving in the ingest path.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_kb_chunks_document_id_ordinal"),
        # Declared here, not only in raw migration SQL: an index Alembic cannot see
        # is an index Alembic tries to DROP on the next autogenerate.
        #
        # HNSW rather than IVFFlat because IVFFlat needs representative data at
        # build time to choose its lists, and this table starts empty.
        Index(
            "ix_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class CrawlPage(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A snapshot of one page, as the crawl engine found it."""

    __tablename__ = "crawl_pages"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(1024))
    meta_description: Mapped[str | None] = mapped_column(Text)
    canonical: Mapped[str | None] = mapped_column(String(2048))
    h_tree: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    links: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    jsonld: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    word_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_crawl_pages_business_id_url", "business_id", "url"),)


class Run(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One execution of the agent graph. Resumable: state IS the checkpoint."""

    __tablename__ = "runs"

    goal: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", nullable=False, index=True
    )
    current_node: Mapped[str | None] = mapped_column(String(32))
    plan: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    completed: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    step_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0.50"), server_default=text("0.50"), nullable=False
    )
    used_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    # A run that resumed is not a failed run, but it is worth counting: a rising
    # number here means the workers are unstable.
    resumed_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    finished_reason: Mapped[str | None] = mapped_column(String(255))

    # `rejected` is its own terminal value rather than a flavour of `partial`, and the
    # reason is that they answer different questions. `partial` means a node fell short;
    # `rejected` means a person refused the output. Collapsing them would make "how often
    # do reviewers refuse what we produce" unanswerable in SQL and would blame the machine
    # for a human decision. Nothing in the graph can produce it -- only the reject route.
    __table_args__ = (
        CheckConstraint(
            "state in ('queued','running','awaiting_approval','done','failed','partial',"
            "'rejected')",
            name="state_valid",
        ),
    )


class RunEvent(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """Append-only timeline of a run.

    Every event streamed over SSE is also written here, so a browser reload
    replays the timeline from the database instead of showing an empty screen.
    The stream is a convenience; this table is the truth.
    """

    __tablename__ = "run_events"

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # started|done|failed|skipped
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),)


class ModelUsage(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One model call. The cost ledger.

    ``prompt_version`` is recorded so an evaluation can attribute a quality
    change to a prompt or to a model, rather than to folklore.
    """

    __tablename__ = "model_usage"

    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    node: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 8), default=Decimal("0"), server_default=text("0"), nullable=False
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class Action(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """An external side effect, and the record that makes it idempotent.

    The unique index on ``idempotency_key`` IS the lock. The row is inserted
    BEFORE the external call, so a crash mid-call leaves an ``in_flight`` row
    that the reconciler can ask the provider about -- rather than an invisible
    gap that a retry would turn into a double publish.
    """

    __tablename__ = "actions"

    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(32), default="in_flight", server_default="in_flight", nullable=False
    )
    external_ref: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status in ('in_flight','succeeded','failed','refused')", name="status_valid"
        ),
    )


class Opportunity(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A ranked growth opportunity the agent identified."""

    __tablename__ = "opportunities"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    target_keywords: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    expected_impact: Mapped[str] = mapped_column(String(16), nullable=False)  # high|medium|low
    effort: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="open", server_default="open", nullable=False
    )


class ContentPiece(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A produced artifact: article, landing page, or channel rendering."""

    __tablename__ = "content_pieces"

    opportunity_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL")
    )
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL")
    )
    surface: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(512))
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    seo_score: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(32), default="draft", server_default="draft", nullable=False, index=True
    )
    published_url: Mapped[str | None] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','needs_edit','approved','published','rejected')",
            name="status_valid",
        ),
    )


class GeoPrompt(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One question in the AI-visibility prompt set.

    The set is versioned because comparing this week's share of voice to last
    week's is only valid if the questions did not change. A prompt is deactivated
    rather than deleted, so historical results stay interpretable.
    """

    __tablename__ = "geo_prompts"

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    set_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "set_version",
            "prompt",
            name="uq_geo_prompts_business_id_set_version_prompt",
        ),
    )


class GeoResult(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One probe: what one model answered to one prompt, at one moment.

    ``no_answer`` is a first-class column, not an absence of data, and it is
    excluded from the share-of-voice denominator. A model outage or refusal must
    never be recorded as the brand being absent -- that is the difference between
    a measurement and a fabrication.
    """

    __tablename__ = "geo_results"

    geo_prompt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("geo_prompts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    mentioned: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    cited: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    no_answer: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )

    # A short excerpt only. The full answer is neither ours nor useful to keep,
    # and storing it would make this table grow without bound.
    answer_excerpt: Mapped[str | None] = mapped_column(Text)
    competitors_seen: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    #: Which probe run this row belongs to. Nullable because rows written before
    #: migration ``b5e73c1a8f42`` genuinely have no run identity -- see that
    #: migration for why one was not invented for them. Not a FK to ``runs``:
    #: probing is not driven from an agent run, so an FK would refuse a standalone
    #: probe. ``probed_at`` remains the run's TIME; this is its NAME.
    run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))

    # Declared here, not only in the migration: an index Alembic cannot see is an
    # index Alembic tries to DROP on the next autogenerate -- the same trap the
    # HNSW index above documents. Composite with business_id because every read is
    # tenant-scoped by RLS, so the planner wants both columns together.
    __table_args__ = (Index("ix_geo_results_business_run", "business_id", "run_id"),)


# --------------------------------------------------------------------------- #
# Runtime model configuration
#
# PLATFORM-level, deliberately: there is no business_id on either table, so neither
# carries RLS. Which model serves which task is an operator decision about cost and
# quality, not customer data -- and the router is a process-wide object, so making it
# per-business would mean resolving a tenant at every model call.
#
# Per-business override is one nullable column away if an agency ever needs it. It is
# not needed now, and adding it now would put a tenant lookup on the hot path of every
# node for nobody.
# --------------------------------------------------------------------------- #


class ModelRoute(Base, UuidPkMixin, TimestampMixin):
    """Which models serve one task class, in fallback order.

    Rows OVERRIDE the code defaults in ``llm/router.py``; an empty table behaves
    exactly as the code does. That is what makes this safe to ship: the feature adds a
    way to change the routing without becoming the only way it works.
    """

    __tablename__ = "model_routes"

    #: One row per task class. Unique, because two rows for GENERATE is an ambiguity
    #: with no correct resolution.
    task_class: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Ordered ``[{"provider": ..., "model": ...}]``. A list rather than one model
    #: because a single provider outage must not stop a run, and the order IS the
    #: fallback policy.
    chain: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    #: Who last changed it. Model choice moves cost and quality, so a change needs an
    #: author -- "why is generation suddenly worse" has to be answerable.
    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("tier in ('cheap','mid','strong','embed')", name="tier_valid"),
    )


class ProviderSetting(Base, UuidPkMixin, TimestampMixin):
    """Whether a provider may be used, and where to reach it.

    ``base_url`` exists for Ollama: a local server has no API key, so availability is a
    reachability question and the address is configuration rather than a secret. Hosted
    providers leave it NULL and are gated by their key in the environment.

    An API KEY IS NEVER STORED HERE. Keys stay in the injected environment
    (see CLAUDE.md): a key in a database row is a key in every backup, every replica
    and every screenshot of this table.
    """

    __tablename__ = "provider_settings"

    provider: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "provider in ('openrouter','anthropic','ollama','fake')", name="provider_valid"
        ),
    )


class SamplingPolicy(Base, UuidPkMixin, TimestampMixin):
    """Temperature and output ceiling for one task class.

    A separate table from ``model_routes`` rather than two more columns on it, and the
    reason is a real bug that shape would have: ``RouteConfigWriter.set_route`` DELETES
    the row when the chain is empty, because "use the default chain" and "use nothing"
    are different intentions. Sampling settings living on that row would be discarded
    the moment an operator reverted a route to its default chain -- silently, and with
    no way to notice except by watching output change.

    Both value columns are nullable and NULL means "send nothing, take the provider
    default", which is exactly what every call site does today. So an empty table
    behaves precisely as the code does -- the same property that makes ``model_routes``
    safe to ship ahead of its screen.

    The CHECK constraints duplicate the bounds in ``llm/sampling.py``. That duplication
    is deliberate: the Python bounds refuse a bad request, and these refuse a bad row,
    including one written by a migration, a fixture or a psql session.
    """

    __tablename__ = "sampling_policies"

    task_class: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    #: ``Numeric``, not ``float``, so a stored 0.7 reads back as ``0.7`` on a settings
    #: screen instead of ``0.7000000000000001``. Not because it is money -- it is not,
    #: and unlike ``usd`` it is serialised as a JSON number rather than a string.
    temperature: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)

    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "temperature is null or (temperature >= 0 and temperature <= 1)",
            name="temperature_in_range",
        ),
        CheckConstraint(
            "max_output_tokens is null or "
            "(max_output_tokens >= 1024 and max_output_tokens <= 8192)",
            name="max_output_tokens_in_range",
        ),
    )


class NodeToolPolicy(Base, UuidPkMixin, TimestampMixin):
    """Tools an operator has switched OFF for one graph node.

    **There is deliberately no ``granted`` column, and that absence is the security
    property.** ``agents/tools.NODE_TOOLS`` is a prompt-injection barrier
    (docs/AGENT_RUNTIME.md section 3), so this table may only ever narrow it: the
    effective set is ``NODE_TOOLS[node] - revoked``, a set difference, which cannot
    return a tool the code did not already grant whatever is stored here. A ``granted``
    column would make widening expressible from a browser, and a stolen admin cookie
    would then be enough to hand ``publish`` to the node that reads competitor HTML.

    See ``services/tool_policy.py`` for the full argument and
    ``backend/tests/services/test_tool_policy.py`` for the proof, which includes a test
    asserting this model has no column that could express a grant.
    """

    __tablename__ = "node_tool_policies"

    node: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    #: Tool names to remove from this node's allowlist. A name the node never held is
    #: inert rather than an error, because set difference ignores it -- but the API
    #: refuses an unknown name anyway, so an operator is never left believing they have
    #: switched off something they have not.
    revoked: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    updated_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# The lead loop
#
# Attribution is deliberately decoupled from PUBLISHING: we own the short link, so a
# click is measurable whether we posted the content or the owner pasted the caption by
# hand. Two channels in this product cannot carry a clickable link at all (Instagram and
# TikTok captions), which is exactly why the link hub exists.
# --------------------------------------------------------------------------- #


class ShortLink(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A tracked redirect. ``code`` is what appears in /l/{code}."""

    __tablename__ = "short_links"

    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_piece_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content_pieces.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str | None] = mapped_column(String(32))
    campaign: Mapped[str | None] = mapped_column(String(128))
    #: Denormalised counter so a list view needs no aggregate over link_clicks.
    click_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class LinkClick(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One click. No IP and no user agent are stored.

    A hashed user-agent would be a weak fingerprint and an IP is personal data under
    GDPR; neither is needed to attribute a lead to a piece of content, so neither is
    kept. Bot filtering uses the UA in memory and discards it.
    """

    __tablename__ = "link_clicks"

    short_link_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("short_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    referrer_host: Mapped[str | None] = mapped_column(String(255))
    is_bot: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )


class Lead(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A form submission, attributed to the content that produced the link."""

    __tablename__ = "leads"

    content_piece_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content_pieces.id", ondelete="SET NULL"), index=True
    )
    short_link_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("short_links.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(64), default="form", server_default="form")
    utm: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    #: Whatever the form collected. JSONB because a landing page's fields are generated
    #: per opportunity, so a fixed column set would be wrong by the second page.
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default="new", server_default="new", nullable=False
    )


# --------------------------------------------------------------------------- #
# Feedback, and what the agent learns from it
# --------------------------------------------------------------------------- #


class Feedback(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """A rating on one produced piece.

    Four axes rather than one score: "bad" is not actionable, while "on-brand 2,
    accuracy 5" says which prompt to change.
    """

    __tablename__ = "feedback"

    content_piece_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_pieces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # approved|rejected
    axes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("verdict in ('approved','rejected')", name="verdict_valid"),)


class LearnedStyle(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """Preferences distilled from feedback, PENDING the owner's approval.

    Never applied until approved. An agent that silently rewrote a business's brand
    rules from a few rejections would be changing the product's voice without consent —
    and the owner would have no way to see why the output drifted.
    """

    __tablename__ = "learned_style"

    rule: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="proposed", server_default="proposed", nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status in ('proposed','approved','rejected')", name="status_valid"),
    )


# --------------------------------------------------------------------------- #
# Connected platforms
# --------------------------------------------------------------------------- #


class PlatformConnection(Base, UuidPkMixin, BusinessScopedMixin, TimestampMixin):
    """One business's authorisation to act on one external platform account.

    The credential is the only genuinely dangerous column in this schema, so three
    things about it are deliberate.

    **It is encrypted, not hashed.** A password is only ever compared, so it is hashed
    and the plaintext discarded; an access token has to be presented to Facebook later,
    so it must be recoverable. See ``core/token_cipher`` -- the key comes from the
    environment, never from this database, because a key stored beside the ciphertext it
    protects protects nothing.

    **The plaintext has no read path.** ``credential_hint`` is a masked prefix/suffix,
    and it exists so that every screen, API response and log line has something safe to
    render. Only ``db/adapters/connection_store.reveal_*`` decrypts, and only the
    actuator calls it.

    **``status`` is a cache, not the truth.** A credential whose ``expires_at`` has
    passed is expired whether or not anything has written the row yet, so
    ``ConnectionView.unusable_reason`` folds the clock and is the authority every surface
    asks. The column exists so a SQL-level report agrees with the screens.

    ``refresh_credential_encrypted`` is nullable because not every platform issues one --
    and a connection without it cannot be renewed, only re-authorised, which is worth
    being able to see in a query.
    """

    __tablename__ = "platform_connections"

    #: One of ``services.platform_oauth.CONNECTABLE_PLATFORMS``. Constrained rather than
    #: free text: a typo'd platform is a connection nobody can ever publish through, and
    #: a CHECK turns that into an insert failure instead of a support ticket. Widening
    #: the list is a migration, on purpose.
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The account on the platform's side -- a Page id, a LinkedIn member urn. Part of
    #: the uniqueness rule, because a business can legitimately connect two Pages.
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: What the human recognises it as. Nullable: some platforms return an id and nothing
    #: else, and inventing a name would be inventing data.
    external_account_name: Mapped[str | None] = mapped_column(String(255))
    #: What was actually GRANTED, which is not always what was requested -- a token
    #: issued with a subset is the usual reason a publish fails with a permissions error
    #: long after the connection looked fine.
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="connected", server_default="connected", nullable=False
    )
    #: When the platform says the credential dies. Nullable means "no expiry stated",
    #: which is NOT "never expires" -- only the platform rejecting it can move that one.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Which cipher wrote the envelopes below. Queryable, so a key rotation can find the
    #: rows it has still to re-encrypt without opening every one of them.
    credential_scheme: Mapped[str] = mapped_column(
        String(32), default="", server_default="", nullable=False
    )
    #: The encrypted access credential. ``Text`` because an envelope's length depends on
    #: the token and the scheme, and a token that does not fit is a connection that cannot
    #: be stored. NULL after a revoke: disconnecting forgets the credential rather than
    #: leaving it decryptable in a row marked dead.
    credential_encrypted: Mapped[str | None] = mapped_column(Text)
    #: The encrypted refresh credential, where the platform issues one.
    refresh_credential_encrypted: Mapped[str | None] = mapped_column(Text)
    #: A masked prefix/suffix of the access credential. The ONLY credential-derived value
    #: any read path returns.
    credential_hint: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    #: True when the grant came from the fake OAuth provider, i.e. no real platform app
    #: was involved. Carried to the screen, because a simulated connection that looks
    #: identical to a real one is worse than no connection at all.
    fake: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status in ('connected','expired','revoked')",
            name="status_valid",
        ),
        CheckConstraint(
            "platform in ('facebook','instagram','linkedin','tiktok','youtube','google_business')",
            name="platform_known",
        ),
        # One row per business per account. A second authorisation of the same account
        # must UPDATE the credential rather than accumulate rows nobody can choose
        # between -- and "which of these three LinkedIn tokens is live" is not a question
        # a publish path should have to answer.
        UniqueConstraint(
            "business_id",
            "platform",
            "external_account_id",
            name="uq_platform_connections_business_id_platform_external_account_id",
        ),
    )
