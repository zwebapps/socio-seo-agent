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
- [ ] Two channel-limit tables disagree, and one is wrong. `agents/nodes.CHANNEL_LIMITS` is
  `Mapping[str, int]` (plain ceilings: linkedin 3000, facebook 2000, instagram 2200, x 280) while
  `evals/rubric.CHANNEL_LIMITS` holds the richer spec the rubric grades against (min_chars,
  hashtag ranges, link rules). The channel NAMES do not even match — `facebook` vs
  `facebook_post`, `instagram` vs `instagram_caption` — and the numbers conflict (linkedin's 3000
  is the rubric's *hard* max, not its 1700 editorial target). The rubric's own comment predicted
  this: "two copies of a platform limit is how the eval starts disagreeing with the product it is
  grading." Resolve when `channel_specs` lands in Phase 6; until then `engines/channel` takes
  limits as arguments so it is not a third copy
- [x] 10-payload prompt-injection corpus as a test — ten DISTINCT mechanisms in
  `backend/tests/agents/test_prompt_injection.py`, each naming its mechanism and its
  failure mode. It found a real hole: the `<<<UNTRUSTED_CONTENT>>>` fence could be
  CLOSED from inside by a page emitting our own marker, so everything after it read as
  trusted — `prompts.escape_markers` closes it. Read the module docstring before citing
  the file: it pins framing, escaping and structure, and the only two controls that
  hold whatever the model does are the tool allowlist and the claim gate, because
  neither consults a model. Behavioural resistance against live models is NOT measured
  and belongs in `evals/`
- [x] Per-node tool allowlist enforced in the runtime, not only documented —
  `agents/tools.py:NODE_TOOLS` is the single source of truth and every node reaches its
  engines through `NodeToolbox`. Our code asking for an ungranted tool raises; a MODEL
  asking for one (the injection case) is refused, logged at WARNING and recorded in
  `state["errors"]` as `tool_not_allowed`, while the legitimate output still lands. A
  test PARSES the §3 table out of `docs/AGENT_RUNTIME.md`, so doc and runtime cannot
  drift in either direction. Three doc names were wrong and are fixed: INTAKE was
  documented with no tools while it reads memory, `seo.nap` named a non-existent module,
  and REPACK's `social.validate` should be `channel.validate`
- [x] Regulated-claim guard from `dna.banned_claims` — new deterministic
  `engines/claims` (no LLM: "does this text contain one of these phrases" is
  computable). Handles case, whitespace, HTML (inline vs block tags), CSS-hidden text
  and comments, invisible characters, word boundaries with the hyphen counted as part
  of the word, bounded German inflection and umlaut transliteration. Precision-first:
  it does NOT detect paraphrases, and the 20-case eval corpus is asserted to produce
  zero false positives. Wired at VALIDATE, and the graph will not carry a failing
  verdict to REVIEW — so a forbidden claim cannot be approved, let alone exported.
  REPACK withholds an offending social post separately
- [x] CSRF beyond SameSite=Lax; `__Host-` cookie prefix — `core/csrf.py` validates
  `Origin` (then `Referer`) against the CORS allowlist on every state-changing request
  that CARRIES the session cookie, so the anonymous public lead form still accepts a
  submission from a landing page on any host. Chosen over a double-submit token because
  the frontend is a separate origin calling with `credentials: "include"`: page script on
  :3100 cannot read a cookie the API set on :8100, so the "double" half is unavailable —
  and double-submit's own weakness is the same-site subdomain case that is the whole
  reason `SameSite=Lax` is not enough. No frontend change: the browser sends `Origin`
  and script cannot forge it. The cookie also gains `__Host-`, conditional on the same
  predicate as `Secure` (local is plain HTTP, where a prefixed cookie is simply dropped),
  so the name is environment-dependent and `core/cookies.session_cookie_name` is the only
  reader — including in the tests, which no longer write the literal

## Phase 12 — Observability and evaluation
- [x] Langfuse seam, no-op without keys, redaction inside the tracer — `d9deedf`
- [x] 20 cases + 5 deterministic scorers (Ragas absent, marked so, not invented) — `d9deedf`
- [x] `evals/report.md` with RAG off vs on vs oracle — `d9deedf`
- [x] prompt v1-vs-v2 comparison as a FLAG — `--prompt-version {v1,v2}`; v1 kept EXECUTABLE, not described, and it immediately shrank the credit the prompt change deserves: of the 0.17 gain in `rag_on`, 0.14 was the rubric bug and only 0.03 the prompt. Without a runnable v1 the whole 0.17 would have been misattributed. Header names the version. Original text preserved (the comparison itself has now been done by hand across four live runs — see the note in `evals/run.py._user_prompt`; what is missing is the ability to run both arms in one invocation). Cheap-vs-strong is now runnable: `evals/run.py --tier {cheap,mid,strong}` overrides the GENERATE tier and the report header names it. Blocked for THIS credential only — its OpenRouter data policy refuses the mid and strong chains (404 `no endpoints matching your guardrail restrictions`), so only `--tier cheap` can run live until that account setting changes
- [ ] ⛔ Langfuse keys

## Phase 13 — Feedback → learned preferences
- [x] 4-axis rating + reject reason + proposal approval — `d9deedf`
- [x] Distil at 3+ occurrences into PROPOSED rules, applied only on approval — `d9deedf`

## Found while building, still open
- [x] RLS policies were not null-safe: an emptied tenant GUC RAISED instead of returning zero rows — `d9deedf`
- [x] `FakeProvider` returned empty arrays/objects, making every list-shaped tool untestable — `d9deedf`
- [x] `businesses.slug` column — migration `9a4f21c7de83` (unique, NOT NULL, no server default: a public address has no sensible default and an expression default would report alembic drift forever). `/go/{handle}` accepts the slug OR the old UUID, permanently — that string may already be in an Instagram bio, and for IG/TikTok the hub IS the conversion path. Slugs transliterate German properly (ü→ue, ß→ss) and fall back to `b-<id>` for a name with nothing slugifiable
- [x] Retire the privileged connection in `lead_store.resolve` for a `SECURITY DEFINER` function — migration `7c1e4a90b2d5` adds `resolve_short_link(varchar)` AND `resolve_form_target(uuid)` (the second lookup was unwritten); both are STABLE, pin `search_path`, are REVOKEd from PUBLIC and granted to `sma_app` only. `_privileged_factory`/`_privileged_session` deleted, so the public request path no longer opens a second privileged pool
- [x] Wire `resolver_can_bypass_rls()` into a startup check — RESOLVED BY REMOVAL, which is the better outcome: the check existed only to detect a deployment whose migration role lacked BYPASSRLS, and with the privileged connection retired there is no bypass to depend on. Replaced with a stronger test asserting the app role has NEITHER `rolsuper` NOR `rolbypassrls` — without that, every RLS assertion in the suite could pass vacuously
- [x] Refund the per-email rate counter on success — `WindowCounter.give_back` on both backends (Redis guards with EXISTS so DECR cannot resurrect an expired key, and clamps at 0); `login` refunds the EMAIL dimension only, never IP (refunding IP would make one valid credential an unlimited enumeration budget). Residual, deliberately open and now pinned by a test: an attacker who knows an address can still burn its 15-minute window, because the check must stay *before* argon2 to ration it at all
- [x] Body-size limit middleware — login hashes an unbounded password — `core/body_limit.py`,
  64 KiB default (8x the largest body any route declares, and deliberately above the lead
  form's own 8 KiB cap so that endpoint's control stays the one that bites), overridable per
  path prefix for a future upload route. Enforced twice, because once is not a limit:
  `Content-Length` before the stream is touched, and the stream itself chunk by chunk for a
  header that lies or is absent. Found while testing: FastAPI wraps its body read in a bare
  `except Exception` and turns anything into `400 There was an error parsing the body`, so
  the streaming refusal needs the send side to replace that response — a test app reading
  `request.body()` in the endpoint does not reproduce it. Plus a 1024-character ceiling on
  the `password` field itself (`auth.MAX_PASSWORD_FIELD_CHARS`), which is what stops login
  handing an unbounded string to argon2; it sits deliberately above the service's 256-char
  policy so a long passphrase still meets the readable refusal rather than the redacted 422
- [x] Proxy headers are a DEPLOYMENT REQUIREMENT — and the item's premise was half stale, which the fix corrects: `--proxy-headers` is already ON by default in uvicorn 0.52.3 (verified in the installed source), so the real knob is `FORWARDED_ALLOW_IPS`, whose `127.0.0.1` default silently discards `X-Forwarded-For` whenever the proxy is a separate container. Documented in README + the Dockerfile, with `core/proxy_trust.py` detecting the misconfiguration at runtime — including `FORWARDED_ALLOW_IPS=*`, which is WORSE than the bug (a client can then claim any address and evade the limit entirely). Still to wire: calling the detector from the request path, which waits on the middleware work landing
- [x] `docs/CHANNELS.md` §5 still lists `ua_hash`; no UA is stored — §5 now names the real columns and says why no UA is kept (a hash is re-identifying and adds nothing to attribution)

## Deferred (recorded so they are not rediscovered)
- [ ] `analytics` engine — GSC/GA4 cut from Track A: two OAuth flows for a metric that cannot move inside a project timeline
- [x] Verified the OpenRouter slugs — a real call returned and the catalogue lists 415 models — `5162efa`
- [x] `geo_results.run_id` column — migration `b5e73c1a8f42`. `save_outcomes(run_id=...)` makes a retry-vs-new-run explicit instead of inferred from a 6-hour window, which got the operator re-probe case wrong (a deliberate second measurement silently folded into the first). Nullable with NO synthetic backfill, and the timestamp fallback stays for pre-migration rows; not an FK to `runs`, because probing is not driven from an agent run and an FK would refuse a standalone probe
- [ ] Password denylist is 26 entries; a HIBP k-anonymity range check is the real answer
