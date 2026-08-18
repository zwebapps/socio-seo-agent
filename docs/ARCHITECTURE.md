# Architecture — Production-Grade Growth Agent

Companion to [../ROADMAP.md](../ROADMAP.md) (what to build, in order) and [../FEATURES.md](../FEATURES.md) (what it does for a customer). **This document is the technical design.**

Everything lives inside `Social-Marketing-Agent/`. Nothing in the parent `TuringCollege/` folder is read, written, or depended on.

---

## 1. Architecture → business benefit

An architecture document that can't name the business outcome of each decision is decoration. This table is the contract; the rest of the document is how it's honoured.

| Architectural property | How it's achieved | What the business gets |
|---|---|---|
| Output is trustworthy | Engines compute, agents only decide/write; every claim grounded in the customer's documents or a cited source | Content they can publish without fact-checking every line |
| Nothing embarrassing goes live | Approval policy table + `interrupt()` before any Actuator | One hallucinated claim can't end their customer relationship |
| Never publishes twice | Idempotency key on every Actuator call, unique-indexed | No duplicate posts, no double-charged actions |
| A crash costs minutes, not a rerun | Checkpointed run state; resume at the failed node | Work paid for is never lost |
| Costs are predictable | Model router by task tier + per-run and per-business budget ceilings checked *before* each call | A flat monthly price with defensible margin |
| Leads are provable | UTM on every link + `content_piece → lead` attribution | They can see ROI, so they renew |
| Results move in weeks, not quarters | AI share-of-voice probing + conversion-first onboarding | Something measurable before the first invoice |
| One customer can't see another | `business_id` on every row + Postgres RLS + a cross-tenant test | Safe for agencies; GDPR-defensible |
| Quality doesn't silently regress | Eval harness gates prompt and model changes | Swapping a model can't quietly wreck their content |
| Their data isn't a training set | EU-region providers, no-train terms, documents never leave our store except as prompt context | Answerable in a procurement questionnaire |

---

## 2. System context

```
   ┌──────────┐        ┌─────────────┐        ┌──────────────────┐
   │ SMB owner│        │ Agency user  │        │ Their website    │
   │ / marketer│       │ (many biz)   │        │ visitors → leads │
   └────┬─────┘        └──────┬───────┘        └────────┬─────────┘
        │  browser            │                          │ form POST
        ▼                     ▼                          ▼
   ┌───────────────────────────────────────────────────────────────┐
   │                    GROWTH AGENT PLATFORM                      │
   └───┬──────────┬──────────┬──────────┬──────────┬───────────────┘
       │          │          │          │          │
       ▼          ▼          ▼          ▼          ▼
  LLM providers  Search/   AI answer   CMS      Google
  (OpenRouter)   SERP API  engines    (WordPress) GSC/GA4
                 + crawl   (probed)   webhooks
```

**Trust boundaries.** Everything crossing into the platform from outside is untrusted: crawled HTML, uploaded documents, model output, and public form submissions. Each has a named control in §9.

---

## 3. The three component kinds

The single most important rule in this codebase.

```
                 ┌─────────────────────────────┐
                 │   AGENTS  (LLM: decide)     │
                 │   plan · prioritise ·        │
                 │   interpret · write          │
                 └───────┬─────────────┬────────┘
                         │ read        │ request action
                         ▼             ▼
     ┌───────────────────────┐   ┌──────────────────────────┐
     │ ENGINES               │   │ ACTUATORS                │
     │ read + compute only   │   │ external side effects    │
     │ • no side effects     │   │ • idempotency key        │
     │ • no LLM import       │   │ • approval policy        │
     │ • no DB writes        │   │ • audit log entry        │
     │ • pure in → pure out  │   │ • retry-safe             │
     └───────────────────────┘   └──────────────────────────┘
```

| | Engines | Actuators | Agents |
|---|---|---|---|
| Examples | `crawl`, `seo`, `serp`, `geo`, `kb`, `analytics`, `social.validate` | `publish.wordpress`, `social.post`, `notify.email`, `sitemap.ping` | `growth_manager`, `research`, `content`, `social`, `geo` |
| Determinism | total | total, given the key | none |
| Test style | unit, faked HTTP | unit + replay test proving no double-execute | eval harness |
| Failure | typed error | returns prior result, or fails closed | wrong choice → caught by `VALIDATE` |
| Cost | API calls only | API calls only | tokens |

**Enforced, not documented.** A test walks `backend/app/engines/**` and fails the build if any module imports an LLM client, a DB session, or an Actuator. The boundary is the architecture; if it rots, everything else in this document becomes untrue.

**Why three, not two.** Idempotency, approval policy, and the audit log all apply to exactly one class of operation: the ones with external side effects. Naming that class puts those three concerns in one place instead of scattering them through the graph.

### Engine contracts

Every engine is a package exposing typed functions. No DB, no LLM, no global state.

```python
# engines/seo/contract.py
class SeoScoreRequest(BaseModel):
    html: str
    target_keyword: str
    secondary_keywords: list[str] = []
    locale: str = "en"


class SeoFinding(BaseModel):
    code: Literal[
        "title_length",
        "meta_length",
        "keyword_density",
        "readability",
        "heading_tree",
        "internal_links",
        "external_links",
        "image_alt",
        "schema_invalid",
    ]
    severity: Literal["error", "warn", "info"]
    message: str  # human-readable, shown in the UI
    fix_hint: str  # fed back to GENERATE on a validation loop
    measured: float | None
    expected: str


class SeoScoreResult(BaseModel):
    score: int  # 0–100, deterministic
    findings: list[SeoFinding]
    passed: bool  # score >= 85 and no error-severity findings
```

`GENERATE` receives `fix_hint`s verbatim on a retry — the model is told exactly what failed, never asked to guess.

### Actuator contract

```python
class ActuatorRequest(BaseModel):
    business_id: UUID
    run_id: UUID
    action_type: Literal["publish_cms", "post_social", "send_email", "ping_sitemap"]
    idempotency_key: str  # business:workflow:task:action
    payload: dict
    approval_token: str | None  # required when policy != "auto"


class ActuatorResult(BaseModel):
    status: Literal["executed", "replayed", "refused_needs_approval", "refused_policy", "failed"]
    external_ref: str | None  # CMS post id, tweet id …
    result: dict
```

Execution order inside the Actuator service, and it is never reordered:

```
1. resolve policy for (business, action_type)      → auto | notify | approve | human
2. if policy needs approval and no valid token     → refused_needs_approval
3. SELECT … WHERE idempotency_key = ?              → if succeeded, return replayed
4. INSERT actions row (status=in_flight)           → unique index is the lock
5. call the external API                            (timeout, bounded retry)
6. UPDATE actions (status, external_ref, result)
7. INSERT audit_log
```

Step 4 before step 5 is the whole point: a crash between 5 and 6 leaves an `in_flight` row, and the reconciler (§7.3) asks the provider what actually happened rather than blindly retrying.

---

## 4. Component architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  frontend/  Next.js 16 · React 19 · Tailwind 4 · shadcn/ui           │
│  user mode: onboarding · documents · opportunities · run timeline ·   │
│             review & approve · lead inbox                            │
│  /developer (role-gated server-side): models · sliders · prompt       │
│             versions · tool toggles · raw traces                      │
└───────────────┬──────────────────────────────────┬───────────────────┘
                │ REST + SSE                       │ public form POST
                ▼                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  backend/app/api  FastAPI (stateless, N replicas)                    │
│  auth · businesses · documents · runs · opportunities · content ·     │
│  approvals · leads · metrics · feedback · settings · public/forms     │
│  ── middleware: authn → tenant resolve → rate limit → budget guard    │
└───────────────┬──────────────────────────────────────────────────────┘
                │ enqueue (Redis / ARQ)
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  WORKERS (separate pools — a slow crawl must not starve content)      │
│  ├─ worker-content   LLM-heavy graph runs        concurrency 4        │
│  ├─ worker-harvest   crawl / SERP / probe        concurrency 8        │
│  └─ scheduler        geo probes · metric pulls · opportunity engine    │
└───────────────┬──────────────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AGENT RUNTIME  LangGraph state machine + Postgres checkpointer       │
│  INTAKE → HARVEST → OPPORTUNITY → PLAN → GENERATE → VALIDATE →        │
│  REPACK → REVIEW(interrupt) → EXPORT → MEASURE                        │
└───┬──────────────┬───────────────┬───────────────┬───────────────────┘
    │              │               │               │
    ▼              ▼               ▼               ▼
 ENGINES       ACTUATORS      MODEL ROUTER     TOOL REGISTRY
 crawl kb seo  publish        task→tier,       JSON schemas,
 serp geo      social.post    fallback,        per-business
 social        notify         cost ledger      on/off
 analytics
    │              │               │               │
    └──────────────┴───────────────┴───────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Postgres 16 + pgvector   │  Redis  │  S3-compatible  │  Langfuse    │
│  app data · RAG · runs ·  │  queue  │  documents ·    │  traces ·    │
│  checkpoints · ledger     │  cache  │  exports        │  costs       │
│                            │  rate   │                 │  scores      │
└──────────────────────────────────────────────────────────────────────┘
```

**Layering rule:** `api → services → {engines, actuators, agents} → adapters`. Engines never import services. Agents never import adapters directly — only through the tool registry. A dependency test enforces the direction.

---

## 5. Run lifecycle

```
User clicks "Create content"
   │
   ├─ API validates request, checks business budget remaining
   ├─ INSERT runs (state=queued, plan=[…], budget_usd=0.50)
   ├─ enqueue job(run_id)                         ← returns 202 immediately
   └─ client opens GET /runs/{id}/events (SSE)
        │
   worker-content picks up job
        │
   ┌────▼─────────────────────────────────────────────────────────┐
   │ for each node:                                               │
   │   check step cap (14) and budget remaining ──► exceed? stop  │
   │   emit SSE event {node, status:started}                      │
   │   execute                                                     │
   │     engines → structured facts (parallel where independent)   │
   │     agent   → model call via router; write model_usage row    │
   │   checkpoint AgentState to Postgres        ← resume point     │
   │   emit SSE event {node, status:done, cost, summary}           │
   └───────────────────────────────────────────────────────────────┘
        │
   REVIEW node → interrupt()
        │  run state = awaiting_approval, worker releases the job
        │  UI shows draft · SEO findings · social posts · AI-answer blocks
        │
   Human approves ──► API resumes the graph from the checkpoint
        │
   EXPORT: Actuator(publish_cms, idempotency_key=…) → draft in their CMS
        │
   scheduler, later: MEASURE → geo re-probe, GSC pull, lead attribution
        └──────────────► feeds the opportunity engine → next run
```

**Every SSE event is also persisted**, so a user who reloads mid-run sees the same timeline replayed from the database rather than an empty screen. The stream is a convenience; the database is the truth.

---

## 6. Data architecture

### Storage split

| Store | Holds | Why there |
|---|---|---|
| Postgres | businesses, DNA, crawl snapshots, keywords, competitors, geo prompts/results, opportunities, content, social posts, leads, runs, actions, approvals, ledger, feedback, **LangGraph checkpoints** | one transactional store; a run and its cost land atomically |
| pgvector (same DB) | `kb_chunks.embedding` | no second database to operate for RAG |
| Redis | job queue, provider rate-limit token buckets, SERP/crawl response cache, idempotent-request short cache | ephemeral, and the buckets must be **shared across workers** |
| S3-compatible | uploaded documents, generated exports (MD/HTML/PDF), OG images | blobs don't belong in Postgres; keeps DB backups small |
| Langfuse | traces, per-call cost/latency, eval scores, user feedback as scores | purpose-built; queryable for regressions |

### Multi-tenancy

Three layers, weakest to strongest:

1. **Repository layer** — every query goes through a repo that requires `business_id`; no route touches a raw session.
2. **Postgres RLS** on all business-scoped tables, with the tenant set as a transaction-local GUC (`SET LOCAL app.current_business_id`).
3. **A test** asserting user B reads zero rows of user A's data across every table.

> **Warning from experience:** once RLS is on, any legitimately cross-business read — the admin dashboard, platform analytics, the nightly opportunity sweep — returns **zero rows silently** rather than erroring. Those paths must go through explicit `SECURITY DEFINER` functions with the cross-tenant predicate hardcoded inside. Never weaken a policy to make an admin page work, and never assume a migration's `UPDATE` touched anything on a FORCE-RLS table without checking the row count.

### Caching (correctness first, cost second)

| Layer | Key | TTL | Guard |
|---|---|---|---|
| Crawl | `url + ETag/Last-Modified` | 24 h | conditional GET; 304 → reuse |
| SERP | `normalise(query) + locale` | 24 h | never cache across businesses' *rankings*, only the SERP itself |
| Embeddings | `sha256(chunk_text)` | permanent | identical chunk never re-embedded |
| Prompt cache | provider-side, stable system prefix | provider | put the volatile part last |
| GEO probes | **never cached** | — | the whole point is a fresh measurement |

---

## 7. Reliability

### 7.1 Resumability
`AgentState` is checkpointed after every node. A killed worker resumes at the failed node; `runs.resumed_count` records it. A run stuck `in_progress` past its lease is requeued by the scheduler.

### 7.2 Retry policy

| Failure | Policy |
|---|---|
| Provider 429 | honour `Retry-After`, else exponential backoff ×3, then the router's fallback model |
| Provider 5xx / timeout | backoff ×3, then fall back; on exhaustion fail the **node**, not the run |
| Malformed tool arguments | one repair turn carrying the validation error, then skip the tool and record it |
| Engine network error | mark that fact-source unavailable, continue with partial facts, surface it in the UI |
| Actuator failure | never blind-retry — reconcile first (below) |

### 7.3 Idempotency and reconciliation
Unique index on `actions.idempotency_key` is the lock. An `in_flight` row older than its timeout is reconciled by asking the provider whether the action landed (search the CMS for the slug, the social API for the post) before any retry. **A safety net that guesses is not a safety net.**

### 7.4 Budgets and caps
Checked *before* each model call, at three levels: per run (USD + 14 steps), per business per month (USD), per business per week (published pieces — this one is a quality control, not a cost control). Exceeding any cap ends the run with a partial result and a stated reason. Never an infinite loop.

### 7.5 SLOs (starting targets)

| Indicator | Target |
|---|---|
| API availability | 99.5% |
| p95 time to first SSE event | < 2 s |
| p95 full run to `awaiting_approval` | < 4 min |
| Runs completing without human intervention | > 90% |
| Duplicate published actions | **0** — any occurrence is a Sev-1 |
| Cost per content piece | < $0.15 |

---

## 8. Model routing and cost

```
Agent node ──► ModelRouter.resolve(task_class, business_policy)
                     │
                     ├─ tier table  (config, not code)
                     ├─ fallback chain
                     └─ budget guard ──► refuse before spending
                     ▼
               OpenRouter (OpenAI-compatible)
                     ▼
      model A ─fail→ model B ─fail→ model C
                     ▼
            model_usage row: tokens, USD, latency, prompt_version, run_id, node
```

| Task class | Tier | Nodes |
|---|---|---|
| classify / extract / repack | cheap | INTAKE, REPACK, `geo` answer parsing |
| plan / prioritise | mid | OPPORTUNITY, PLAN |
| long-form generation | strong | GENERATE |
| final quality review | strong | optional review pass |
| embeddings | dedicated small model | `kb` ingest |

**Never a hardcoded model name outside `models/`.** Prompts are versioned files (`prompts/generate.v3.md`); the version is recorded on every call, so an eval can attribute a quality change to a prompt or a model rather than to folklore.

**Cost control, in order of leverage:** route by task tier → cache SERP/crawl → stable prompt prefix for provider-side caching → cap output tokens per node → deduplicate embeddings → only then consider a cheaper strong model.

---

## 9. Security architecture

```
UNTRUSTED INPUT              CONTROL
────────────────────────────────────────────────────────────────────
Crawled HTML          →  data envelope + instruction hierarchy;
                         per-node tool allowlist; Actuators
                         unreachable from any harvested text
Uploaded documents    →  type + size check, PII scan, same envelope,
                         no macro/script execution, stored in S3 not DB
Model output          →  schema validation; brand/regulated-claim guard;
                         no raw HTML injected into the CMS unsanitised
Public form POST      →  honeypot + Redis rate limit + optional captcha,
                         strict field schema, no reflection
Provider responses    →  size caps, timeouts, typed parsing
```

**Prompt injection is the top risk in this product**, because the agent reads attacker-controllable pages and can reach a publish Actuator. Three independent barriers, any one of which is sufficient:

1. Harvested text is wrapped in explicit markers with a system rule that content inside is data and never instructions.
2. Each node has a tool allowlist; `HARVEST` and `GENERATE` cannot call an Actuator at all — only `EXPORT` can, and only with an approval token.
3. Human approval before publish for any business whose policy isn't `auto`.

A 10-payload injection corpus is a test, not a checklist.

**Other controls.** SSRF: HTTPS only, DNS-resolve and reject private/link-local/metadata ranges, 5 s timeout, 2 MB cap, `robots.txt` respected, redirect chain re-validated at every hop. Secrets: env-only, never in a browser bundle, provider keys never per-tenant in v1. AuthN: argon2 + HMAC session cookie, `httpOnly`, `sameSite=lax`, **no `domain` attribute**. AuthZ: role checked server-side — developer mode is a server-rendered gate, not a hidden route. Audit log: every Actuator call, approval, policy change, and settings edit, append-only. GDPR: EU-region providers with no-train terms, per-business export and delete, documents deletable with their embeddings, retention configurable.

---

## 10. Frontend architecture

```
app/
├─ (marketing)/                public, static, no auth
├─ (auth)/login  signup
├─ (app)/
│  ├─ layout.tsx               session + business resolve → redirect, once
│  ├─ businesses/[id]/
│  │  ├─ page.tsx              dashboard: SoV, leads, opportunities
│  │  ├─ documents/            upload, status, reindex
│  │  ├─ opportunities/        ranked list → "create content"
│  │  ├─ runs/[runId]/         SSE timeline: nodes, tool calls, live cost
│  │  ├─ content/[pieceId]/    tabs: draft · SEO findings · social · AI blocks
│  │  │                        edit → approve → export
│  │  ├─ leads/                inbox with attribution
│  │  └─ settings/             brand voice, channels, approval policy
│  └─ developer/               role-gated: models, sliders, prompt versions,
│                              tool toggles, raw traces
└─ f/[formId]/                 PUBLIC lead form — no auth, no cookies,
                               its own minimal bundle
```

**Rules.** Server components for reads; client islands only for the SSE timeline, the editor, and the form. The public lead form is a separate route group with its own tiny bundle — a slow form is a lost lead. Every destructive or publishing action is a server action with a confirm step. Streaming a run must degrade to polling if SSE drops. WCAG AA: focus states, contrast, keyboard paths, `aria-live` on the run timeline.

**Guard placement:** authentication and business resolution happen once, in the segment layout, as a server-side `redirect()` — not in middleware (which would make public routes vary on cookie) and not duplicated per page.

---

## 11. API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/signup` · `/auth/login` · `/auth/logout` | argon2, HMAC cookie |
| GET POST | `/businesses` | list / create |
| POST | `/businesses/{id}/onboard` | crawl + extract → draft DNA |
| PATCH | `/businesses/{id}/dna` | user confirmation |
| POST GET DELETE | `/businesses/{id}/documents` | upload → ingest job |
| POST | `/businesses/{id}/harvest` | engines only, no LLM — cheap refresh |
| GET | `/businesses/{id}/opportunities` | ranked |
| POST | `/runs` | `{business_id, goal, surfaces[]}` → 202 + run_id |
| GET | `/runs/{id}` · `/runs/{id}/events` | state / SSE stream |
| POST | `/runs/{id}/resume` | after approval |
| GET PATCH | `/content/{id}` | draft, edits |
| POST | `/content/{id}/approve` · `/publish` | approval token → Actuator |
| GET | `/businesses/{id}/geo` | share-of-voice + trend |
| GET | `/businesses/{id}/leads` | attributed |
| **POST** | **`/public/forms/{formId}`** | **unauthenticated lead capture** |
| POST | `/feedback` | rating + axes + reject reason |
| GET PATCH | `/settings/{business_id}` | voice, channels, approval policy, tool toggles |
| GET | `/health` · `/metrics` | liveness, internal-only metrics |

Versioned as `/api/v1` from the first commit. Money-like values are integers. Enum consumers must tolerate unknown values.

---

## 12. Deployment

**Now — one VPS, docker-compose** (matches how you already run production elsewhere):

```
Cloudflare ──► Caddy (TLS) ──┬──► web      (Next.js, 1)
                             ├──► api      (FastAPI, 2 replicas)
                             └──► /public/forms/* (same api, own rate limit)
                                    │
              worker-content (2) · worker-harvest (2) · scheduler (1)
                                    │
              postgres+pgvector · redis · minio · langfuse
```

Rules: workers are separate services from `api` so a long run can't block a request; `scheduler` is a single replica (its jobs are not concurrency-safe); `api` is stateless and horizontally scalable; migrations run in an entrypoint before the app starts, never inside the app.

**Two operational traps worth naming now**, both learned the hard way elsewhere: build images in CI and pull on the box — a small VPS will not compile a Next.js image; and never construct a Postgres URL by interpolating a raw secret, because generated passwords contain `/ + @` and the URL parser will reject it at boot. Percent-encode in the entrypoint.

**Capacity model** — the load is harvesting, not generation:

| At | Runs/month | Worker-hours/month | Verdict |
|---|---|---|---|
| 20 businesses × 8 pieces | 160 | ~5 | trivial |
| 200 × 8 | 1,600 | ~50 | one VPS, comfortable |
| 2,000 × 8 | 16,000 | ~500 | split pools, read replica, provider quotas become the ceiling |

**Later:** the same compose maps to Kubernetes with no code change, because state is already externalised (Postgres, Redis, S3) and workers are already separate processes.

---

## 13. Observability

Every LLM and tool call is traced with `run_id`, `business_id`, node, model, prompt version, tokens, USD, latency, and outcome. User feedback is attached to the trace as a score, which is what makes "did the agent do a good job?" answerable rather than a matter of opinion.

| Question | Answered by |
|---|---|
| Why did this article come out badly? | trace → node inputs, tool results, prompt version |
| What does a content piece cost us? | `model_usage` grouped by `run_id` |
| Did prompt v3 improve anything? | eval report v2 vs v3 on the fixed dataset |
| Is a provider degrading? | latency and error rate by model |
| Which businesses are unprofitable? | cost per business vs plan price |
| Are we producing leads? | `leads` joined to `content_pieces` |

Alerts worth having on day one, and no more: any duplicate Actuator execution, run failure rate > 10% over an hour, provider error rate > 20% over 15 min, a business crossing 80% of its monthly budget, and any `in_flight` action older than its timeout.

---

## 14. Key decisions and rejected alternatives

| Decision | Rejected | Why |
|---|---|---|
| LangGraph state machine | bare ReAct loop | resumability, `interrupt()` for approval, bounded steps, evaluable nodes |
| Engines / Actuators / Agents | "one agent with tools" | side-effect concerns (idempotency, approval, audit) need their own class |
| OpenRouter | direct provider SDKs | one integration for multi-model, routing, fallback; no lock-in |
| Postgres + pgvector | Postgres + a dedicated vector DB | one store to operate and back up; sufficient to millions of chunks |
| ARQ/Redis workers | Celery · Temporal | Celery is heavier than needed; Temporal is the right answer only once workflows span days |
| Deterministic SEO scoring | LLM-judged SEO | counting is not a language task; determinism makes it testable and free |
| Probe-based AI visibility | waiting for a citation API | none exists; probing is measurable now and is the wedge |
| Approval policy as a table | policy in code | enabling a capability becomes a row, not a release |
| Next.js | Streamlit / Gradio | production UX, and the course grades front-end work |
| Self-hosted Langfuse | LangSmith | EU-hostable, cheaper at eval volume, no vendor coupling |
| RLS + repo scoping | app-level checks only | the database should guarantee isolation, not a convention |

---

## 15. Known limits

1. Deterministic SEO scoring cannot see competition strength or backlinks — it gates drafts, it doesn't predict rankings.
2. AI share-of-voice is a sample, not a census: model answers are non-deterministic and shift with model updates, so comparability requires a fixed prompt set, a pinned model version, and repeat sampling.
3. Lead attribution is last-click via UTM; multi-touch journeys are undercounted.
4. `publish` supports WordPress first; every other CMS is an adapter yet to be written.
5. No cross-run research cache in v1 — the cheapest next cost win.
6. Single-region deployment; no DR beyond database backups.
7. English-first; other locales work but are unevaluated.
8. Content velocity is capped by design — this architecture will not mass-produce, and shouldn't.
