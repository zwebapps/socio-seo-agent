"""Tests for the model router, provider adapters, and cost ledger.

Every test in this package is hermetic. There is no network: HTTP is faked with
`respx` or a stub provider is injected, and each module strips
`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` from the environment so a developer's
real key can never be picked up by the suite and spend real money.
"""
