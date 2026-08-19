# Backlog

The task queue. `/next` reads this file: it takes the topmost unchecked task whose
dependencies are checked, does that one task end to end, then stops.

Derived from [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md), which holds the reasoning
for the ordering. This file holds only the state. Where they disagree, BUILD_ORDER
is the argument and this is the record.

Legend: `[x]` done · `[ ]` open · `⛔` needs a human (money, secrets, irreversible
infra, or legal copy — `/next` must stop and ask, never proceed)

---

## Phase 0 — Foundations
- [x] Scaffold: uv project, compose, FastAPI health, Next.js shell, ruff/mypy/pytest, CI — `a534c4b`
- [x] Engine-boundary test installed before the first engine — `a534c4b`
- [x] Pin dev ports (:3100 web, :8100 api) — `aff33b0`

## Phase 1 — Walking skeleton
- [x] `crawl` engine: fetch, parse, SSRF guard, robots — `dd6eb06`
- [x] Onboarding service: URL → draft Business DNA, TDD — `bea58bd`
- [x] Onboarding API + `/onboard` UI, end to end in a browser — `d554969`
- [x] `GENERATE` writes a real article from the outline, with SEO fix hints on retry — `5c9409c`

## Phase 2 — Seams
- [x] Model router, two provider adapters, cost ledger, budget guard — `edede91`
- [x] `actions` table with unique idempotency key — `26684dc`

## Phase 3 — Knowledge base + agentic RAG
- [x] `kb` engine (extract, chunk, hash) + agentic retrieval loop with trace — `9a3aba8`
- [x] pgvector `ChunkStore`, `ProbeStore`, `RouterEmbedder` — `ddac077`
- [x] Fix: duplicate text cited the wrong document — `ddac077`
- [x] pdf/docx extractors behind a lazy import naming the missing package — `d9deedf`

## Phase 4 — seo + serp + NAP
- [x] `seo` engine: deterministic 0–100 score, quantitative fix hints, JSON-LD — `c7fc7a1`
- [x] `nap` engine: German-first consistency audit — `9683660`
- [x] Fix: switchboard extension and floor annotation were false positives — `7c3c39c`
- [x] `serp` engine: keyword expansion, intent, competitor discovery — `dbb1d5b`
- [ ] ⛔ `TAVILY_API_KEY` for real searches (structural half done, fake provider in use)

## Phase 5 — AI share of voice
- [x] `geo` engine + probe service, `no_answer` excluded from the denominator — `9a3aba8`
- [x] `geo_prompts` / `geo_results` tables with RLS — `bea58bd`

## Phase 6 — The graph
- [x] `AgentState`, run caps, JSON checkpoint round trip — `731670a`
- [x] Graph driver: retry loop, early exits, interrupt, resume, events — `731670a`
- [x] Wire the real nodes: 8 nodes, injected deps, HARVEST+VALIDATE call no model — `5c9409c`
- [x] Persist runs + run_events; resumable, terminating SSE stream — `3163123`

## Phase 7 — Memory
- [x] Business memory read at INTAKE and rendered one rule per line — `d9deedf`
- [ ] "What I remember about your business" panel, editable
- [x] A remembered preference asserted present in the assembled prompt — `d9deedf`

## Phase 8 — Lead loop
- [x] `short_links`, `link_clicks`, `leads` with RLS — `3163123`
- [x] Short-link service `/l/{code}` + link hub `/go/{id}` — `d9deedf`
- [ ] Landing page + CTA generation
- [x] Public form (honeypot, rate limit, size cap) + content→lead attribution — `d9deedf`

## Phase 9 — UI completion
- [x] Run timeline screen, resumable with a polling fallback — `3163123`
- [ ] Review tabs: draft · SEO findings · social · AI blocks
- [x] `/developer/models`: model picker, provider toggles, Ollama address, behind a server-side role check — `1e5f4c5`, `a8b541f`
- [ ] `/developer` extras: temperature and max-token sliders, prompt-version selector, tool toggles, cost dashboard

## Phase 10 — Auth and tenancy
- [x] RLS on every business-scoped table + isolation suite that derives its own table list — `26684dc`
- [x] argon2id + HMAC sessions, indistinguishable login failures, no-domain cookie — `6061732`
- [x] Refuse to boot on the default `SESSION_SECRET` outside local — `6061732`
- [x] Server-side revocation via `sessions_valid_from` in the signed token — `d9deedf`
- [x] Rate limit per IP AND per email, plus a concurrency gate capping argon2 at ~256 MiB — `d9deedf`

## Runtime configuration (added 2026-08-19, not in the original plan)
- [x] `model_routes` + `provider_settings` tables; empty config behaves as the code defaults — `9a9c3bc`
- [x] Admin API for routes and providers, with an unknown-provider guard — `9a9c3bc`
- [x] App-wide redaction of validation errors — a 422 was echoing a submitted API key — `9a9c3bc`
- [x] Ollama adapter + model catalogue; runs with no paid API at all — `5162efa`
- [x] Soft-UI design system and the admin screen — `1e5f4c5`
- [x] Login/signup page — `1e5f4c5`
- [x] `users.role` + platform-admin gate + out-of-band grant script — `a8b541f`
- [x] CORS allowlist derived-then-explicit, with an OpenAPI guard test (PUT was blocked) — `1e5f4c5`
- [ ] Per-business model override (`model_routes.business_id`) — one nullable column, no demand yet

## Phase 11 — Security hardening
- [ ] 10-payload prompt-injection corpus as a test
- [ ] Per-node tool allowlist enforced in the runtime, not only documented
- [ ] Regulated-claim guard from `dna.banned_claims`
- [ ] CSRF beyond SameSite=Lax; `__Host-` cookie prefix

## Phase 12 — Observability and evaluation
- [x] Langfuse seam, no-op without keys, redaction inside the tracer — `d9deedf`
- [x] 20 cases + 5 deterministic scorers (Ragas absent, marked so, not invented) — `d9deedf`
- [x] `evals/report.md` with RAG off vs on vs oracle — `d9deedf`
- [ ] prompt v1-vs-v2 and cheap-vs-strong comparison (needs a live key)
- [ ] ⛔ Langfuse keys

## Phase 13 — Feedback → learned preferences
- [x] 4-axis rating + reject reason + proposal approval — `d9deedf`
- [x] Distil at 3+ occurrences into PROPOSED rules, applied only on approval — `d9deedf`

## Found while building, still open
- [x] RLS policies were not null-safe: an emptied tenant GUC RAISED instead of returning zero rows — `d9deedf`
- [x] `FakeProvider` returned empty arrays/objects, making every list-shaped tool untestable — `d9deedf`
- [ ] `businesses.slug` column — `/go/{id}` takes a UUID because there is no slug to take
- [ ] Retire the privileged connection in `lead_store.resolve` for a `SECURITY DEFINER` function (SQL is written, in the module)
- [ ] Wire `resolver_can_bypass_rls()` into a startup check — a deployment whose owner lacks BYPASSRLS 404s every link with green tests
- [ ] Refund the per-email rate counter on success — an attacker can otherwise lock a victim out
- [ ] Body-size limit middleware — login hashes an unbounded password
- [ ] `--proxy-headers --forwarded-allow-ips` is a DEPLOYMENT REQUIREMENT, not optional: without it every client shares one rate-limit bucket
- [ ] `docs/CHANNELS.md` §5 still lists `ua_hash`; no UA is stored

## Deferred (recorded so they are not rediscovered)
- [ ] `analytics` engine — GSC/GA4 cut from Track A: two OAuth flows for a metric that cannot move inside a project timeline
- [x] Verified the OpenRouter slugs — a real call returned and the catalogue lists 415 models — `5162efa`
- [ ] `geo_results.run_id` column — run identity is currently a `probed_at` window
- [ ] Password denylist is 26 entries; a HIBP k-anonymity range check is the real answer
