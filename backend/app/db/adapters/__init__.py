"""Persistence adapters: the real implementations behind the service ports.

The services in ``backend/app/services`` talk to Protocols -- ``ChunkStore``,
``ProbeStore``, ``Embedder`` -- and never to a session. That is what keeps their
tests hermetic and their logic testable as logic. This package is the other half:
everything that actually knows about SQL, pgvector, and row-level security lives
here and nowhere else.

Two rules hold for every adapter in this package:

* **Every method is business-scoped, and scopes itself.** Each one opens
  :func:`backend.app.db.session.business_session`, which sets the transaction-local
  tenant GUC that the RLS policies key off. No adapter exposes a session
  parameter, so there is no way to call one *without* the scope -- and given that
  an unscoped query returns zero rows *silently* rather than erroring
  (docs/ARCHITECTURE.md section 6), that is the difference between a leak you can
  see and one you cannot.
* **The adapter owns the metric.** ``search`` returns pgvector's ``<=>`` distance
  raw, because only the adapter knows which operator ran. Converting it to a
  similarity here would force the service to guess the metric.
"""

from backend.app.db.adapters.chunk_store import (
    ChunkStoreError,
    EmbeddingWidthError,
    MissingEmbeddingError,
    PgVectorChunkStore,
)
from backend.app.db.adapters.probe_store import PostgresProbeStore

__all__ = [
    "ChunkStoreError",
    "EmbeddingWidthError",
    "MissingEmbeddingError",
    "PgVectorChunkStore",
    "PostgresProbeStore",
]
