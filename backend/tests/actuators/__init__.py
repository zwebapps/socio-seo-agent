"""Tests for the actuator layer: the only part of the system with side effects.

Every test in this package is hermetic. The email actuator's HTTP surface is driven
through an injected `httpx.MockTransport`, so there is no socket to escape through, and
`backend/tests/conftest.py` strips `RESEND_API_KEY` before the suite runs -- which is why
the configured cases pass `env={...}` explicitly rather than setting the environment.
"""
