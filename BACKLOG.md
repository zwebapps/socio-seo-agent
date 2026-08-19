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
- [x] "What I remember about your business" panel, editable — `/memory`: the exact prompt
  lines the next run receives, plus add / reword-in-place / remove over new session-scoped
  `GET·POST·PUT·DELETE /api/v1/memory[/preferences[/{id}]]`. Swept into `045bec1`
- [x] A remembered preference asserted present in the assembled prompt — `d9deedf`

## Phase 8 — Lead loop
- [x] `short_links`, `link_clicks`, `leads` with RLS — `3163123`
- [x] Short-link service `/l/{code}` + link hub `/go/{id}` — `d9deedf`
- [x] Landing page + CTA generation — CONVERSION was the missing link: a tracked short
  link pointing at a page that does not exist earns nothing. Split as the project's one
  rule dictates. **New `landing` engine** (pure, no LLM): a ten-rule deterministic
  conversion audit with the same weighting model as `seo` (weights sum to 100, graded
  severity, `passed = score >= 85 AND no error finding`) plus a total-function HTML
  renderer — "is there a form, and can it be answered" is set membership, and "what
  markup is this spec" is string building. **New `CONVERT` node**, between GENERATE and
  VALIDATE rather than after them, because a landing page makes a promise directly above
  a form and VALIDATE is where the regulated-claim gate runs; a node after VALIDATE would
  emit unchecked copy on the worst possible surface. It writes only what cannot be
  computed (headline, offer, sourced proof, per-channel CTA copy) and reads the business's
  own documents through `kb.search` — no `web_search`, because a proof point sourced from
  a page the business does not control is not proof of anything about the business.
  VALIDATE gained `landing.check` and now folds the landing copy into the SAME
  `claim_check`, so a banned claim in the conversion copy cannot reach REVIEW; the graph's
  backward edge now targets the earliest node whose output failed, so a landing-only
  failure re-runs CONVERT and not the strong-tier GENERATE. **Served** at
  `GET /p/{piece_id}` — public, zero-cookie, zero-JavaScript, one 404 for every refusal —
  over a third `SECURITY DEFINER` resolver (migration `4d2b7f9c1e83`; no table change,
  the page is a `content_pieces` row with its spec in `meta`). The public form endpoint
  now also accepts `application/x-www-form-urlencoded` and answers a browser with a 303
  back to `?sent=1`, because a JSON-only endpoint refuses every no-script visitor's lead
  AFTER they have typed it in; same schema, same honeypot, same size cap, same rate limit.
  One tracked short link per channel CTA, retargeted with its own `?ref=<code>` once the
  insert has minted it, so a lead names the exact link rather than only the channel. The
  whole chain is asserted end to end over real SQL with RLS on in
  `backend/tests/db/test_content_store.py`. (In the working tree, uncommitted.)
- [x] Public form (honeypot, rate limit, size cap) + content→lead attribution — `d9deedf`

## Phase 9 — UI completion
- [x] Run timeline screen, resumable with a polling fallback — `3163123`
- [x] Review tabs: draft · SEO findings · social · AI blocks — `GET /api/v1/runs/{id}/review`
  projects the checkpoint; every tab has an honest empty state naming the node that fills it.
  AI blocks are `outline.answer_blocks`. Swept into `045bec1`
- [x] `/developer/models`: model picker, provider toggles, Ollama address, behind a server-side role check — `1e5f4c5`, `a8b541f`
- [x] `/developer` extras — sliders bounded by arithmetic (temperature 0–1 because that is the MINIMUM our adapters accept, so a control able to emit 1.7 would 400 on an Anthropic fallback; max tokens floored at 1024 from a computed 905), tool toggles that can only NARROW and are now enforced at runtime, a cost dashboard on real `model_usage` rows, and a prompt-version INVENTORY rather than a dropdown — there is one runtime version per surface, so a one-entry dropdown pretending to be a choice would have been the dishonest answer — `08a95a5`

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
- [x] **`GET /api/v1/runs/{id}`, `/events` and `/review` 404'd for EVERY run against the real store** — fixed in `c8ee697`; see the fuller entry below. The tenant is now constructor-injected from the `current_business` dependency every route already had, so there was never a chicken-and-egg, and it exposed a second bug: `update`/`append_event` double-began a transaction, so checkpointing could never have worked either
- [x] **Nothing executed the graph** — `services/run_executor.py`, `f287b2b`; see the fuller entry below for the four things it has to get right and the in-process limitation
- [ ] **REPACK discards the hashtags it asked the model for.** `REPACK_TOOL` accepts
  `posts[].hashtags`, but the node stores only `renderings[channel] = body`, so they never reach
  the checkpoint and the social tab cannot show them
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
- [x] Password denylist is 26 entries; a HIBP k-anonymity range check is the real answer — `core/pwned.py`: SHA-1, first FIVE hex characters on the wire, comparison local, so the password and even its full hash never leave the process. Network is OFF unless `PWNED_PASSWORD_CHECK` is set (same posture as every other provider here), fails OPEN on an outage (a third party's downtime must not stop signups; the offline rules stay the floor), 2s timeout because this sits in front of a 64 MiB argon2 hash. Runs AFTER the offline policy so a too-short password is never transmitted. Hermeticity proven by re-running the suite with sockets hard-blocked

## Found while completing the implementation (2026-08-19)

- [x] **CI never ran a single test.** `.github/workflows/ci.yml` set `ENVIRONMENT: ci` with no
  `SESSION_SECRET`, and `create_app()` refuses to start outside `local` without one — at test
  COLLECTION time. So the Python job was green because ruff and mypy pass on code nobody executed.
  Fixed by generating an ephemeral secret per run (`openssl rand -hex 32` into `$GITHUB_ENV`) rather
  than storing a repository secret: it signs nothing that outlives the job, so there is no production
  value to leak and a fresh clone needs no manual setup to be honest. Also widened CI's mypy from
  `backend` to `backend evals` — the eval harness decides whether the product works and was unchecked.
  Verified by running the full suite under CI's exact environment.
- [x] **The test suite was not safe against two concurrent runs on one database.**
  `test_auth_api`'s teardown did `DELETE FROM users WHERE email LIKE 'authapi-test-%'` — a shared
  namespace, so parallel runs could delete each other's rows mid-test. It cost two rounds of false
  failure reports during this session's parallel agent work. `EMAIL_PREFIX` now carries the pid plus a
  random suffix, so the delete can only reach rows this process created.
  **Honest note on the verification:** two concurrent runs pass after the fix — but they also passed
  once WITH the shared prefix, because the race needs one run's teardown to fire while the other is
  mid-test, and it did not reproduce on demand in a two-run race. So the fix is sound by construction
  (a shared-prefix DELETE can only ever be a cross-run delete) rather than proven by a failing test.
  A deterministic reproduction would need the teardown held open against a known point in the other
  run, which is more machinery than the fix.
  Still open, and larger: every other `db` test file writes to the same shared tables, so the same
  class of interference exists wherever teardown is by pattern rather than by transaction rollback.
  The real answer is a transaction-per-test or a schema-per-run.
- [ ] **`docs/AGENT_RUNTIME.md` documents four tools that are granted but not wired**
  (`geo.probe`, `nap.audit` on HARVEST; `kb.search` on OPPORTUNITY/PLAN/GENERATE; `web_search` on
  GENERATE). `NodeToolbox.available()` now distinguishes "not granted" (design) from "not wired"
  (build state) instead of narrowing the doc to today's code.
- [ ] **Text extraction is NOT a security boundary, and is now measured rather than assumed.**
  `display:none`, `visibility:hidden` and HTML comments are dropped by trafilatura, but
  `font-size:0` and off-screen text survive into `main_text`, and instruction text in `img alt`
  survives into `facts`. Tests assert both the drops AND the survivals, so this cannot be upgraded
  into "hidden instructions never reach the model".
- [ ] **Login CSRF is not closed** (a pre-login request carries no cookie to check). `SameSite=Lax`
  blocks the cross-site POST; closing it properly needs a pre-session synchronizer token, which the
  Origin-validation design deliberately avoids.
- [ ] **A cookie-bearing request from a non-browser client is now refused** (no `Origin`/`Referer`).
  Intended — the cookie is a browser credential and there is no machine-to-machine mode — but it is a
  behaviour change for anyone curling with a session cookie.

## Found by the review-tabs work, fixed the same day (2026-08-19)

- [x] **Every run endpoint 404'd in production, and checkpointing could never have worked.**
  `PostgresRunStore._business_for` resolved the owning business from the run row on an UNSCOPED
  session — its docstring said "the OWNER session" but it imported `db.session.session`, which is
  the RESTRICTED role. Under FORCE RLS with no tenant GUC that reads zero rows, so the lookup
  returned `None` for every run and `GET /runs/{id}`, `/events` and `/review` all 404'd. Verified
  live: owner counts 1, app role counts 0. **The Phase 9 timeline screen had never worked outside
  tests**, because `InMemoryRunStore` is a dict with no RLS to fail against.
  Fixed by constructor-injecting the tenant (`PostgresRunStore(business_id)`) from the
  `current_business` dependency every route already had — so there was never a chicken-and-egg, and
  isolation is now the database's answer rather than an `if` in the route. Deliberately NOT the
  SECURITY DEFINER pattern used for `resolve_short_link`: that exists because a public visitor has
  no tenant context, whereas here the context existed and the right move is to use it rather than
  privilege a read past it. Zero protocol changes — the id goes in the constructor, not the methods.
  **The first bug was hiding a second:** `update` and `append_event` called `s.begin()` on a session
  `business_session` had already begun, which raises `InvalidRequestError`. They had never fired,
  because the broken lookup returned early first. So a run's checkpoint — the entire resumability
  mechanism — could not have been written in production either.
  `backend/tests/db/test_run_store.py` is new and is the test whose absence hid all of this: the
  store was only ever exercised against the in-memory implementation. An adapter whose whole job is
  to satisfy RLS cannot be tested against something that has no RLS.
- [x] **The `--edge` contrast token failed the 3:1 it existed to provide** — 1.57:1 light, 1.86:1
  dark, while `globals.css` claimed 3:1 in a comment. So the documented mitigation for the
  neumorphic ~1.2:1 shadow problem was delivering nothing, on every interactive control, in both
  themes. Now 3.37 / 3.57 against `--bg` with the worst surface checked too, and the focus ring got
  its own `--focus-ring` token (3.89 light) so the brand `--accent` did not have to move. Ratios
  recomputed independently from the hex values, not taken on trust.
- [x] **Nothing in the application executed the graph.** `run_graph`, `build_nodes` and
  `RunService.checkpoint` were called only from tests, so `POST /api/v1/runs` created a `queued` row
  that never advanced. `services/run_executor.py` is the join. Verified end to end against the live
  app + database: a run went `queued` → executed → `done` with 7 events at monotonic `seq`, INTAKE
  and HARVEST completing.
  Four things it gets right on purpose, each of which has a plausible wrong answer:
  ordered event persistence (the sink is SYNCHRONOUS and `record_event` is async, and a task per
  event races on `next_seq` — a duplicate is bad, a hole is worse because a resumed run reads it as
  a node that never ran); failures that reach the database (a fire-and-forget task whose exception
  nobody retrieves leaves the row saying `running` forever, indistinguishable from slow); strong
  task references (`create_task` returns the only one, and dropping it lets a run be
  garbage-collected mid-flight); and bounded concurrency (each run holds DB sessions AND calls a
  provider, so unbounded exhausts both at once).
  `serp_search` is wired ONLY when the provider is real — `get_serp_provider()` falls back to the
  fake, and wiring that would suppress HARVEST's honest "no provider configured" gap and make a run
  look researched. The executor emits its own timeline line naming what was actually wired, under
  the `EXECUTOR` label rather than borrowing a node's.
  Crawl results are summarised before entering state: the checkpoint is rewritten on EVERY node and
  `PageFacts.main_text` is a whole page body, so the raw result would put megabytes into JSONB ten
  times per run.
  **Honest limitation:** it runs IN the API process, so a restart mid-run leaves the row `running`.
  Runs were made resumable for exactly this, and `POST /runs/{id}/resume` is the recovery path — it
  refuses a finished run (re-running would overwrite approved work) and one awaiting approval (that
  is a person, not a stall). What is missing is a sweeper that finds stalled runs automatically,
  which is a worker's job; `ROADMAP` names ARQ/Redis and it is not installed.
- [x] **A provider outage was reported to the customer as a business judgement.** In `graph.py`,
  `if name == "OPPORTUNITY" and not state.get("opportunity")` returned "No opportunity met the bar
  for this business" — but `opportunity` is ALSO None when the node could not run. Observed live:
  OPPORTUNITY failed with `AllProvidersFailedError` (the credential's data policy refuses every
  mid-tier model) and the run finished `done`, telling the owner nothing about their business was
  worth writing about. Same fabrication the project forbids elsewhere: `no_answer` is excluded from
  the share-of-voice denominator precisely because "a model outage must never be recorded as the
  brand being absent". Now `_node_failure()` reads the `node_failed` record `_run_node` already
  writes, and a crash returns `partial` with the real cause named ("a failure to look, NOT a finding
  that nothing was worth writing about"), while a clean run that chose nothing keeps the original
  message. Matched on the error CODE, not on any error being present — `record_error` also carries
  ordinary degradations, so keying on "are there errors" would report a good judgement as a failure
  whenever a crawl lost a page. Three tests, including that one.
- [ ] `/api/v1/onboarding` has only `/preview` — nothing persists the drafted Business DNA, so
  `tone`/`audience`/`banned_claims` are empty for every business (created at signup with `dna = {}`).
  Consequence: the regulated-claim guard has no claims to enforce for a real tenant.
- [ ] `/auth/me` exposes no `businessId`, which is why the memory routes derive the tenant from the
  session instead of taking a path id like their `proposals` sibling.

## Closed after the developer console landed (2026-08-19)

- [x] **Tool revocations were stored, displayed and computed but NOT enforced.** `tool_policy` shipped
  `RUNTIME_ENFORCED = False` with a docstring naming the one call site that would change it, and the
  screen said so out loud — correct, because a UI implying a live kill switch that does nothing is
  worse than no UI. `agents/nodes._toolbox` now passes the revocations into
  `NodeToolbox(revoked=...)`, so revoking `publish`+`notify` from EXPORT is a deploy-free stop on
  every outward side effect. The un-widenable direction is structural, not validated: `allowed` is a
  set DIFFERENCE, so no stored value can add a capability. Loaded once per run (a switch thrown
  mid-run would give a run whose first half could publish and whose second half could not), and a
  read failure degrades to the code allowlist — the narrower answer — so a settings-table outage
  cannot become an inability to work. The test that pinned `RUNTIME_ENFORCED is False` is replaced by
  one asserting the flag AGREES WITH THE BEHAVIOUR, so the two cannot drift.
- [x] **`model_usage` was written by nothing.** The table called itself "the cost ledger" from the
  Phase 1 schema and had no writer, so it was structurally empty and the cost dashboard had to report
  its figures as unavailable rather than a confident `$0.00`. `services/usage_recorder.py` + a
  `usage_sink` on the router now write real rows. The sink is synchronous because it sits on every
  node's hot path, so it buffers and the executor flushes on each node boundary — per node, not per
  run, so a run that dies mid-flight still has a ledger for the nodes that finished. A failed flush
  logs and DROPS, clearing the buffer first: losing a row under-reports, retrying one over-reports
  spend, and over-reporting is the error an operator would act on. Proven against real Postgres with
  RLS on, including that B cannot see A's spend.
- [x] **Every LLM span was recorded with an empty run_id, business_id and node.** Found while wiring
  the ledger: nothing passed `trace` to `router.complete`, so `llm_span_fields` defaulted all three
  to `""`. Invisible because the tracer is a no-op without Langfuse keys — and it also meant a
  `model_usage` row could not have been attributed to anything even once a writer existed. The one
  node call site now passes business_id, node and prompt_version.
- [ ] **The ledger cannot be demonstrated end to end on the current credential.** A live run's only
  model call is at OPPORTUNITY, which routes to the MID tier and is refused by this OpenRouter
  account's data policy — and a failed call has no usage to record, correctly. So `model_usage` fills
  in the DB tests but stays empty on a real run here. Unblocked by the same account setting as the
  strong-tier eval.
