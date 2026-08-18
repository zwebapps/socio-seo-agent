"""Engines: deterministic, read-only computation. No side effects.

An engine crawls, parses, scores, counts, or calls a read API and returns typed
data. It must NOT import an LLM client, a database session, an actuator, or an
agent -- that separation is the load-bearing rule of this architecture, and it
is enforced by tests/test_engine_boundary.py rather than by convention.

Engines that will live here (docs/ARCHITECTURE.md section 3):
    crawl  kb  seo  serp  geo  social  analytics

Anything with an external side effect belongs in an actuator instead, where
idempotency, approval policy, and the audit log are applied.
"""
