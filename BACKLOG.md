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
- [ ] PDF and DOCX extractors (registry entries currently raise; needs pypdf, python-docx)

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
- [ ] Persist runs and run_events; SSE endpoint for the timeline

## Phase 7 — Memory
- [ ] Business memory read at INTAKE and applied in the system prompt
- [ ] "What I remember about your business" panel, editable
- [ ] Cross-run demo: state a preference in run 1, see it obeyed in run 2

## Phase 8 — Lead loop
- [ ] `leads` + `short_links` tables with RLS
- [ ] Short-link service `/l/{code}` and the `/go/{business}` link hub
- [ ] Landing page + CTA generation
- [ ] Public form endpoint (honeypot, rate limit) and `content_piece → lead` attribution

## Phase 9 — UI completion
- [ ] Run timeline screen consuming SSE
- [ ] Review tabs: draft · SEO findings · social · AI blocks
- [x] `/developer/models`: model picker, provider toggles, Ollama address, behind a server-side role check — `1e5f4c5`, `a8b541f`
- [ ] `/developer` extras: temperature and max-token sliders, prompt-version selector, tool toggles, cost dashboard

## Phase 10 — Auth and tenancy
- [x] RLS on every business-scoped table + isolation suite that derives its own table list — `26684dc`
- [x] argon2id + HMAC sessions, indistinguishable login failures, no-domain cookie — `6061732`
- [x] Refuse to boot on the default `SESSION_SECRET` outside local — `6061732`
- [ ] Server-side revocation: `users.sessions_valid_from` folded into the signed token
- [ ] Rate-limit `/login` and `/signup` on Redis — argon2 at 64 MiB × 4 lanes makes login a memory-amplification DoS

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
- [ ] Langfuse tracing on every model and tool call, feedback attached as a score
- [ ] Eval set of 20 cases; Ragas faithfulness + a deterministic rubric
- [ ] `evals/report.md`: RAG off vs on, prompt v1 vs v2
- [ ] ⛔ Langfuse keys

## Phase 13 — Feedback → learned preferences
- [ ] Rating + 4-axis rubric + reject reason
- [ ] Distil recurring rejections into proposed brand rules the user approves

## Deferred (recorded so they are not rediscovered)
- [ ] `analytics` engine — GSC/GA4 cut from Track A: two OAuth flows for a metric that cannot move inside a project timeline
- [x] Verified the OpenRouter slugs — a real call returned and the catalogue lists 415 models — `5162efa`
- [ ] `geo_results.run_id` column — run identity is currently a `probed_at` window
- [ ] Password denylist is 26 entries; a HIBP k-anonymity range check is the real answer
