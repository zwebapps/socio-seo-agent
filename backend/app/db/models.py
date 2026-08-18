"""ORM models for Phases 1-6 of docs/BUILD_ORDER.md.

The full target schema is drawn in docs/DIAGRAMS.md section 11. Tables arrive
with the phase that needs them rather than all at once, so a migration is never
speculative.

Present here: users, businesses, documents, kb_chunks, crawl_pages, runs,
run_events, model_usage, actions, opportunities, content_pieces.
Still to come: keywords, competitors, geo_prompts, geo_results, social_posts,
leads, feedback, learned_style, approvals.
"""

from datetime import datetime
from decimal import Decimal
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


class User(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), nullable=False
    )

    businesses: Mapped[list["Business"]] = relationship(back_populates="owner")


class Business(Base, UuidPkMixin, TimestampMixin):
    """A customer's business. The unit of tenancy for everything else."""

    __tablename__ = "businesses"

    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
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

    __table_args__ = (
        CheckConstraint(
            "state in ('queued','running','awaiting_approval','done','failed','partial')",
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
