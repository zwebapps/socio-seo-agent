# Backlog

The task queue. `/next` reads this file: it takes the topmost unchecked task whose
dependencies are checked, does that one task end to end, then stops.

Derived from [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md), which holds the reasoning
for the ordering. This file holds only the state. Where they disagree, BUILD_ORDER
is the argument and this is the record.

Legend: `[x]` done · `[ ]` open · `[~]` superseded, do NOT build (the decision that
retired it is named in the entry, and its original text is kept so the reasoning is not
lost) · `⛔` needs a human (money, secrets, irreversible infra, or legal copy — `/next`
must stop and ask, never proceed)

`[~]` is deliberately not `[x]`: ticking a task nobody did would make this file lie about
what was built, and leaving it `[ ]` would make `/next` pick up work the founder cancelled.

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
- [x] Two channel-limit tables disagree, and one is wrong — `ca9f030`. Resolved by
  `backend/app/engines/channel/specs.py`: ONE table, keyed on the names the PRODUCT already
  stores (`link_service._CHANNEL_TAGS`, `renderings`, every short link), with the rubric's
  `facebook_post`/`instagram_caption`/`blog_article` as aliases so the twenty eval cases keep
  naming their channel exactly as they did. `nodes.CHANNEL_LIMITS` is derived from it and
  `evals/rubric.CHANNEL_LIMITS` is deleted. It changed one number honestly: Facebook's ceiling
  is its real 63,206 rather than the 2,000 that used to live in the node table, because 2,000
  is an editorial target and truncating good copy at a target is not enforcement — being over
  the target is REPORTED instead. The `channel_specs` config TABLE is not built and is not
  needed: nothing has asked for per-tenant channel limits, and the drift this item was about
  was between two code copies. Original text kept below.

  ~~Two channel-limit tables disagree, and one is wrong.~~ `agents/nodes.CHANNEL_LIMITS` is
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
- [x] **REPACK discards the hashtags it asked the model for** — `ca9f030`. `renderings` is now a
  mapping per channel (body, surviving hashtags, how many code removed, how many are still
  missing, whether the post is over its editorial target), and readers tolerate the old flat
  string because those rows are in the database. It also turned out `channel.validate` was
  granted to REPACK and implemented by NOTHING, so `enforce_hashtags` had never once run inside
  a run — the engine was correcting hashtags in the eval harness and not in the product. The
  review screen shows the counters rather than hiding them: three tidy hashtags shown without
  saying five were cut credits the model for the renderer's work. Original text: `REPACK_TOOL`
  accepts
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
- [x] **`docs/AGENT_RUNTIME.md` documents four tools that are granted but not wired** — all four
  wired in `a0c011a`.
  - `kb.search` in OPPORTUNITY, PLAN and GENERATE. Topics were being chosen from the crawl and
    the SERP alone, which is exactly the evidence a competitor also has. PLAN retrieves against
    the chosen opportunity rather than the run goal — retrieving against "more local leads"
    returns whatever is most on-topic in general.
  - `nap.audit` in HARVEST, which needed a SOURCE rather than code. New `engines/nap/extract.py`
    reads the business's own `LocalBusiness` JSON-LD and its Impressum, the two places a German
    business publishes its NAP and routinely disagrees with itself. **No directory scraping and
    no paid aggregator**, so the audit's scope is self-consistency and the payload SAYS so —
    "94" would otherwise be read as "your address is consistent online".
  - `geo.probe` in HARVEST, carrying `ShareOfVoice.headline` rather than a bare percentage: a
    rendering that physically cannot print a share without its denominator.
  - `web_search` in GENERATE, as a bounded tool loop in `_ask` (two rounds, results fenced as
    untrusted, the record tool offered FIRST because `FakeProvider` answers with `tools[0]`).
  Found while doing it: `box.offer` filtered on the allowlist alone, so an UNWIRED tool was
  still offered to the model — which contradicts §4's own rule that a tool the node will not get
  is removed so it never plans around it. `NodeToolbox.available()`'s meaning has correspondingly
  narrowed: "not wired" now means a deployment or tenant fact (no real search provider, no
  indexed documents) rather than a build state, and every caller turns it into a named
  `fact_gap`.
- [x] **Text extraction is NOT a security boundary, and is now measured rather than assumed** —
  DELIBERATE, closed as recorded rather than as fixed. There is nothing to implement: the point
  of the item is that the limit is MEASURED, and the tests assert the survivals as well as the
  drops precisely so this can never be upgraded into "hidden instructions never reach the
  model". The barrier that does the work is the per-node tool allowlist, not the extractor.
  `display:none`, `visibility:hidden` and HTML comments are dropped by trafilatura, but
  `font-size:0` and off-screen text survive into `main_text`, and instruction text in `img alt`
  survives into `facts`. Tests assert both the drops AND the survivals, so this cannot be upgraded
  into "hidden instructions never reach the model".
- [x] **Login CSRF is not closed** — DELIBERATE, and recorded rather than fixed. A pre-login
  request carries no cookie to check, so closing it properly needs a pre-session synchronizer
  token, which the Origin-validation design deliberately avoids. `SameSite=Lax`
  blocks the cross-site POST; closing it properly needs a pre-session synchronizer token, which the
  Origin-validation design deliberately avoids.
- [x] **A cookie-bearing request from a non-browser client is now refused** — INTENDED, and
  recorded so nobody re-discovers it as a bug. No `Origin`/`Referer` means no session:
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
- [x] `/api/v1/onboarding` has only `/preview` — **stale when written; closed by `310b735`**,
  which added `/confirm`, `save_confirmed_dna` and the `/onboard` screen's confirm step, with
  `tests/db/test_onboarding_confirm.py` against real Postgres. Ticked here rather than deleted
  because the consequence it named is worth keeping in view: until it landed,
  `tone`/`audience`/`banned_claims` are empty for every business (created at signup with `dna = {}`).
  Consequence: the regulated-claim guard has no claims to enforce for a real tenant.
- [x] `/auth/me` exposes no `businessId` — `417c5f8`. `null` rather than an error for an account
  with no business, because mid-signup is a legitimate state and it is the screen's job to say
  "finish onboarding". The lookup is factored out of `runs.current_business` and takes a session
  rather than opening one: two queries answering "whose business is this" is how a screen and an
  authorisation check start disagreeing. Original text: this is why the memory routes derive the
  tenant from the
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
- [x] **The ledger cannot be demonstrated end to end on the current credential** — closed by
  `4fca6e1`, though not the way this item expected. Wiring retrieval into a real run gave the
  ledger something to record on EVERY run: driving the app produced 26 rows across
  OPPORTUNITY, PLAN, GENERATE, CONVERT and the retrieval loop. At $0, because the run was
  deliberately made against the fake provider — which is the honest way to demonstrate the
  ledger's WIRING without claiming a cost measurement.

  It also found a real bug of exactly the kind this project has fixed before: 18 of those 26
  rows carried an EMPTY `node`, because the agentic retrieval loop makes three cheap calls per
  attempt and none of them passed trace context. `kb_service.retrieve` now threads `trace`
  through all three, labelled `KB` rather than a graph node's name — the calls belong to the
  loop, and borrowing HARVEST's name would put another node's spend in its column. Verified
  against the live ledger. The ⛔ part of this item is unchanged and unblocked only by the
  OpenRouter account setting: a LIVE run's mid-tier call is still refused, so real dollar
  amounts still cannot be shown here. Original text: a live run's only
  model call is at OPPORTUNITY, which routes to the MID tier and is refused by this OpenRouter
  account's data policy — and a failed call has no usage to record, correctly. So `model_usage` fills
  in the DB tests but stays empty on a real run here. Unblocked by the same account setting as the
  strong-tier eval.

## The owner journey, closed (2026-08-19)

- [x] **A normal user could do almost nothing from the UI.** Three breaks, all now fixed:
  the website URL was previewed and thrown away (nothing persisted it); nothing in the
  frontend called `POST /api/v1/runs`, so a run could only be started by curl and
  `/runs/[runId]` was unreachable; and `GET /api/v1/leads` had no screen at all, despite
  the docs calling attribution "how the customer BELIEVES the leads are real".
  Now: `POST /api/v1/onboarding/confirm` persists the confirmed DNA (merging, so it cannot
  wipe business memory, and writing `website` where HARVEST reads it), a dashboard starts
  runs, `GET /api/v1/runs` lists them, and `/leads` shows the attribution.
- [x] **Two text tokens failed WCAG 1.4.3 AA across every screen** — measured
  `--text-muted` 3.79:1 and `--text-faint` 2.28:1 against `--bg` in the light theme, and
  `--text-faint` 3.95:1 in dark. Fixed by walking lightness at the same hue until the
  WORST of bg/raised/sunken cleared 4.5 (the first candidates passed on the page and
  failed inside a recessed well, which is passing nowhere that matters). Now 4.75/4.74
  light and 6.78/4.89 dark.
  **The cost, recorded rather than hidden:** once both clear 4.5:1 on a light ground they
  land on nearly the same colour, so the muted/faint TIERS COLLAPSE. Three legible greys
  do not fit. Hierarchy below `--text` now has to come from size and weight; a genuinely
  distinct faint tier needs larger type (AA allows 3:1 at >=24px) or a darker surface.
- [x] **`--border` was referenced by several screens and defined NOWHERE**, so
  `borderColor: var(--border)` silently fell back to `currentColor` — a border that took
  the text colour. Aliased to `--edge`.
- [x] **`Pill` with `tone="accent"`** — `4fca6e1`. Fixed by FILLING the accent pill:
  `--accent-ink` on `--accent` measures 6.09:1 light and 7.14:1 dark, and it is the token that
  exists for exactly this — it keeps the brand orange instead of darkening a colour used all
  over the app. The other four tones pass as text on surface and are left alone; filling all
  five would repaint every screen to fix one. Original text: it renders `--accent` as text at
  ~2.54:1 on the surface,
  failing AA for its 11px label. Pre-existing (the run timeline already used it). No
  information is lost — the pill always shows text as well as colour — only legibility.
  Needs either `--accent-ink` on a filled pill or a darkened text-only variant.
- [x] **`POST /api/v1/runs/{id}/resume` still has no UI caller** — `4fca6e1`. A "resume if
  stalled" control on the runs list, for a `queued` or `running` run only (mirroring the
  endpoint's own refusals rather than guessing at them). The API's refusal is printed VERBATIM:
  a run that is genuinely executing looks identical from the list, only the executor can tell
  "a task is driving this" from "a process died and left it there", and hiding that behind a
  generic error would turn a working safeguard into a mystery. Original text: a run stranded
  `running`
  by a dead process is recoverable only by curl. The runs list is its natural home.
- [x] **No frontend test framework exists** — `729a020`. Decision taken: **Vitest + React
  Testing Library in jsdom**, 53 tests, wired into the `web` CI job as its own step. Aimed
  where a bug is invisible rather than at coverage — `partial` never rendering as `ok`,
  polling that provably STOPS, the pagination dedupe run as a real sequence, a refusal shown
  verbatim, and every empty review tab naming its own node. Each behaviour was
  mutation-tested (broken in the source, suite confirmed red, source restored). It found a
  contradiction in shipped copy: "Searchable — 0 passages the agent can quote".
  Deliberately still untested: `safe-html.tsx`, which is the XSS boundary and the
  highest-value target in the frontend — it needs its own suite (see below).
- [x] **`GET /api/v1/runs` has a cap, not pagination** — `417c5f8`. A keyset cursor on
  `(created_at, id)` — the tuple the query already orders by — compared as a row value, because
  `created_at < stamp` alone drops every run sharing that microsecond and `<=` repeats it.
  Offset was the wrong tool for a correctness reason rather than a speed one: a run started
  while somebody reads page one shifts every offset by a row. `nextCursor` rather than a total,
  base64url so a `+` in an ISO offset cannot decode as a space, and a malformed cursor is a 422
  rather than a silent first page. The poll re-reads only the first page. Original text: the
  page says "showing N (the most
  recent 50)" rather than claiming a total, so it is honest, but a business past 50 runs
  cannot reach the older ones.

## Found while closing the tail (2026-08-20)

- [x] **The knowledge base was built, tested, and UNREACHABLE** — `a0c011a` + `4fca6e1`. Not in
  this backlog before, and the largest gap in the product: `kb_service.ingest_document`
  extracts, chunks, embeds and stores a file, and it was called from twenty tests and nowhere
  else. No route, no UI, no `documents` row — so no business could ever hold a chunk, which made
  the pgvector store, the pdf/docx extractors and the whole agentic retrieval loop unreachable,
  and made `docs/FEATURES.md` §7's step 1 ("crawl site, ingest documents") half true.
  `POST/GET/DELETE /api/v1/documents`, a `PostgresDocumentStore` for the table nothing had ever
  written to, and a `/documents` screen. Verified end to end against real Postgres: upload →
  extract → chunk → embed → store → list, with the refusals (415 unreadable format, 413
  oversize, 422 empty, 503 missing parser) each exercised.
- [x] **`build_real_deps` passed `retrieve=None, load_memory=None`, so no real run had ever read
  a document or a remembered preference** — `a0c011a`. Two headline claims were true of the test
  suite and not of the product. Memory is wired unconditionally (our own data, so an empty
  result means "nothing remembered yet"); retrieval is wired only when the business has indexed
  something, which is the rule this function already applied to the search provider — a
  retriever over an empty store answers "nothing relevant", and that reads as a business whose
  own material had nothing to say. Verified on a live run: `fact_gaps` no longer names "uploaded
  documents" once a document exists.
- [x] **`langgraph` was a declared dependency that nothing imported** — `862e7e9`. Not in this
  backlog before either. `ARCHITECTURE.md` §14 called this a "LangGraph state machine", which
  was a claim about the SHAPE (bounded steps, a defined human interrupt, resumable, per-node
  evaluation) and true of the hand-written driver — but not a claim about the code, and a reader
  grepping for the import would not have found one. `agents/state_graph.py` compiles the
  identical machine with the library; `agent_runtime` selects it (default) or the builtin driver;
  and every test in `tests/agents/test_graph.py` now runs against BOTH, because a fallback that
  is not equivalent is not a fallback. The builtin driver should be deleted if it goes a release
  untouched.
- [x] **Ragas is used, out-of-process** — the criteria name it, so it is there. It cannot
  share a venv with this codebase: `ragas` depends on `instructor`, which caps
  `openai<3.0.0`, while this project pins `openai>=3.2.0` deliberately (v3 is built on
  httpx2, which is why `httpx2` is a declared dev dependency and why respx cannot
  intercept our provider calls). `ragas==0.3.1` escapes that pin and then fails at import
  on `langchain_community.chat_models.vertexai`, removed in langchain-community 1.x.

  So `evals/ragas_arm.py` drives `evals/ragas_runner.py` inside `.venv-ragas`
  (`make ragas-env`, pinned in `evals/ragas-requirements.txt`) as a subprocess with JSON
  in and JSON out, behind `--ragas`, off by default. Batched — Ragas evaluates a dataset,
  so one interpreter start covers every case rather than eighty. What the boundary costs
  is stated in the report itself: no per-call budget guard and no fallback chain. What it
  keeps: the judge's model id is resolved from OUR routing table (so a tier change still
  moves the judge and no model id is written at a call site), and the child reports token
  usage back so the spend is priced with our own table.

  Verified through the real subprocess against a local stub judge: 4 judge calls, usage
  tallied, faithfulness scored on the grounded arm. **That run also found a real problem:
  Ragas returns `faithfulness = 1.00` for a sample whose retrieval context is EMPTY** —
  there is nothing there to contradict, so every claim passes by default. That would put
  the report's best number on its least grounded output, so the score is discarded on the
  way back with the reason kept. `answer_relevancy` needs an embeddings endpoint (Ragas
  embeds generated questions against the original); without one it reads `n/m` rather
  than a number derived from nothing.

  Both judged arms now exist and both are opt-in. Running the two together is the
  interesting case: where they disagree on the same text, the gap is a measurement of the
  JUDGES, which is why the report renders them side by side instead of picking one.
- [ ] **A live Ragas measurement has not been obtained**, only a stubbed one. `--ragas
  --live` needs a real key and spends real money (several judge calls per case-arm), so
  it is ⛔ under the money guardrail. Everything up to the provider call is proven.
- [ ] **`answer_relevancy` is unmeasured on both arms until an embeddings endpoint is
  configured.** Ragas needs one; our `RouterEmbedder` reports `using_fake` because no
  embeddings provider is set. Set `RAGAS_EMBEDDINGS_MODEL` (and a key that serves
  embeddings) to fill that column. Reported as `n/m` meanwhile, never as a score.
- [ ] **`evals/report.md` still names Ragas in its header and its column titles.** The
  checked-in report is a real `--live` run (2026-08-19, `gpt-4.1-mini`, real money), and
  regenerating it hermetically would overwrite measured numbers with FakeProvider
  canned-string ones — destroying evidence to refresh a header. Needs one `--live
  --deepeval` run by somebody with a key, which is ⛔ by the money guardrail. Roughly 280
  mid-tier calls for the full 20 cases × 2 arms.
- [x] **`frontend/app/components/safe-html.tsx` has no tests, and it is the XSS boundary.**
  — `500850c`. 24 cases against real payloads (img/onerror, case-varied `<ScRiPt>`, the SVG
  vector, the `noscript` mutation payload, `javascript:`/`data:`/whitespace-smuggled hrefs,
  the no-DOMParser fallback). Mutation-tested rather than assumed: `dangerouslySetInnerHTML`
  turns 12 of 24 red, widening `SAFE_SCHEMES` turns 3 red. Two defects were in the TESTS,
  which is the part worth keeping: the entity case, written as a JSX attribute literal, was
  decoded by JSX before it reached the component — so it received a REAL script tag and the
  test silently duplicated the first case in the file while appearing to prove the opposite;
  and the no-escape-hatch source scan matched the component's own docstring explaining why it
  avoids `innerHTML`, a check satisfiable only by deleting the explanation. `vite-raw.d.ts`
  declares the `?raw` suffix that scan imports through, narrowly — a wildcard shim would type
  a typo as `any`.
- [x] **`statusTone(status)` remains status-only for callers that have no document.**
  `documentTone(document)` is what the screens use, and it is what lets a zero-passage
  `indexed` file avoid a green pill. The narrower function is kept because a caller with
  only a status string is a real case, but the two must not drift — if a third colour rule
  appears, fold them. — the note is now ENFORCED rather than recorded: a drift-guard block
  in `documents-api.test.ts` asserts the RELATIONSHIP (`documentTone` is `statusTone` plus
  exactly one documented exception) instead of each function in isolation, which is what
  the four existing per-function tests did — they would all still pass while the two
  diverged. It computes which statuses diverge and asserts that set is exactly
  `["indexed"]`, holds the count irrelevant on every other status, and pins both to the
  four tones `Pill` can render. Mutation-tested: adding a `failed`+zero-chunk rule to
  `documentTone` alone turns 3 of 19 red. Note for whoever folds them — `statusTone` has
  no caller outside `documentTone` and the test file today, so the "real case" it is kept
  for is still hypothetical
- [x] **The Docker `images` CI job is unverified against the new frontend test files.**
  → verified 2026-08-21 in CI (green on `main`); see the §B entry.
  `docker build -f frontend/Dockerfile` ran past 15 minutes locally and was killed. Local
  `pnpm build` exercises the same compile-and-typecheck path with the test files present
  and `--frozen-lockfile` is verified, so the risk is low — but low is not verified, and CI
  is where it will be found out.

## Publishing, built (2026-08-20)

The architecture named an Actuator layer, `docs/AGENT_RUNTIME.md` §3 tabulated EXPORT and
MEASURE, and none of it existed: `publish`/`notify` were string constants, `EXPORT` was in
the allowlist and not in `graph.ORDER`, `backend/app/actuators/` did not exist, and a
repo-wide grep for `oauth|access_token|graph.facebook|linkedin.com/v2` returned nothing.

- [x] **The actuator layer** — `backend/app/actuators/`. `actuate()` is the only entry
  point and owns idempotency, approval and audit, so a new integration cannot forget
  them. Claim-the-key-before-calling is the ordering that turns a crash mid-call into an
  `in_flight` row a human can chase instead of an invisible gap a retry turns into a
  double post. Never raises: its caller is a graph node, and a node that dies on a failed
  publish takes the rest of the run's output with it.
- [x] **EXPORT and MEASURE in `ORDER`, after REVIEW and unreachable without it** — in both
  runtimes, so "nothing publishes without a human" is a property of the machine. The
  subtle half: REVIEW's LangGraph router would have returned EXPORT via `_next_unvisited`,
  routing AROUND the edge `interrupt_before` is armed on.
- [x] **`POST /api/v1/runs/{id}/approve`** — the human decision, finally reachable.
  Nothing recorded WHO approved a run, so EXPORT's no-approver refusal fired on every run.
  The approver is the authenticated user and lands on every `actions` row.
- [x] **The email actuator** (`notify.email`) — the one channel with no App Review. Legal
  checks run BEFORE the fake/real branch, because a refusal that only fires once a key is
  set is a refusal first exercised on a real recipient.
- [x] **`platform_connections` + AES-256-GCM at rest** — business-scoped with RLS, secrets
  masked on every print path, bound to `business|platform` so a ciphertext moved between
  rows will not open.
- [x] **The Tier-3 export pack** (`GET /runs/{id}/export`, JSON + markdown) and a Delivery
  tab that cannot render a simulated send as a delivered one.
- [x] **Actuators wired into real runs.** Verified end to end: approve → EXPORT → MEASURE,
  with the ledger reading `publish.page succeeded simulated=true`, `social.post refused
  "no linkedin connection"`, and `notify_note: "no email address on record"` — three
  different reasons, none of them a silent skip.

### Open, and each one is honest about why

- [ ] ⛔ **Tier 1 direct publish is gated on per-platform App Review**, not on code: Meta
  (screencast, privacy policy, business verification; 2–6 weeks, refusable), LinkedIn
  Marketing Developer Platform, TikTok audit. No real `OAuthProvider` and no
  `SocialPublisher` exist deliberately — neither could be exercised by this suite or by
  hand, and untested code pretending to be a feature is worse than a stated gap.
- [ ] ⛔ **A real email send needs `RESEND_API_KEY`.** Every mapping is written against
  Resend's documented error envelope and exercised through `MockTransport`; what a key
  would prove is the actual status codes, that a 200 always carries `id`, and that
  `List-Unsubscribe` survives delivery.
- [x] **No connect/callback/disconnect API routes** for platform connections. The store,
  the cipher and the OAuth seam are done and tested; nothing exposes them, so a business
  cannot connect an account even to the fake provider. **→ superseded by A3 in the finish
  plan below; work that entry, not this line.**
- [x] **`nodes._notify_owner` builds a `notify.email` the email actuator refuses** — no
  sender, no body, no unsubscribe, no consent basis. Either that node supplies them or
  owner notifications get their own action type with transactional rules. Widening
  `CONSENT_BASES` to make it pass would throw away the point of the check. **→ superseded by
  A4 + A4b below, which settle the ruling and name two further defects.**
- [x] **`publish.page` is simulated even though the page is served by this app.**
  `publish_landing_page` exists with no caller, so "publishing" a landing page is a status
  change nobody makes. This is the cheapest real publish left and it needs no credential.
  **→ closed by A1a + A1b below. The audit found it is much more than "the cheapest
  publish left": it is the reason the lead chain is unreachable from a run at all.**
  **Overtaken a second time, 2026-08-21 (`69a18f9`):** the founder ruled we host no page, so
  `PAGE_PUBLISH_ACTION` is no longer in `EXPORT_ACTIONS` and a run publishes no page at all.
  The actuator and `GET /p/{piece_id}` stay for pieces already published — real rows exist —
  so this line is closed by both halves: it was built, and then it left the run.
- [ ] **MEASURE reports the attribution PATH, not lead counts.** Real counts need a
  lead-store read, which is outside its documented grants (`geo.probe`,
  `analytics.fetch`), so it states `leads_measured: false` with the reason. **→ superseded by
  A5 below, which grants the read and keeps the no-fabricated-zero rule.**
- [ ] **The weekly published-pieces-per-business cap** (`ARCHITECTURE.md` §8) is not
  implemented; it needs a cross-run ledger read the node cannot make hermetically today.
  **→ superseded by A6 below. That stated reason is STALE: `NodeDeps.actuator_store` is
  already the injected-store pattern this needs.**
- [x] ⛔ **The Docker `images` CI job is still unverified** against the new frontend test
  files — the local build ran past 15 minutes twice and was killed. **→ reclassified ⛔ in
  §B below: verifying it needs a push, which is outward-facing.**

---

# The finish plan (architect, 2026-08-20)

Written in answer to "finish the backlog". Read the three-bucket split below before
picking anything up: **the honest answer is that the backlog cannot be fully closed by
the loop**, and §C names the residue so nobody spends a sitting trying.

Audit basis: backend suite **2577 tests, green** (`pytest -q`, exit 0, real Postgres on
:5435); frontend **108 tests, green** (`pnpm test`, 8 files). Every `[x]` sampled below
was checked against the code, not against its commit message.

## A · LOOP-SAFE — ordered, topmost first

Dependencies are stated. Do not reorder A1 below A4/A5: both of those count things that
A1 is the first code to create.

### N · Found by the 2026-08-21 audit, after the autonomous-operation work landed

Twenty-seven commits landed on `feat/autonomous-operation-seo-audit` without this file
being touched, so these were found by reading the tree rather than by planning it. Audit
basis, re-measured 2026-08-21: backend **2988 tests green** (`pytest -q`, exit 0, real
Postgres on :5435), frontend **259 tests green** (17 files), `ruff` clean, `mypy --strict`
clean on 278 files, `tsc --noEmit` clean. Topmost first, and N1 is first because it is the
one an owner can see.

- [x] **N1 · Automation can be RUN but not TURNED ON — nothing writes `automation_settings`**
  — the exact inverse of the bug the scheduler commit (`e410d9c`) set out to fix, and the same
  shape: a capability that is complete on one side of the wire and unreachable from the other.
  The worker reads `automation_settings` (cadence, `channels`, `goal_template`, `next_run_at`),
  claims a slot conditionally, starts the run and advances the cadence — all tested. But
  `grep -rn automation backend/app/api/` is EMPTY, and so is the frontend: there is no route
  and no screen, so a row can only come into existence by hand in SQL. So the honest current
  claim is "the scheduler executes automations", NOT "a business can automate its marketing" —
  and `CRITERIA_MAP.md`'s autonomy row must not say the second while this is open.
  **done = an owner reads and writes their own automation from the UI (on/off, cadence,
  channels, goal), the row is tenant-scoped by RLS like every other write, `next_run_at` is
  computed by `automation_service.compute_next_run` and never by the route (one authority for
  the arithmetic, so the screen and the worker cannot disagree), turning it OFF stops the
  worker picking it up, and the screen states when the next run is due rather than implying
  it already happened.**
  **CLOSED, both halves.**
  - **Backend** — `GET`/`PUT /api/v1/automation` (`api/automation.py`) over
    `services/automation_settings_service.py`, business derived from the session, PUT a full
    replacement, `nextRunAt` computed on every save and read-only on the wire, off clears the
    slot as well as the mode, an explicit enable clears a system pause, and the channel rule is
    now literally the same function `POST /runs` validates against
    (`specs.canonicalise_known`). 20 tests on real SQL asserting through `due_automations()`
    itself + 19 hermetic route tests; verified by hand against the dev database.
  - **Screen** — `frontend/app/automation/page.tsx` + `lib/automation-api.ts`, in the sidebar
    under Work. The form's whole vocabulary (channels, cadences, goal length, poll interval) is
    read off the response rather than restated, so a picker cannot offer what the API refuses;
    `nextRunAt` is rendered and never recomputed from the cadence, which is the reason the API
    returns it at all; `pausedReason` is rendered verbatim, because the platform's own sentence
    (with the budget figures in it) beats any summary of it.
  - **One thing found while building the screen and worth keeping: `isOverdue`.** The worker
    advances `next_run_at` BEFORE starting a run, so a due timestamp still sitting in the past
    five poll intervals later means nothing is claiming it — in practice `make worker` is not
    running. So the panel says "overdue, and here is why" instead of confidently printing a next
    run that no process will honour. Grace derived from the server's own reported interval, and
    suppressed while paused (the pause is already the explanation). 20 unit tests on the derived
    answers, including that a stale timestamp on a PAUSED automation is not reported as overdue.
  - Honest note on verification: the route was driven end to end against the dev database with a
    real session, the pure client logic is unit-tested, and the page was loaded in a browser —
    where it renders and correctly shows the signed-out refusal. The **signed-in** rendering was
    not verified by me: doing so needs a password entered into the login form, which is not mine
    to type.

- [x] **N2 · A SCHEDULED run is not subject to the monthly USD cap** — found while verifying
  A7, and it is a money hole rather than a tidy-up. `_require_monthly_headroom` lives in
  `api/runs.py` and guards the two HTTP routes; the scheduler calls `RunService.start` +
  `submit(...)` directly (`worker/scheduler._start_run`), so an automation on a weekly cadence
  spends past the ceiling that a human pressing the same button is refused for. The cap being
  in the API module was correct when the API was the only way a run could begin; a second
  caller makes that placement the bug. Note this is the same lesson as `publish.page` going
  through `actuate()` and never around it: a guarantee belongs where every caller must pass,
  not where the first caller happened to be.
  **done = the ceiling is enforced in ONE place both callers reach (the executor or a service
  the executor calls), before any provider call; a scheduled run over the ceiling does not
  start and says why in the run's own record rather than only in a log; the API's 409 body is
  unchanged; a test starts a scheduled run for an over-cap business and asserts no provider
  call was made.**
  **CLOSED, with ONE deliberate deviation from that criterion, recorded rather than quietly
  taken.** `cost_service.monthly_cap_state` is now the single decision — the ledger read, the
  configured ceiling (read there, so two entry points cannot enforce two different numbers),
  and one `sentence` stating both figures. Every entry point asks it: `POST /runs` and
  `/runs/{id}/resume` refuse 409 with the body **byte-identical** to before (the sentence stops
  before the consequence and each caller appends its own), and the scheduler asks BEFORE
  claiming the slot. `approve` stays exempt for the reason already documented at its call site.
  - **The deviation: a scheduled refusal is recorded on the AUTOMATION, not "in the run's own
    record".** Writing it to a run means creating one — and `api/runs.py` already argues, at
    length, that a `partial` row with no events appears in the runs list, in the dashboard's
    run count and in the timeline as work it never did. So the refusal is written to
    `automation_settings.paused_reason`, which is the column whose model docstring already
    names "budget exhausted" as its example, and which the owner sees on their own screen. The
    original wording was written before that column was in view; this is the better answer and
    the criterion was wrong, not the code.
  - **The guard is not enforced by review.** `tests/test_run_start_guard.py` walks the AST of
    every module under `backend/app` and fails the build if one hands a run to the executor
    without referencing `monthly_cap_state` — module level, not per call site, because a
    per-function exemption list for `approve` would make the test a rubber stamp. It carries a
    second assertion pinning the set of run-starting modules, so it cannot start passing
    vacuously when `submit` is renamed or a route file is split. Verified by deleting the guard:
    three scheduler tests and this one go red, which is exactly the shipped bug reproduced.
  - Also: `BUSINESS_MONTHLY_CAP_USD=0` is documented as stopping all model spend for every
    business, and now actually does — it has to stop the spend nobody is watching as well as
    the spend somebody just clicked. `ARCHITECTURE.md` §7.4 rewritten to describe this, and to
    stop stating the weekly published-pieces cap as fact (A6 is still open).

- [ ] **N3 · The connection-expiry sweep has no caller** — see A3-ii, sharpened rather than
  duplicated: the function is written and tested, and the scheduler is now the obvious home.

- [x] **N5 · The `Web — types, tests, build` CI job had NEVER passed, and it failed in a way
  that reported failure without running anything** — found 2026-08-21 by pushing. Every run on
  `main` since 2026-08-20 is red, and the same three steps are red every time: `pnpm/action-setup@v4`
  fails in six seconds and `setup-node`, `pnpm install`, `typecheck`, `test` and `build` are all
  SKIPPED. So the frontend's type check, its 279 tests and its production build have never once
  been verified by CI — they were only ever green on a laptop. Cause: the job sets
  `defaults.run.working-directory: frontend`, which applies to `run` steps only; an ACTION does
  not see it, so the setup looked for `packageManager` in a root `package.json` this repo does
  not have (the web app lives in `frontend/` — see the README's "Why anything is at the repo
  root"). The workflow's own comment asserted it "reads `packageManager` from
  frontend/package.json", which is now true rather than merely intended: `with:
  package_json_file: frontend/package.json`. **Worth naming as a pattern, because this is the
  third instance in this file** — CI was green on work nobody ran (P1: `ENVIRONMENT: ci` with no
  `SESSION_SECRET` meant pytest never collected), and a 64-character constraint name had
  disabled `alembic check` entirely. A gate that cannot fail is worse than no gate, because it
  is *reported* as a gate.

- [x] **N4 · Three documents describe the frontend as it was yesterday** — a fresh drift, not a
  reopening of A8-i (which correctly closed for the tree that existed when it was written).
  `30a57b2` moved the owner's home to `/dashboard` and put a marketing front page at `/`, and
  `09cf3c3` / `c21ebfa` added `content/`, `business/` and the post calendar. So:
  `ARCHITECTURE.md` §10's route block still calls `page.tsx` "owner's home" and omits
  `dashboard/`, `business/` and `content/` — while asserting in its own prose that `ls
  frontend/app` turns up nothing that is not in it, which makes the omission a falsifiable
  claim rather than a stale line; `CRITERIA_MAP.md` §1's route list has the same three gaps;
  and `README.md`'s quickstart names only `make api` and `make web`, so a reader who follows
  it runs no scheduler and sees automation do nothing — the very failure `e410d9c` fixed.
  **done = §10's block, §1's list and the README's quickstart match `ls frontend/app` and the
  Makefile; the README says a third process exists and what breaks without it.**
  **CLOSED.** §10's block and §1's list now match `ls frontend/app` exactly (five screens were
  missing: `dashboard/`, `automation/`, `content/`, `business/`, and `page.tsx` was described as
  the owner's home); §10 gained a paragraph on why `/` is public and `/dashboard` is the home;
  §1's interactions row gained scheduling and the calendar; the README's quickstart names
  `make worker` as terminal 3 and says plainly what silently does not happen without it, and its
  journey now walks `/dashboard` → `/content` → `/automation`.
  - **And it is now a TEST, because this drifted twice in two days.**
    `backend/tests/test_docs_frontend_tree.py` parses §10's ASCII block and fails the build on
    any entry in `ls frontend/app` that is missing from it (or documented and absent), and
    checks that `CRITERIA_MAP.md` §1 names every screen — matched on the backticked path token,
    so the row stays free to write `developer/{models,runtime,tools,cost}` the way a person
    would. A third assertion pins the premise that `components/`, `lib/`, `globals.css` and
    `layout.tsx` are not screens, so the exemption cannot rot into a hidden hole if one of them
    ever gains a `page.tsx`. Verified by deleting a row from the block: red. It cannot check
    that a DESCRIPTION is accurate — no test can — only that the enumeration is complete.
  - **Three further claim defects found while doing it, all fixed here rather than filed:**
    (1) `asgi.py` documented itself as "the ONLY place that touches the environment" while
    `worker/__main__.py` also loads `.env` — both docstrings now state the rule as *every process
    entry point loads it, nothing below one does*, and the README's layout block agreed with the
    stale version. (2) The README's layout block had no `worker/` at all. (3) **Bigger, and the
    reason this entry grew: `ARCHITECTURE.md` §12 and `DIAGRAMS.md` §12 both draw a deployment
    that does not exist** — `worker-content` / `worker-harvest` pools pulling jobs from Redis.
    There is no job queue: a graph run executes in the API process and the scheduler scans
    `next_run_at`. Both are now labelled PLANNED with a "what ships today" note carrying the
    reason (ARQ is uninstallable against this project's `redis` pin, and the database is already
    an adequate queue for a weekly cadence). Left as annotation rather than a rewrite: the target
    topology is still the target, and `CRITERIA_MAP.md` §7 asks that a claim be true, not that an
    intention be deleted.

- [x] **A1a · A real `publish.page` actuator, so a run actually creates the landing page**
  — `c3a5dab`. `backend/app/actuators/landing.py`, wired in `run_executor`. `fake` is
  permanently False (this app serves the page — no credential to be missing), so one run
  now carries a real published page beside a simulated social post. Five tests on real SQL
  under RLS (published → servable → one retargeted link per CTA → second EXPORT replays →
  mixed real/simulated stay apart → unpublishable is refused writing nothing) plus THREE
  resolver tests, because the five inject the actuator and would all still pass if the
  wiring reverted to `FakeActuator` — verified by reverting it: the resolver tests fail,
  the actuator tests stay green.
  — `publish.page` is not "the cheapest real publish left", it is the missing link in the
  product's core chain, and the audit found the consequence is larger than the item says:
  **nothing in the application ever calls `content_store.create_landing_page`**, so no
  landing `content_pieces` row has ever existed outside `tests/db/test_content_store.py`,
  `GET /p/{piece_id}` can never serve anything, and **no per-channel tracked short link is
  ever minted in a real run** (`tests/api/test_runs_api.py::test_the_pack_never_invents_a_tracked_short_link`
  documents exactly this). So run → page → tracked link → click → lead → attribution — what
  `BUILD_ORDER.md` Phase 8 calls "the only screen that proves the product's actual promise"
  — is unreachable from a run today; a lead can only exist if a human mints a link by hand.
  Build a `LandingPageActuator` in `backend/app/actuators/landing.py` that performs
  `publish.page` by calling `services/landing_service.publish_landing_page(status="published")`,
  and register it in `run_executor._build_actuator_for` in place of the `FakeActuator`.
  **It belongs in the actuator layer, not in the node** — publishing to a surface this app
  serves is `ARCHITECTURE.md` §3's `publish_cms`, and the actuator layer is what buys the
  content-derived idempotency key; a node calling `publish_landing_page` directly would put
  a DB write in a node (`test_engine_boundary.py` is right to object) and a resumed EXPORT
  would create a duplicate content piece plus a duplicate set of short links every time.
  `LandingPageNotPublishableError` maps to `ActuationRefusedError`, not `ActuatorError`: a
  page that cannot capture a lead is a policy refusal, not a provider failure.
  **done = a test drives EXPORT against real Postgres and asserts (a) one `content_pieces`
  row with `status='published'` and its spec in `meta`, (b) `GET /p/{id}` returns 200 with
  the form, (c) one `short_links` row per channel CTA, each `?ref=<code>`-retargeted, and
  (d) running EXPORT twice creates exactly one piece and one link set — the second is
  `replayed`. `Outcome.fake` is False on this action while `social.post` stays True in the
  same run, so the Delivery tab tells the two apart.**

- [~] **A1a-i · SUPERSEDED 2026-08-21 (FOUNDER) — there is no published page to attribute.**
  `CONVERT` now picks a destination on the business's own site and `PAGE_PUBLISH_ACTION` has
  left `EXPORT_ACTIONS` (`69a18f9`), so no run creates a `content_pieces` row and the null
  join column this described cannot occur for a new run. **The underlying gap is real and
  narrower than this entry: `AgentState` still has no run-id key, so every `actions` row a run
  writes loses its link to the run.** That half is now carried by A5 (which wants the join) and
  by N2 below (which needs the executor to be the place a cap is enforced). Original text, kept
  because the `AgentState` analysis is still accurate: — found by
  A1a and deliberately NOT folded into it. `AgentState` has no run-id key at all, so
  `nodes._actuate` builds every `Actuation` without `run_id`, and therefore every page a run
  publishes lands with `content_pieces.run_id = NULL` and every `actions` row loses its link
  to the run. `LandingPageActuator` already forwards `actuation.run_id`, so it needs no
  change — what is missing is upstream. Closing it means adding the key to `AgentState`,
  populating it in the executor, and threading it through `_actuate`, which changes the
  checkpoint shape and must hold in BOTH drivers (a checkpoint written before the key exists
  must still resume). A5 (MEASURE lead counts) wants this: a lead count per run is not
  answerable while the join column is null.
  **done = a run's published page and its `actions` rows both carry its run id; a
  pre-migration checkpoint with no run id still resumes; asserted in both drivers.**
  `test_landing_actuator.py` currently asserts `run_id is None` WITH the reason — that
  assertion inverts when this lands, and it is the test that will notice.

- [x] **A1b · The published page and its real short links reach the export pack and the
  Delivery tab** — depends on A1a. `review_service` currently hard-codes
  `_NO_LANDING_PAGE`/`trackedLinkNote` and `test_the_pack_never_invents_a_tracked_short_link`
  forbids `/l/` anywhere in the pack. Both were correct while nothing minted a link and
  both are now wrong. Revisit them honestly rather than deleting them: the prohibition
  becomes "no short link may appear unless a `short_links` row backs it".
  **done = the pack carries the real `/p/{id}` URL and one real `/l/{code}` per channel;
  the two notes appear only when the run genuinely published neither; the amended test
  asserts a fabricated code still fails.**

- [x] **A1c · `/developer/runtime` says "Graph nodes (all eight)" and the graph has ELEVEN**
  — the label is DERIVED now: `graph_node_count()` reads `graph.ORDER` through the same lazy
  `importlib` path the version constants use, because a module-level `import graph` here would
  pull nodes/tools/engines into every HTTP process — which is what this module was written to
  avoid. `_DECLARED` carries an EMPTY label for that row, so there is no literal left to drift.
  `graphNodeCount` (11) and `taskClassCount` (8) ship as two separate API fields and render as
  two labelled figures, because a single count is what let a reader believe they were the same
  concept. Four tests, and the load-bearing one extends `ORDER` under `mock.patch` and asserts
  the displayed count FOLLOWS — asserting against `len(ORDER)` once would pass on a second
  hardcoded literal that happened to be right the day it was written. An unreadable graph
  renders a bare "Graph nodes" rather than any number. Verified live against a real admin
  session. The eight TaskClass cards were left untouched, per the founder's ruling.
  **Found while verifying, and fixed with it:** `PromptSurface` had no alias generator, so
  `how_to_change` shipped snake_case while the screen read `surface.howToChange` — the one
  sentence telling an operator how to change a prompt version rendered as `undefined`, which
  React shows as nothing. An outer model's `response_model_by_alias` does not reach a nested
  model.
  — found by the founder reviewing the screen, and it is a claims-discipline defect of exactly
  the kind this project keeps finding: a hardcoded count that drifts from the code it describes.
  `services/prompt_inventory.py:69` hardcodes the label `"Graph nodes (all eight)"` and its
  module docstring repeats "ONE value shared by all eight graph nodes", while
  `graph.ORDER` now holds **eleven** (`INTAKE HARVEST OPPORTUNITY PLAN GENERATE CONVERT VALIDATE
  REPACK REVIEW EXPORT MEASURE` — EXPORT and MEASURE landed with the publishing epic and nothing
  updated the count). It is wrong in the other direction too: only **five** call sites use `_ask`,
  the single helper that stamps `PROMPT_VERSION`, so the prompt does not cover eight nodes either.
  Two nodes make no model call at all by design (EXPORT actuates, MEASURE probes) and VALIDATE is
  deterministic scoring per `CLAUDE.md`'s compute-it rule.
  **Do NOT fix this by editing the number.** A second hardcoded count drifts the same way on the
  next node. Derive the label from `graph.ORDER`, and state what the constant actually covers
  ("every node that calls the model") rather than a count of nodes it does not.
  **done = the label is derived, not written; adding a node to `ORDER` changes the screen with no
  other edit; a test asserts the rendered label against `len(graph.ORDER)` so the two cannot
  diverge again.**
  Recorded and NOT a bug: the "Sampling per task class" section lists the eight `TaskClass`
  values (`CLASSIFY EXTRACT REPACK PLAN PRIORITISE GENERATE REVIEW EMBED`), which are
  model-routing classes and NOT graph nodes — `llm/contract.py:40` says so outright ("Named after
  the work, not after a node, so two nodes doing the same kind of work share a route"). That
  section is correctly titled. The confusion is caused by the neighbouring stale caption, which is
  the more reason to fix it.

- [x] **A2a · Carry `RetrievalTrace` into `AgentState` so the agentic-RAG evidence leaves
  the process** — not previously in this backlog and it is the highest-stakes gap for
  grading. `nodes._retrieved()` calls `box.call(KB_SEARCH, question)` and immediately
  reduces the result to `_passages(trace)`, so the query rewrites, the per-chunk relevance
  grades and the fallback decision are **discarded at the call site**. No `AgentState` key
  holds them and no route returns them. That leaves `BUILD_ORDER.md` Phase 3's "Visible: a
  retrieval trace panel … **this panel IS the Hard #1 evidence**" undelivered, and makes
  `CRITERIA_MAP.md` §8 step 5's own mitigation — "show it from the API response" —
  unsupported, because no API response contains it. Add a bounded `retrieval_traces` key
  (cap the count and drop chunk TEXT, keeping ids, grades and the decision: the checkpoint
  is rewritten on every node, which is why `summarise_crawl` exists).
  **done = a test asserts a run's checkpoint holds, for at least one node, the rewritten
  queries, a grade per chunk id, and the fallback decision; and asserts the stored trace
  carries no chunk body text.**

- [x] **A2b · A retrieval-trace panel on the review screen** — depends on A2a. Query →
  chunks → grades → decision, per node, with an honest empty state for a business with no
  documents (which is a normal state, not a failure).
  **done = a Vitest case renders a trace and asserts the fallback decision is shown in
  words; a second asserts the no-documents state names the node and does not imply
  retrieval failed. Then delete "no UI yet" from `CRITERIA_MAP.md` §8 step 5 — and not
  before.**

- [x] **A3 · Connect / callback / disconnect routes for `platform_connections`** — genuinely
  loop-safe, no credential: `get_oauth_provider()` returns `FakeOAuthProvider` for every
  platform, `PLATFORM_CREDENTIAL_KEY=ephemeral` is the documented local cipher mode
  (`.env.example:49`), and `connection_service.begin_connect/complete_connect/
  refresh_connection/revoke_connection` are all implemented and tested. Only the HTTP layer
  is missing. **Two rulings so this is not improvised.** (1) `begin_connect` returns a
  `state` whose docstring says verification "has to be held wherever the browser's session
  is" — hold it in a signed, short-TTL, `__Host-`-prefixed, `SameSite=Lax` cookie over the
  existing `core/security.py` HMAC, **not** a DB row (a table that needs sweeping for a
  one-shot nonce) and **not** Redis (not a hard dependency of the API today). (2) The
  callback is a redirect-borne GET carrying no `Origin`, so `core/csrf.py` must not be
  touched to accommodate it — **the state-cookie comparison IS the CSRF control on that
  route**, which is the standard OAuth design. `oauth_status()` must be surfaced verbatim,
  so the screen says publishing is waiting on somebody else's approval queue.
  **done = tests assert a full connect → callback → view → disconnect cycle against
  `FakeOAuthProvider` with no network; a callback whose `state` does not match the cookie
  is refused; the response never contains a token or ciphertext, only the mask; and
  business B cannot see or revoke business A's connection (RLS).**

- [x] **A4 · `notify.owner`: a transactional action type, not a widened `CONSENT_BASES`** —
  depends on nothing; ruling settled here. The item asks whether the node supplies the
  missing fields or owner notifications get their own action type. **It is the second**, and
  the reasons are not stylistic: (a) `actuators/email.py`'s own comment says a transactional
  message "is a different action type with different rules, not this one with a flag";
  (b) an owner service notification must not carry a marketing unsubscribe link —
  unsubscribing from "your run published 3 of 4" breaks the product — and `existing_customer`
  is a soft-opt-in *marketing* basis, so borrowing it would be the same widening in
  disguise. **Two defects the item does not name, both found in this audit and both in
  scope.** (i) `_notify_owner` passes `target=address`, which the email actuator refuses on
  purpose (`_check_target_is_a_handle`) because `actuate()` renders `target` — and that
  address then flows `Outcome.target` → `_outcome_row` → `runs.checkpoint` → the Delivery
  tab. The new type must derive its target through a `recipient_target`-style handle and a
  `build_*_actuation` helper, exactly as `build_email_actuation` does. (ii) The recipient is
  read from `state["dna"]["email"]`, which is a contact address extracted from a crawled
  homepage; our own transactional mail must go to the **authenticated account** address.
  Resolve it in `run_executor` and inject it on `NodeDeps` (the `actuator_store` pattern), so
  the node still touches no database.
  **done = `notify.owner` is refused with a named reason when the sender identity or body is
  missing, and SUCCEEDS on a well-formed owner notification driven through the REAL payload
  parser; a test asserts the actuation's `target` contains no `@`; and the recipient comes
  from the account, not from `dna`. Plus the claims fix below, in the same sitting.**

- [x] **A4b · The test double for `notify.email` is wider than the actuator, and asserts a
  green path the product does not have** — `tests/agents/test_export.py::test_the_owner_is_told_what_went_live_and_what_did_not`
  asserts `notified is True` using a generic `Publisher(NOTIFY_ACTION)`. Against the real
  actuator that same payload is refused (no sender, no body, no unsubscribe, no consent
  basis, and a bare address in `target`). Same class as "the engine was correcting hashtags
  in the eval harness and not in the product".
  **done = the EXPORT notify tests route their actuation through the real
  `parse_email_payload` / the real owner-notify parser, so a double can never again be more
  permissive than the actuator it stands in for.**

- [ ] **A5 · MEASURE gets a first-party `leads.count` grant, and still refuses to print a
  zero** — depends on A1a (before it there are no tracked links to count against). The
  tool-grant question resolves **in favour of granting**: MEASURE makes no model call
  (verified — no `_ask`, no router use in the node), so a first-party read widens no
  injection surface; the data is the tenant's own, and `memory.load` at INTAKE is the
  precedent for granting a node a first-party read; and `AGENT_RUNTIME.md` §3 already lists
  MEASURE's *Emits* as "metrics, lead attribution", so this fits the documented design
  rather than expanding it. **`analytics.fetch` stays unwired and stays named** — that grant
  is the GSC/GA4 cut, and quietly reusing it for a lead read would make the named gap
  disappear; our own leads and Google's analytics are different claims. **The honesty
  constraint is the task, not a caveat:** `LEADS_NOT_YET_NOTE`'s reasoning is correct, so
  report counts only with the window stated and keep an explicit `too_early` state for a
  piece published moments ago — never a bare `leads: 0`, and never a conversion rate on a
  denominator below a stated minimum.
  **done = with clicks and leads present, MEASURE reports both with the window named; with
  a piece published seconds ago it reports `too_early` and the reason, not a zero; a test
  asserts no rate is emitted below the minimum denominator; the §3 doc table and
  `NODE_TOOLS` are updated together (`test_tool_allowlist.py` parses the doc, so a
  one-sided change fails the build).**

- [x] **A6 · The weekly published-pieces-per-business cap** (`ARCHITECTURE.md` §7.4) —
  depends on A1a. **The recorded reason for it being blocked is stale:** `NodeDeps` already
  carries `actuator_store`, which is the same injected-store pattern, and the `actions`
  table already holds precisely what needs counting (`status='succeeded'`,
  `action_type IN PUBLISHABLE_ACTIONS`, `created_at >= now() - 7 days`). An in-memory
  counter makes it hermetic. §7.4 calls it "a quality control, not a cost control", so it is
  enforced **in EXPORT before actuating**, as a refusal naming the count and the window —
  not as a model-call guard and never as a silent skip.
  **done = a business at the cap gets a refusal that states the count, the window and the
  cap; a business under it publishes; the count is per business (a test proves A's
  publishes do not consume B's allowance); and a `refused` row lands in `actions`.**
  **CLOSED, with ONE deliberate deviation from that criterion — the last clause is wrong and
  the code is right.** `services/publish_cap.py` owns the decision: a rolling seven-day count
  of `actions` rows in `PUBLISHABLE_ACTIONS`, against `BUSINESS_WEEKLY_PUBLISH_CAP`
  (default 10 — six channels × one weekly run, plus headroom for manual publishes, and short
  of two full runs in a week; `0` is the kill switch, like the USD cap).
  - **Why no `actions` row: recording the refusal there would poison the idempotency key.**
    `actions.idempotency_key` is uniquely indexed and `claim` returns the outcome a key
    already holds, so a stored cap refusal would replay forever — the same post could never
    be published in ANY later week. The cap says *not this week*; a mechanism that turned
    that into *not ever* would be the opposite of a quality control. `actions` is the ledger
    of ATTEMPTED side effects and a capped publish attempts nothing, so the check sits
    BEFORE the claim and the refusal is reported where the project already reports what
    happened to finished content: a `weekly_cap_reached` NodeError on the run (which the
    review screen renders) and the `error` on the `PublishResult` the button gets back.
    Note this is the reverse of `actuate()`'s approval check, which is deliberately AFTER
    the claim precisely so it leaves a row — the asymmetry is the point, and both docstrings
    now say so.
  - **PARTIAL rather than all-or-nothing**, following this node's own rule instead of
    inventing one: per-destination failure is already per destination and "a run that
    published three of four says which one it did not". Room for one and two approved
    publishes the first (the order REVIEW showed) and names the other. Refusing both because
    there was room for one would discard work an owner approved.
  - **BOTH publish paths ask, which is the N2 lesson applied before it bit.** EXPORT and the
    calendar's publish button are the two ways a piece reaches a platform; a cap on one is
    advisory. `tests/test_run_start_guard.py` grew a second structural assertion — any module
    calling `actuate()` must name `weekly_publish_state`, with the known callers pinned — plus
    a third asserting `run_executor` wires `published_this_week`, since `None` means "not
    enforced" and one deleted keyword argument would otherwise disable the cap in production
    while every node test stayed green.
  - Verified by deleting each half: five EXPORT tests and two calendar tests go red. 9 more on
    real SQL pin what counts (`succeeded` and `in_flight` yes — the conservative direction;
    `refused`/`failed` no, or a rejected post would consume its replacement's allowance;
    another action type no; outside the rolling window no; and A's publishes do not consume
    B's, through RLS as the restricted role). 16 pure tests pin the arithmetic, including that
    a cap LOWERED below what is already published clamps to zero rather than slicing
    `pieces[:-4]`, which would publish everything except the last four.
  - `ARCHITECTURE.md` §7.4 rewritten: the "not implemented yet" marker is gone and the
    ordering rule above is recorded.

- [x] **A7 · The per-business monthly USD cap** — DONE, verified 2026-08-21 against the code
  rather than a commit message. `core/config.py:DEFAULT_BUSINESS_MONTHLY_CAP_USD` ($25, and
  `BUSINESS_MONTHLY_CAP_USD=0` is the kill switch), `cost_service.monthly_spend_usd` +
  `over_monthly_cap`, enforced by `api/runs._require_monthly_headroom` on BOTH the start and
  the resume route, before anything that could reach a provider, refusing 409
  `monthly_cap_exceeded` with the spend and the ceiling both stated as strings (a `Decimal` in
  an exception `detail` would leave as a JSON float, in the one path that exists to talk about
  money accurately). Tested in `tests/api/test_runs_api.py`.
  **Residual, and it is a MONEY hole rather than a tidy-up: the guard lives in the API module,
  so the scheduler — which starts runs through `RunService.start` directly — does not run it.**
  See N2 below. Original text: — not previously listed. `ARCHITECTURE.md`
  §7.4 states three cap levels as fact; only the **per-run** one exists (`llm/contract.py:BudgetState`
  is documented "remaining spend for a run"). `services/cost_service.cost_report(business_id,
  window_days=...)` already computes windowed spend, so the read exists.
  **done = a business over its monthly ceiling has its next run refused before any provider
  call, with the spend and the ceiling stated; a business under it runs; and the check is
  proven to run BEFORE the call, not after (the same ordering rule as the run budget).**

- [x] **A8 · Two documentation claims the code does not support** — cheap, and claims
  discipline is binding on docs per `CRITERIA_MAP.md` §7. (1) §1's Documentation row claims
  "6 documents + **in-app help assistant**". No assistant exists: no route, no screen, no
  service, no help retrieval path — and unlike step 5's retrieval trace, this claim is not
  marked aspirational. Correct the row; E5 is a bonus task and `BUILD_ORDER.md`'s own ledger
  reaches 4 easy / 8 medium / 4 hard without it, so building an assistant is a separate,
  larger decision for the human, not a doc fix. (2) `ARCHITECTURE.md` §9 says "developer
  mode is a server-rendered gate, not a hidden route", while `frontend/app/developer/layout.tsx`
  says in its own comment "No role check here. The gate is server-side on the API." The
  **data is genuinely protected** — every `/api/v1/admin/*` route carries `require_admin` —
  so this is a wording defect, not a hole: say "the data is gated server-side on every admin
  route; the shell renders and shows the refusal".
  **done = both statements match the code, and a grader reading either document is not told
  about a screen that does not exist.**

- [x] **A9 · `/developer/cost` tells an owner two contradictory things, and the route's gate
  is a category error** — found by live testing, and the ruling covers both halves so the
  loop does not have to choose. Verified: `backend/app/api/cost.py:63` gates `GET /cost` with
  `require_admin` (**platform_admin** — `api/admin_models.py:165`), while the same route
  takes `current_business`, reads `model_usage` under RLS **as one tenant**, and echoes
  `business_id` deliberately "because a spend figure with no statement of whose spend it is
  invites being read as a platform total". So a PLATFORM role guards a structurally
  PER-TENANT read — which serves neither audience: the owner who would want their own
  numbers is refused, and the platform operator who is admitted gets their own single
  tenant's numbers rather than a platform total.

  **Ruling on access (the product half): keep `require_admin`. Do NOT widen the gate, and do
  NOT move the screen in this task.** `BUILD_ORDER.md` Phase 9 places the cost dashboard in
  **developer mode**, "role-gated server-side", and lists user mode as dashboard / documents
  / opportunities / timeline / review / leads / memory — no cost. So the gate matches the
  documented user-vs-developer split, and loosening an authorization check to make a
  sentence true is the wrong direction of fix. The two things that ARE missing are recorded
  in §D rather than built here, because each is new scope and one of them is a product call
  the human owns: a **platform-wide** total needs a `SECURITY DEFINER` aggregate (the page's
  own docstring already names this as the mechanism — "not a looser session"), and an
  **owner-facing** per-business spend view is a product decision about whether an owner sees
  cost at all.

  **Ruling on copy (the loop-safe half): yes, the refusal reason becomes per-page, and the
  duplicate goes.** The generic string is not merely vague on Cost, it is FALSE — there is no
  setting to change and the page has just told the reader it is their own tenant's data.
  Three edits: (1) `frontend/app/developer/shell.tsx:105-118` takes the `forbidden` reason as
  a prop so each page supplies a true one (routing / sampling / tool access keep
  "platform-wide settings"; Cost says the console is operator-only and points the owner at
  the run timeline's live cost, which they CAN see); (2) delete the second hardcoded copy in
  `frontend/app/developer/models/page.tsx:119-130` — two copies of one string is the same
  drift this repo already fixed once for channel limits; (3) correct the Cost `PageHeader`,
  which currently describes a per-tenant owner view on an operator-only screen — it is the
  operator's own tenant's ledger, and it should say so rather than implying any signed-in
  business can read it here.
  **done = a Vitest case renders the Cost page's `forbidden` state and asserts the words
  "platform-wide settings" do NOT appear, while a routing/sampling/tool-access page's
  `forbidden` state asserts they DO; a test asserts the refusal string exists in exactly one
  place in the frontend; and the Cost header no longer promises a view the gate refuses.
  `CRITERIA_MAP.md` §7 claims discipline is binding on UI copy, so this is the same class of
  fix as A8.**

- [ ] **A9-i · The API's own 403 sentence is wrong for `/cost`, and only the frontend hides it**
  — found while fixing A9. `admin_models.require_admin` answers every gated route with
  `"Your account cannot change these settings."` It is route-agnostic because the dependency
  cannot know which screen asked, and it is false on `/api/v1/cost`: there is no setting on
  that route, the caller was refused a NUMBER. A9 stopped the console from RENDERING it (the
  screen supplies its own sentence for a 403), but the string is still what the API emits, so
  any other client, the OpenAPI schema, and anyone reading the response body still get the
  false noun. Two candidate fixes, and the second is probably right: a per-route refusal
  message, or drop the prose from the 403 body altogether since `code` is what callers act on
  and the prose is the screen's job to get right — which is the principle A9 established.
  **done = no gated route asserts what KIND of thing it is refusing unless it actually knows;
  a test pins the cost route's 403 body against the claim it makes.**

- [x] **A3-i · `revoke_connection` can make disconnecting IMPOSSIBLE** — found by A3 and worked
  around at the route rather than fixed at the source, deliberately, because the service is
  not the route's file and is separately tested. `connection_service.revoke_connection` calls
  `reveal_access` first, which raises `CredentialUnreadableError` on an envelope that will not
  open — a rotated key, or the ephemeral vault after ANY restart, which is the documented local
  configuration. Unhandled that is a 500, and a customer could never disconnect an account.
  `DELETE /connections/{platform}` now catches `TokenCipherError` and reaches the honest end
  state (`set_status(REVOKED, forget_credential=True)`), with a test that simulates a restart.
  **done = the SERVICE decides what an unreadable credential means for revocation, so every
  caller gets it right rather than each one catching separately; the route's workaround folds
  into it.**
- [ ] **A3-ii · Nothing writes `expired` to a platform connection** — NARROWED 2026-08-21: the
  sweep asked for here has since been WRITTEN. `connection_service.sweep_expired_connections`
  exists with tests on real SQL (`tests/db/test_connection_sweep.py`,
  `tests/services/test_connection_sweep.py`) — and has **no caller anywhere in `backend/app`**,
  so it has never run. What is left is wiring, not design, and the place is now obvious: the
  scheduler tick, beside `_sweep_stranded`, in the same one-tenant's-failure-costs-that-tenant
  posture. **done = the worker sweeps stale connections every tick; a failure in the sweep
  cannot stop the automations half of the same tick; a test asserts a stale connection is
  `expired` after one tick.** Original text: `refresh_connection` and
  `mark_expired_if_stale` exist and are unexposed. No SCREEN is wrong (usability is derived by
  the pure `unusable_reason` on every read, which is why the settings view and the publish
  refusal cannot disagree), but a SQL-level report would disagree with the API. This wants a
  sweep, not a route — same shape as the payment-style reconcilers.
- [x] **A3-iii · `backend/tests/conftest.py` does not strip `PLATFORM_CREDENTIAL_KEY`** the way
  it strips the six outbound credentials. Not a network risk, but a developer with a real AES
  key set gets `AesGcmCipher` where CI gets `NotConfiguredCipher` — the class of divergence that
  ends in "works in CI, fails on the box", which this repo has already been bitten by once.
  A3's own tests inject the cipher explicitly and are immune; other suites may not be.
- [x] **A3-iv · No frontend screen for platform connections** — the four routes exist and are
  tested; nothing renders them, so a business still cannot connect an account from the UI.
  Deliberately out of A3's scope (routes + tests only).
  **done = a settings screen lists connections with their derived usability, starts a connect,
  and disconnects; the secret is never rendered beyond its mask.**

- [x] **A8-i · Two documents describe a frontend route tree that does not exist** — CLOSED for
  the tree as it stood on 2026-08-20: `CRITERIA_MAP.md` §1 and `ARCHITECTURE.md` §10 both now
  describe the flat tree and §10 says out loud that `ls frontend/app` should turn up nothing
  that is not in its block. **Re-staled the next day by the autonomous-operation work — see N4
  below, which is a fresh drift and not a reopening of this one.** Original text: — found by A8
  and correctly left alone as a third, separate claim rather than folded in. `CRITERIA_MAP.md`
  §1's "User interactions" row points at `frontend/app/(app)/`, and `ARCHITECTURE.md` §10's
  route tree describes `(marketing)/`, `(auth)/`,
  `(app)/businesses/[id]/{documents,opportunities,content/[pieceId],settings}` and `f/[formId]/`.
  **None of those exist.** Verified 2026-08-20: `frontend/app` is FLAT — `connections`,
  `developer`, `documents`, `leads`, `login`, `memory`, `onboard`, `runs/[runId]`, plus a root
  `page.tsx`. There is no route group at all and no public lead-form route under `f/`. Bigger
  than a row edit because §10's "Guard placement" paragraph reasons FROM that tree, so the
  paragraph has to be re-derived from the real one, not just relabelled — and the real lead form
  is a public POST endpoint rather than a page, which is the interesting part to state correctly.
  **done = both documents describe the tree that exists; §10's guard reasoning follows from it;
  a grader diffing either against `ls frontend/app` finds no invented route.**

- [ ] **A3-v · `provider.revoke` is guarded only for `OAuthError`** — found by A3-i and correctly
  left alone. A future real provider raising anything else (a bare transport error) would 500 a
  disconnect through the same door A3-i just closed. Deliberately NOT widened to `except
  Exception`: that hides genuine defects, and `platform_oauth.OAuthProvider` documents
  `OAuthError` as the contract today. Worth doing only when a real provider client is written —
  which is itself gated on App Review, so this waits on §B.

- [x] **A3-vi · A connect flow cannot COMPLETE, and the screen is the thing that proves it**
  — OBSOLETE 2026-08-21, and by the first of the two exits it named rather than by the
  test-only bypass it refused to choose: two real adapters now exist behind the seam —
  `platform_oauth_meta` for `facebook`/`instagram` (`1bcc2ab`) and `platform_oauth_linkedin`
  for `linkedin` (`604fe81`). `get_oauth_provider` selects by CREDENTIAL and by nothing else,
  so a machine with no app configured still gets `FakeOAuthProvider` and still says so, while a
  machine with `META_APP_ID`/`META_APP_SECRET` (or the LinkedIn pair) reaches a real
  authorization URL a browser can complete. **Note what this does NOT unblock: publishing.**
  Tier 1 direct publish is still gated on App Review in §B, so a connected account can be
  connected and still not be posted to. Original text:
  — surfaced by A3-iv, which is why it is filed rather than hidden. `get_oauth_provider` returns
  `FakeOAuthProvider` for every platform and its authorization URL is
  `https://fake-oauth.invalid/…` — RFC 2606 reserved, so no browser can ever reach the callback
  and **no platform connection can be created from the UI at all**. The screen states that
  instead of rendering a link into a browser error, which is correct behaviour and not a
  workaround. The only two ways out are (a) a real provider adapter, which is ⛔ on App Review,
  or (b) a development shortcut that calls the callback directly — and (b) means forging the
  signed-state cookie flow the CSRF control exists for, so it needs a decision about whether a
  test-only bypass may exist in a path that guards a real security property. **That is a founder
  call, not the loop's.** Note this makes the four A3 routes provably correct and, today,
  un-completable end to end by a human — which is worth saying out loud rather than leaving a
  reader to discover by clicking.

- [x] **A10 · THE HUMAN DECISION HAS NO BUTTON — a run can be reviewed and never published**
  — CLOSED by A10a (approve control), A10b (the ruling) and A10c (reject control + the
  `rejected` terminal state): both defects this entry named are fixed, and a reviewer at the
  gate can now say yes or no from the screen. Its follow-ons keep their own boxes and are NOT
  covered by this tick: A10d and A10d-i are superseded (there is no draft page to persist),
  while A10d-ii and A10e remain genuinely open. Original text:
  (founder-reported 2026-08-20, verified same day; this is ahead of A4–A7 in value because it is
  the step the whole product is built around). The founder described the intended flow — onboard →
  analyse → generate → "bring to publish dashboard so the user can read and publish or reject" —
  and everything in it works EXCEPT the last step:
  - `POST /api/v1/runs/{id}/approve` **exists, is tested, and NOTHING CALLS IT.** Zero occurrences
    of `approve` in `frontend/app/runs/[runId]/review.tsx` or `page.tsx`. The run page offers
    **Resume**, which deliberately refuses a parked run ("waiting for a human decision, not
    stalled") — so a reviewer's only available button is the one that cannot help, and EXPORT's
    no-approver refusal is what they get if they find a way through. This is the same shape as the
    bugs this project keeps finding: the capability is built, tested, and unreachable.
  - **There is no reject route at all.** `grep reject backend/app/api/runs.py` is empty. Rejection
    exists only at the CONTENT-PIECE level in `feedback_service` (`VERDICTS = ("approved",
    "rejected")`, `reject_reason`, distil at 3+ occurrences) and `POST /content/{id}/feedback` has
    no UI caller either. `DIAGRAMS.md` §4 documents the intent — `REVIEW --> [*]: rejected, reason
    feeds the feedback loop` — so the behaviour is specified; the code is not.
  **Split, because the two halves have different risk:**
- [x] **A10a · The approve control on the review screen** — pure UI, no schema, no decision: the
  route exists and its contract is settled (202, approver is the AUTHENTICATED user and never
  client-supplied, 409 `run_not_awaiting_approval` from any other state, 409 `no_checkpoint`, and
  deliberately NOT idempotent — a second approval is a 409, not a silent no-op).
  **done = a reviewer at the gate can approve from the screen; the button appears ONLY in
  `awaiting_approval`; both 409s render their own sentence rather than a generic failure; a test
  asserts the client never sends an approver.**
- [x] **A10b · Reject — RULED 2026-08-20 (architect); no longer a decision. A new terminal
  `rejected` state, NO `feedback_service` write, and a reason is REQUIRED.** Backend only; the
  control is A10c. The three questions, answered from what the code actually permits:
  - **(1) A new `rejected` state, NOT `partial`.** Reusing `partial` makes a human's "no"
    indistinguishable in SQL from a node that fell short, so "how often do reviewers refuse the
    output" — the single number that says whether this product is any good — stops being askable,
    and the run is painted `warn` as a shortfall the machine is to blame for. That is the exact
    misattribution `partial`'s own colour rule exists to avoid, pointed the other way.
    `DIAGRAMS.md` §4 already draws rejection as its own exit. It is **terminal and NOT
    reversible**: the recovery from a rejection is a NEW run, which re-derives from current
    documents rather than republishing what was refused. Approve-after-reject is therefore the
    existing 409 `run_not_awaiting_approval` and needs no new vocabulary.
  - **(2) NO — and this is not a judgement call, it is impossible without inventing data.**
    `Feedback.content_piece_id` is `NOT NULL` with an FK to `content_pieces`, and verified
    2026-08-20 the ONLY thing in the application that ever inserts a `content_pieces` row is
    `content_store.create_landing_page`, reached from the `publish.page` actuator, which
    `run_executor._build_actuator_resolver` resolves at **EXPORT — after the gate**. So NO run has
    a content piece at REVIEW, approved or rejected; the review tabs are projected from
    `runs.checkpoint` by `review_service`, not from a row. **The loop must NOT: create a
    `ContentPiece` to hang the rejection on; make `feedback.content_piece_id` nullable; add a
    `run_id` column to `feedback`; or call `feedback_service.record` from this route.** The
    rejection is recorded on the run itself — `state` + `finished_reason` — and that is the whole
    record. *Filed, and deliberately NOT part of A10b's done:* the day a run persists its draft as
    a `content_pieces` row, a REVIEW rejection can carry piece-level feedback into `distil` and the
    loop `DIAGRAMS.md` §4 describes closes for real. That wants its own item — **it is A10d below,
    ruled and specced 2026-08-20 on the founder's decision to build it** — and the ordering was the
    point: inside A10b it would have been a fabrication. The caveat survives into A10d rather than
    being answered by it: `distil`'s theme table is style-specific while a gate rejection can be
    about anything, so the honest claim is that a gate rejection FEEDS distillation, not that it
    reliably produces a rule.
  - **(3) A reason is REQUIRED** — `min_length=10`, `max_length=240`, measured after whitespace
    collapse, refused 422 before anything is written. Required because a reasonless rejection is
    the one input this product can do nothing with, and the reviewer is the only person who will
    ever know why. 10 characters because it costs a real sentence nothing and it stops `x`/`no`/
    `bad`; no validator stops a determined shrug and none should pretend to. 240 and not 255
    because `runs.finished_reason` is `VARCHAR(255)` and `clamp_reason` TRUNCATES: silently
    shortening a provider stack trace is a cosmetic loss, silently shortening a person's stated
    reason is not — so the API's 422 is the only length refusal a human can ever meet, and the
    clamp stays a backstop for machine-authored reasons.
  - **The rejecter is deliberately NOT recorded — a difference from approve, not an oversight.**
    `approved_by` exists because it authorises an outward publish and lands on every `actions` row;
    a rejection authorises nothing and sends nothing. There is one user per business today
    (`business_for_user`, no membership table), so a `rejected_by` column would store what
    `business_id` already implies. When a business can have several users this becomes a `RunEvent`
    (`node="REVIEW"`, a fifth `EventStatus`) — which also costs the CLOSED frontend union
    `"started" | "done" | "failed" | "skipped"` in `runs/[runId]/page.tsx`, which is why it is
    neither free nor now.
  - **Migration** — hand-written like every other one here, in
    `backend/app/db/migrations/versions/` (root `alembic.ini`, `script_location` points there;
    CI applies it with `uv run alembic upgrade head`). File
    `e6a1c3f5b28d_run_rejected_state.py`, `revision = "e6a1c3f5b28d"`,
    `down_revision = "d4f18a6c93b7"` (verified head: nothing revises it). Template is
    `1fc3f48597dc_user_role.py`.
    `upgrade()`: `op.drop_constraint(op.f("ck_runs_state_valid"), "runs", type_="check")` then
    `op.create_check_constraint(op.f("ck_runs_state_valid"), "runs", "state in ('queued',
    'running','awaiting_approval','done','failed','partial','rejected')")` — no data write.
    `downgrade()`: the surviving rows must be mapped FIRST or the six-value constraint refuses
    them — `UPDATE runs SET state = 'partial' WHERE state = 'rejected'`, lossy and commented as
    such (`finished_reason` still says why) — and **that UPDATE must be wrapped in `ALTER TABLE
    runs NO FORCE ROW LEVEL SECURITY` / `... FORCE ROW LEVEL SECURITY`**: `runs` has FORCE RLS,
    the GUC is unset inside a migration, so a bare UPDATE matches ZERO rows and the downgrade then
    fails on rows it believes it fixed. The core-schema migration's own comment
    (`3b8336ae2975`, above the RLS block) already states this rule — follow it rather than
    rediscovering it. Mirror the seventh value in `models.py`'s `CheckConstraint` and in
    `RunState`. **`AgentState`'s `Outcome` literal is left EXACTLY as it is** (five values: `running`, `awaiting_approval`, `done`, `partial`, `failed` — an earlier draft of this entry said six, which was wrong; `queued` is a run-row state and never a graph outcome. The count is not a target, the instruction is "do not add `rejected`"): the graph never concludes
    a rejection, a person does, and `run_executor` already clamps graph outcomes to
    `{done, failed, partial}`.
  - **Contract** — `POST /api/v1/runs/{run_id}/reject`, body `{"reason": str}`. Mirrors approve
    where the shapes agree and differs only where the meaning does:
    - **200, not 202.** Approve is 202 because it starts minutes of work; reject starts none and is
      complete when it returns. Body `RunDecisionResponse{runId, state, finishedReason}` — its own
      model, and the third field is the point: the response echoes the STORED reason, so the screen
      renders what was persisted rather than what it sent.
    - **404 `run_not_found`** through the existing `_require_own_run`.
    - **409 `run_not_awaiting_approval`** for every other state — the SAME code as approve, because
      it is the same condition and one code lets one handler serve both buttons; the message says
      "rejected". A second reject is therefore a 409, never a silent no-op: same doctrine.
    - **422** from `Field(min_length=10, max_length=240)` on the request model — the same shape the
      goal bounds already produce, so the client can mirror the numbers as it mirrors `GOAL_MIN`.
    - **NO `no_checkpoint` refusal.** The one deliberate divergence: approve refuses a
      checkpoint-less run because the approval is written INTO the checkpoint and approving nothing
      is meaningless, whereas a rejection writes nothing there and a run parked having produced
      nothing is exactly what a reviewer should be able to dismiss. **A reviewer must always be
      able to say no.**
    - **No `run_already_executing` guard either** — verified in `run_executor._execute`: after
      `await_approval` the task returns and makes no further write, so the only window in which a
      parked run is still in `_live` is one where nothing can overwrite the rejection. Guarding it
      would refuse a legitimately parked run.
    - The route calls `service.finish(run_id, outcome="rejected", reason=...)` and **never touches
      the executor**. `runs.checkpoint` is left INTACT so the review tabs still render what was
      refused; clearing it is forbidden.
    - **`TERMINAL` in `api/runs.py` gains `rejected`**, or the SSE stream for a rejected run holds
      open the full `MAX_STREAM_SECONDS` (15 minutes) waiting for events that cannot come.
  **done = a reviewer's "no" is a terminal fact in the database that no other route can undo:
  `POST /api/v1/runs/{id}/reject` with a valid reason answers 200 and leaves `state='rejected'`
  with `finished_reason` = the cleaned reason; a missing, blank, too-short or over-long reason is
  422 BEFORE any write; every non-`awaiting_approval` state is 409 `run_not_awaiting_approval`,
  including a second reject; a run with NO checkpoint is still rejectable; `POST /approve` AND
  `POST /resume` on a rejected run are both 409 and NEITHER reaches the executor — asserted by a
  test that the executor was never submitted, because until `rejected` joins `resume`'s finished
  set a rejected run sails straight past the review gate and publishes, which is the real hole
  here; the checkpoint survives, proven by projecting the review after rejecting; and `alembic
  upgrade head` then `alembic downgrade -1` both succeed on a database containing a rejected run.**
- [x] **A10c · The reject control, and a rejected run that reads as a decision rather than a
  fault** — UI only, no schema. Depends on A10a (the decision card) and A10b (the route). The
  control lives in the SAME card as approve, because a decision surface with one option is not a
  decision; it is visually SECONDARY to approve (approve is the intended path) and NOT an alarming
  red (nothing is broken). Two-step: choosing reject reveals the reason field, so "no" is never one
  careless click and the required field is not a wall standing in front of the approve path.
  `rejectRun(runId, reason)` + `REJECT_REASON_MIN`/`REJECT_REASON_MAX` (mirroring `GOAL_MIN`/
  `GOAL_MAX`) + `canReject(state)` in `runs-api.ts` — the same predicate as `canApprove` today, its
  own name so the day they diverge the seam already exists. Then the three places a new state
  leaks, all of which are `string`-typed and fall through silently, which is why each needs an
  explicit test rather than a shrug: `runStateTone("rejected")` → **`muted`**, explicitly NOT `err`
  and NOT `warn` (a deliberate human "no" is neither a fault nor a shortfall, and a fault colour
  tells the owner the machine broke) — asserted so it is intent and not the accident of the default
  branch; `rejected` added to `TERMINAL_STATES` in `runs-api.ts` AND `TERMINAL` in
  `runs/[runId]/page.tsx`, or the screen polls and holds a stream open forever on a run nothing will
  ever move; and `nodeCaption` gains a `rejected` branch — a THIRD verb, because both existing ones
  lie about it ("waiting at REVIEW" implies a decision still pending, "stopped at REVIEW" implies a
  fault).
  **done = a reviewer can say no from the screen and the screen then reads as a decision: the
  reject control renders only in `awaiting_approval` and never as a disabled button; it refuses to
  submit under `REJECT_REASON_MIN` in its own sentence rather than by round-tripping a 422; a test
  asserts the client sends only a reason and never a rejecter; after rejecting, the state pill reads
  `rejected` in `muted` (asserted, not inherited), polling and the event stream both stop, the
  existing `finishedReason` well shows the typed reason under a heading that names the human
  decision instead of "Why it stopped" in `warn`, the runs list captions it with its own verb, and
  the review tabs stay MOUNTED so the refused draft is still readable — a rejected draft is
  evidence, and hiding it would withhold work the owner already paid for.**

- [~] **A10d · SUPERSEDED 2026-08-21 (FOUNDER) — there is no draft landing page to persist.**
  The whole task was scaffolding for a page the run created; with pages out of the run
  (`69a18f9`) its six rulings answer questions that no longer arise. **What must NOT be lost
  with it:** the thing A10d existed to enable — a rejection attaching to the piece it refused,
  so `DIAGRAMS.md` §4's `rejected, reason feeds the feedback loop` is real — survives as
  A10d-ii, whose subject is now the social renderings rather than a page. Original text:
  **A rejection has something to ATTACH to: the draft landing page is persisted before
  the gate, and EXPORT flips it instead of creating a second one** — RULED 2026-08-20 (architect)
  on the founder's decision to build it; no longer a decision. This is the item A10b filed and
  refused to do inside itself, and it REVERSES A10b's ruling (2) — conditionally, and only because
  the condition A10b named is exactly what this task creates. Once a `content_pieces` row exists at
  REVIEW, a rejection can carry piece-level feedback into `distil`, and `DIAGRAMS.md` §4's
  `REVIEW --> [*]: rejected, reason feeds the feedback loop` stops being a drawing of something the
  code cannot do. **What A10b forbade and what stays forbidden:** `feedback.content_piece_id` stays
  NOT NULL, `feedback` gets no `run_id` column, and no `ContentPiece` is EVER created in order to
  have something to reject. The inversion is the whole point — the piece is created because a
  reviewer is about to review it, and the rejection attaches to it only if it happens to be there.
  Six rulings, each answering a question the implementation cannot dodge.
  - **(1) WHERE the draft is persisted: in `run_executor._execute`, at the park boundary — NOT a new
    graph node, NOT inside CONVERT/REPACK, and NOT a new actuator.** Both drivers converge on
    `GraphResult(interrupted=True)` and the executor's `if result.interrupted: await
    service.await_approval(run_id)` is the ONE line they share, so persisting there holds in the
    builtin driver and in LangGraph with **zero changes to `agents/`** — no `nodes/__init__.py`, no
    `state.py`, no `graph.ORDER`, no driver. Ruled out, with reasons, so they are not re-proposed:
    *a node* is a database write inside the graph, which is the objection that made A1a an actuator
    in the first place and `tests/test_engine_boundary.py` is not the only reason it is wrong;
    *CONVERT* is reachable twice (`VALIDATE --> CONVERT` on a landing-only failure), so persisting
    there writes a piece per retry; *REPACK* is the last node before REVIEW and would work, but it
    is a model-calling node and the write has nothing to do with repacking. *A new actuator with a
    `publish.draft` action type* is the interesting near-miss and is REFUSED on two specific costs:
    `actuate()` requires a non-empty `approved_by`, so a pre-gate write would have to carry
    `"policy:…"` — putting a synthetic approver into a ledger whose `approved_by` exists to answer
    "which human authorised this outward publish"; and it would land an `actions` row that reads as
    a succeeded publish for something nobody can reach, on the Delivery tab whose entire job is
    telling a real publish from a simulated one. The actuator layer buys idempotency, audit and
    approval; this write needs the first, must not fake the third, and is actively harmed by the
    second. So it is a service call from the composition root: `landing_service`, unchanged,
    invoked at `status="draft"` — the mode its own docstring was written for ("approving the page
    lights them up without touching them"). `RunService` is deliberately NOT the caller either: it
    owns the `runs` table through a store abstraction, and handing it `content_pieces` widens it for
    nothing.
  - **The draft write is NON-FATAL and gated on the same audit as a publish.** It runs only when
    `state["landing_page"]` is a mapping with a non-empty `headline` — the same guard `_publishable`
    uses, so the two cannot disagree about whether there is a page — and `publish_landing_page`'s
    `LandingPageNotPublishableError` is CAUGHT: a page that cannot capture a lead persists nothing
    and the run still parks, exactly as `_resolve_owner_notice` already degrades. A run whose page
    fails the audit therefore has no piece, and per ruling (4) its rejection carries no feedback.
    That is the correct direction: no row, rather than a row for a page that could never be served.
  - **(2) HOW EXPORT stops double-creating: `publish.page` LOOKS FOR the draft belonging to THIS
    run and flips it; only when there is none does it create.** Precisely — the actuator stays thin
    and the branch lives in `landing_service`, so it is testable with no database: a new
    `promote_landing_draft(...) -> PublishedLandingPage | None` returns `None` when no draft exists
    and the actuator falls through to today's `publish_landing_page`. What promotion does: re-run
    `check_landing_page` FIRST (the refusal must not become bypassable by having pre-persisted — a
    page that cannot convert must be refused whether or not a row exists for it), then
    `mark_published(business_id, piece_id, url)` — a new store method rather than the existing
    `set_status`, because it also stamps `published_at` and `published_url`, two columns that are
    NULL on every row in the product today while `_LIST_HUB` orders by `cp.published_at DESC NULLS
    LAST`, i.e. by nothing — then READ BACK that piece's existing `short_links` through a new
    `links_for_piece(business_id, piece_id)` and return them as the `ctas`. **No link is minted at
    promotion.** The `Outcome.detail` shape is unchanged (`content_piece_id`, `path`, `status`,
    `score`, `ctas` with `channel/text/code/path/url`) so A1b's export pack keeps working, plus one
    new key naming which branch ran, because "created" and "promoted a draft" are different facts
    about this run even when they are the same fact about the world — the same distinction
    `Outcome.replayed` already draws.
  - **The lookup key is `content_pieces.run_id` via `actuation.run_id`, and NOTHING ELSE.** Not
    "the most recent draft landing page for this business", which would flip a different run's
    draft on a business with two parked runs — that is a wrong-page publish, not a near miss. Not a
    piece id threaded through `AgentState`, which would be a SECOND run-attribution mechanism built
    beside the one A1a-i is building, and would change the checkpoint shape twice. This is why A10d
    is BLOCKED on A1a-i (ruling 6) and it is a hard dependency, not merge hygiene: until
    `actuation.run_id` is populated the actuator cannot find the draft, so it would create a second
    piece and a second link set — shipping the bug this ruling exists to prevent.
  - **On a RESUME that retries EXPORT, nothing happens twice, and it is already guaranteed.**
    `actuate()` claims the content-derived key before calling, so a second attempt with the same
    spec is `replayed` and `perform` is never entered. What CHANGES is only what the key protects:
    it used to mean "do not create a second page", it now means "do not flip twice" — and the flip
    is idempotent anyway, so the two layers agree rather than depend on each other. Two adjacent
    behaviours are pre-existing, verified, and deliberately NOT changed here: `claim` replays a
    REFUSED row too, so a page refused by the audit cannot be retried under the same spec (it needs
    an edited spec, which is a different effect — correct, if surprising); and a run that crashes
    between the draft write and `await_approval` leaves an inert `draft` row nothing will promote,
    which is residue rather than a defect, because a draft is unreachable by construction (ruling
    4). **Re-parking is believed unreachable** — `arm_interrupt="REVIEW" not in state["visited"]`
    means a run parks at most once — so the draft write is create-if-absent keyed on
    `(run_id, surface='landing_page')` and, if a row already exists, it logs and creates nothing
    rather than updating. That ordering is chosen so that if re-parking ever DOES become reachable
    the failure is one stale draft, not duplicate pieces with duplicate link sets.
  - **(3) A draft row is SAFE to exist, verified in code 2026-08-20 rather than assumed.**
    `api/pages.py` `LIVE_STATUSES` and `api/leads.py` `LIVE_FORM_STATUSES` are both
    `{"approved", "published"}`, so `GET /p/{id}` is the same 404 as a typo and the public form
    POST is refused; `lead_store.HUB_VISIBLE_STATUSES` is the same pair, so a draft's CTAs are
    never advertised on the link hub. `resolve_short_link` deliberately does NOT filter on status,
    so a draft's `/l/{code}` DOES resolve and redirects to a 404 page — that is the honest
    degradation and **must not be "fixed"**: the code is unguessable, unadvertised, and the
    alternative is a redirect that lies. So a draft piece is visible to: nobody. Not the public,
    not the hub, and not the owner either — there is no content-listing route and `review_service`
    projects the review tabs from `runs.checkpoint`, so **the piece is an ANCHOR, not a new
    surface.** Do NOT wire the export pack or the review tabs to it; the A1b invariant ("no short
    link may appear unless a `short_links` row backs it") is satisfied by the pack reading
    `checkpoint["published"]["refs"]`, and re-pointing it at `content_pieces` would start printing
    a `/p/{id}` for a run that has published nothing.
  - **(4) WHAT a rejection attaches to: the piece, with `verdict="rejected"`, `axes = {}` and the
    reason already stored on the run — and the piece is marked `status="rejected"`, not deleted.**
    `content_pieces.status`'s CHECK constraint already permits `'rejected'`, so this needs NO
    migration and invents no vocabulary; and it is the right answer over leaving it `draft` (which
    says "awaiting a decision" about something decided) and over deleting it (the refused draft is
    evidence of work the owner paid for — the same reason A10c keeps the review tabs mounted). This
    also gives `content_store.set_status` its first application caller: today only tests call it.
    **`axes` is EMPTY and no code path may populate it.** `feedback_service.record` accepts a
    partial rubric by design ("someone saying 'the voice is wrong' should not have to invent an SEO
    score"), and `distil` reads ONLY `reject_reason` — so the four-axis rubric is satisfied by
    omission, and a reviewer at the gate is never asked for a rating they did not give. Whether the
    gate should ASK for the axes is a founder call, recorded below, not the loop's.
  - **One rejection writes at most ONE feedback row, and the guard is `status='rejected'` on the
    piece.** Not a new unique index: the existing state already answers the question. This matters
    beyond tidiness — `distil` proposes at three occurrences, so one rejection counted twice is a
    third of a fabricated pattern. Ordering: the piece-side writes (mark rejected → insert feedback
    → `distil`) run FIRST, inside one `business_session` so they cannot half-apply, and
    `service.finish(outcome="rejected")` runs LAST. If the piece side raises, nothing is written and
    the run is still `awaiting_approval`, so the reviewer presses reject again and it converges; the
    reverse order would leave a terminally rejected run — un-retryable, since every other state is
    409 — with no feedback and no way to add it. A10b's guarantee is preserved: a run with NO piece
    is still rejectable and still 200, and writes no feedback at all.
  - **(5) The rejecter is still NOT recorded.** `Feedback.user_id` is nullable and stays NULL, and
    the reject route still takes no `CurrentUser` — A10b's reasoning is unchanged by this task
    (`approved_by` authorises an outward publish; a rejection authorises nothing, and with one user
    per business a rejecter column stores what `business_id` implies).
  - **(6) SEQUENCING. A10d-i is blocked on A1a-i — hard, per ruling (2) — and collides with
    NEITHER A5 nor A6.** A5 touches `measure`, A6 touches `_export_refusal`, both inside
    `nodes/__init__.py`, and A10d touches no file under `agents/` at all; the only shared file with
    A1a-i is `run_executor.py`, and the two edits are in different functions (`_initial_state` vs
    the park branch of `_execute`). A6 is worth landing before or after without preference, but note
    the interaction and do not let it surprise anyone: a business at the weekly cap has EXPORT refuse
    before actuating, so its draft stays `draft` — correct, and it is why the piece must never be
    treated as evidence of publication.
  - **Claims discipline.** After this lands, `CRITERIA_MAP.md`'s "the agent updates persistent
    business preferences from explicit feedback" is true of the rejection path and is reachable from
    a screen (A10c's control) for the first time. It must not be written up as more than that: the
    gate rejection PROPOSES a rule, only at three occurrences, only after the owner approves the
    proposal does anything reach `businesses.dna`, and `distil`'s theme table is style-specific — a
    gate rejection about a wrong price falls through to exact-repeat grouping and needs three
    identically-phrased rejections. The honest sentence is "a gate rejection is recorded against the
    piece it refused and feeds the same distillation as a content rating", never "the agent learns
    from rejections".
- [~] **A10d-i · SUPERSEDED 2026-08-21 (FOUNDER)** with its parent A10d — nothing is persisted
  at the park boundary because nothing drafts a page. Original text: — backend
  only, no migration, no new action type. **Depends on A1a-i.** Atomic on purpose: persisting
  without the promotion branch ships a duplicate-piece bug, so the two halves may not be split.
  Touches `run_executor._execute` (park branch), `landing_service` (`promote_landing_draft`),
  `actuators/landing.py` (the two-branch `perform`), `content_store` (`mark_published`,
  `landing_page_for_run`) and `lead_store` (`links_for_piece`).
  **done = a run parked at REVIEW has exactly ONE `content_pieces` row, `status='draft'`, with its
  `run_id` set and its spec in `meta['landing']`, plus one `short_links` row per channel CTA, each
  `?ref=<code>`-retargeted; `GET /p/{id}` on that draft is 404 and a POST to its form endpoint is
  refused, asserted rather than assumed; approving that run PROMOTES that row — the piece count is
  still 1 after EXPORT, no second link set exists, the row reads `status='published'` with
  `published_at` and `published_url` stamped, and the `publish.page` `Outcome.detail.ctas` carries
  the SAME codes the draft minted, so A1b's pack is unchanged; a run whose actuation carries no
  draft still publishes by the create path (asserted with the promotion lookup finding nothing); a
  landing page that fails `check_landing_page` persists NO draft and the run still parks; a second
  EXPORT attempt is `replayed` and promotes nothing twice; and a test asserts `review_service`'s
  pack still prints no `/p/` and no `/l/` for a run that has not published, because it still reads
  `checkpoint['published']['refs']` and never `content_pieces`.**
- [ ] **A10d-ii · A gate rejection is recorded against the piece it refused, and feeds `distil`** —
  backend only, no migration. **Depends on A10d-i.** One service function so the route stays a
  route and the three writes are one transaction; the response gains an additive
  `proposedRules: list[str]` (camelCase, empty by default) so the loop's output is reportable
  without A10c's screen changing.
  **done = rejecting a run that has a draft piece leaves that piece `status='rejected'` and exactly
  ONE `feedback` row against it, with `verdict='rejected'`, `axes == {}` and `reject_reason` equal
  to the reason stored on the run; three rejections carrying the same themed reason produce a
  `learned_style` row with `status='proposed'` and a test reads `businesses.dna` to assert it is
  untouched; rejecting a run with NO piece is still 200, writes no feedback and raises nothing;
  a second reject is still 409 `run_not_awaiting_approval` and the feedback row count is asserted to
  be 1, because at a three-occurrence threshold one rejection counted twice is a fabricated pattern;
  a failure injected into the piece-side writes leaves the run `awaiting_approval` and rejectable
  rather than rejected-with-no-record; and `axes` is asserted EMPTY, with no code path and no test
  fixture supplying a rating a reviewer did not give.**
- [ ] **A10e · The reviewer sees what their "no" proposed** — UI only, depends on A10d-ii. The
  rejection panel renders `proposedRules` as "we noticed a pattern" with a link to the proposals
  panel, and renders NOTHING when the list is empty — which is the normal answer and must not read
  as a failure. Filed separately so A10d-ii cannot grow a screen.
  **done = a rejection that proposed a rule says so and links to where it waits; one that proposed
  nothing shows no empty state; asserted in both cases.**

- [ ] **A2c · A node that raises loses its retrieval trace along with everything else** — found
  by A2a, pre-existing, and deliberately not changed there. `GENERATE` and `CONVERT` raise
  `ValueError` when the model returns no tool call; the driver converts that to a `NodeError` and
  **discards the whole update dict** — so the trace for that node is lost, and so is `_cost`. It
  is not retrieval-specific: every field the node computed before it raised goes the same way,
  which is why fixing it inside A2a would have been the wrong shape. `OPPORTUNITY`'s no-args path
  already does it right by RETURNING rather than raising, so the pattern to follow exists.
  **done = a node that fails after doing chargeable work still reports that work (its cost and
  its trace) alongside the error, in BOTH drivers; a test asserts the cost of a failed GENERATE
  is not silently dropped.** Note this is a money-visibility bug as much as an evidence one: a
  run can spend on a node whose spend is then never recorded.

## B · ⛔ BLOCKED — what the human must supply, and the exact question

- [ ] ⛔ **The OpenRouter account's data policy** — cheapest unblock available and it
  releases three items at once. Ask: *"On the OpenRouter account behind this key, enable the
  data policy that permits mid- and strong-tier models (it currently returns 404 `no
  endpoints matching your guardrail restrictions`), or supply a key whose account already
  does."* Unblocks: a live run reaching OPPORTUNITY/PLAN/GENERATE at all, `evals/run.py
  --tier {mid,strong}`, and the first real dollar figures in `model_usage`. **Costs money to
  use, so the run itself stays gated too.**
- [ ] ⛔ **`TAVILY_API_KEY`** — ask: *"Provide a Tavily key (free tier is enough for the demo)
  so `serp.search` and HARVEST's competitor discovery run against real search instead of the
  fake."* Structural half is done; without it HARVEST honestly reports "no provider
  configured", which is correct and is why the loop must not wire the fake in its place.
- [ ] ⛔ **Langfuse keys** — ask: *"Provide `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  (EU region), or confirm H2 is demoed from `model_usage` + the run timeline instead."* The
  seam is a no-op without keys by design; demo step 13 names a Langfuse trace.
- [ ] ⛔ **`RESEND_API_KEY`** — ask: *"Provide a Resend key and a verified sending domain, and
  name one address you consent to receive a test send at."* What a key proves that
  `MockTransport` cannot: real status codes, that a 200 always carries `id`, and that
  `List-Unsubscribe` survives delivery. **Sending to a real address is outward-facing** —
  the loop must not do it even with a key present.
- [ ] ⛔ **A live Ragas measurement** — several judge calls per case-arm, real money. Ask:
  *"Authorise roughly N judge calls of spend for one `--ragas --live` run?"* Everything up to
  the provider call is already proven.
- [ ] ⛔ **`answer_relevancy` needs an embeddings endpoint** — ask: *"Set
  `RAGAS_EMBEDDINGS_MODEL` plus a key that serves embeddings."* Reported as `n/m` until then,
  never as a score, which is correct.
- [ ] ⛔ **Regenerating `evals/report.md`** — the checked-in report is a real `--live` run
  (2026-08-19, `gpt-4.1-mini`, real money) and regenerating it hermetically would overwrite
  measured numbers with FakeProvider canned strings. Ask: *"Authorise ~280 mid-tier calls for
  one `--live --deepeval` run over 20 cases × 2 arms?"* **The loop must never regenerate this
  file hermetically to fix its header** — that destroys evidence.
- [ ] ⛔ **Tier 1 direct publish** — per-platform App Review (Meta screencast + privacy
  policy + business verification, 2–6 weeks and refusable; LinkedIn MDP; TikTok audit). Ask:
  *"Do you want to start the Meta/LinkedIn/TikTok review applications, accepting that they
  may be refused and cannot land inside this timeline?"* No real `OAuthProvider` or
  `SocialPublisher` should be written before an approval exists — untested code pretending
  to be a feature is worse than a stated gap.
- [x] ⛔ **The Docker `images` CI job is unverified** — RESOLVED 2026-08-21 by the push, which
  is exactly what §C said this one needed. It has been running on every `main` push since
  2026-08-20 and **passing**: run 32381757702 reports `Docker — build both images: success`
  alongside `Python — lint, types, tests: success`. So both images build in CI; only the local
  15-minute build was the problem, and CI's cache is why it is not CI's problem. Original text:
  — the local build ran past 15 minutes
  twice and was killed. Verifying it properly means a push, which is outward-facing. Ask:
  *"Push the branch so the `images` job runs, or authorise an unbounded local `docker build`?"*
  Fold it into the next PR the human pushes anyway. **Packaging note for the safe-html
  commit: `frontend/vite-raw.d.ts` is currently UNTRACKED and `safe-html.test.tsx` imports
  `./safe-html.tsx?raw`. `tsconfig.json` includes `**/*.tsx`, so if the suite lands without
  that declaration file, `pnpm typecheck` AND `next build` both fail and the web job goes
  red. Commit the two together.**

## C · The residue — why "finish the backlog" cannot mean "no open items"

Every item in §A can be closed by the loop. **No item in §B can**, and there is no honest
way to tick one: each needs a credential, real money, a third party's approval queue, or a
push. Manufacturing a green tick on any of them would break the guardrail against
presenting synthetic output as real, which is the one rule this project has enforced
hardest.

So the truthful stopping point is: **§A closed, §B still open and correctly marked.** That
is the report to give — not a fully-ticked file. The residue is, precisely: three
credentials (Tavily, Langfuse, Resend), two spend authorisations (live Ragas, the
`--live --deepeval` report), one account setting (the OpenRouter data policy), one external
approval queue (App Review), and one push (the `images` job).

## D · Deliberately deferred / won't do — do not rediscover these

- **GSC/GA4 `analytics` engine** — cut; two OAuth flows for a metric that cannot move inside
  the timeline. `analytics.fetch` stays granted-and-unwired so the omission is named.
- **Per-business model override (`model_routes.business_id`)** — one nullable column, no
  demand. Leave it.
- **A `channel_specs` config table** — resolved by `engines/channel/specs.py`; the drift this
  was about was between two code copies, and nothing has asked for per-tenant limits.
- **Login CSRF** — deliberate. A pre-login request carries no cookie to check; closing it
  needs a pre-session synchronizer token the Origin-validation design avoids. `SameSite=Lax`
  blocks the cross-site POST.
- **Text extraction as a security boundary** — deliberate, and MEASURED rather than assumed:
  the tests assert the survivals as well as the drops precisely so this can never be
  upgraded into "hidden instructions never reach the model". The barrier is the tool
  allowlist.
- **Refusing a cookie-bearing request with no `Origin`/`Referer`** — intended; the cookie is
  a browser credential and there is no machine-to-machine mode.
- **Folding `statusTone` into `documentTone`** — deliberate; a caller with only a status
  string is a real case. Fold if a third colour rule appears.
- **Transaction-per-test or schema-per-run for the `db` suite** — real and larger than it
  looks: every `db` file writes to shared tables and teardown is by pattern. Deferred with
  the reason, not forgotten.
- **A sweeper for runs stranded `running`** — that is a worker's job; `ROADMAP` names
  ARQ/Redis and it is not installed. `POST /runs/{id}/resume` plus the UI control is the
  recovery path today.
- **Real `OAuthProvider` / `SocialPublisher`** — absent on purpose until an App Review
  approval exists to exercise them against.
- **A platform-wide cost total for the operator** — cannot come from reading `model_usage`
  as one tenant; needs a `SECURITY DEFINER` aggregate written for the purpose, in the same
  posture as the other cross-business definer functions. Named by A9, deliberately not built
  there: new scope, and nobody has asked for it.
- **An owner-facing per-business spend view** — a PRODUCT question for the human, not a bug:
  does a normal owner get to see their own model cost? The read is already tenant-scoped and
  refusing it protects nothing, but `BUILD_ORDER.md` Phase 9 deliberately puts the cost
  dashboard in developer mode. Ask before building. If the answer is yes, it is a new USER-mode
  screen on its own route — not a loosened gate on `/developer/cost`.
- **Wikipedia editing · a customer mobile app · autonomous publishing · paid ads** — cut in
  `CLAUDE.md` / `BUILD_ORDER.md`. Cut things stay cut unless the human reopens them.
