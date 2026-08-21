# Architecture — Production-Grade Growth Agent

Companion to [ROADMAP.md](ROADMAP.md) (what to build, in order) and [FEATURES.md](FEATURES.md) (what it does for a customer). **This document is the technical design.**

Everything lives inside `Social-Marketing-Agent/`. Nothing in the parent `TuringCollege/` folder is read, written, or depended on.

> **Diagrams:** every diagram for this system — system context, component kinds,
> the full request flow, the agent state machine, the tool-calling loop, agentic
> RAG, model routing, the actuator/idempotency sequence, lead attribution, the
> content spine, the data model, deployment, degradation, and the Track-B
> multi-agent topology — is in **[DIAGRAMS.md](DIAGRAMS.md)**.
>
> **How the agents work** — `AgentState`, all ten nodes with their model tier and
> tool allowlist, the node loop, prompt assembly, the three memory stores, the
> validation loop, caps, interrupt/resume, and a costed worked run — is in
> **[AGENT_RUNTIME.md](AGENT_RUNTIME.md)**.

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
| Examples | `crawl`, `seo`, `serp`, `geo`, `kb`, `nap`, `claims`, `landing`, `channel` | `notify.owner` (the transactional owner notice EXPORT sends — real behind `RESEND_API_KEY` + `OWNER_NOTICE_FROM`, and it REFUSES any marketing field, which is what stops it becoming the door marketing uses to skip the consent rule), `notify.email` (the marketing type, real behind `RESEND_API_KEY`; deliberately granted to no node), `social.post` (refuses without a platform connection), `publish.page` (real — this app serves the page, so there is no credential to be missing) | the nine graph nodes in `agents/nodes/` |
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
│  /developer (role-gated on the API): models · sliders · prompt        │
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
│  AGENT RUNTIME  LangGraph StateGraph; checkpoints in `runs.checkpoint` │
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
| Postgres | businesses, DNA, crawl snapshots, keywords, competitors, geo prompts/results, opportunities, content, social posts, leads, runs, actions, approvals, ledger, feedback, **run checkpoints** (`runs.checkpoint`, written by `RunService.checkpoint` after every node — NOT a LangGraph `BaseCheckpointSaver`; see §14) | one transactional store; a run and its cost land atomically |
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
| final quality review | mid | optional review pass — **MID, not strong**: a reviewer checks a draft against stated constraints, which is a judgement task rather than a generation one, and GENERATE stays the only STRONG consumer so the per-piece cost stays predictable. A test fails if a second task drifts onto STRONG. |
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

**Prompt injection is the top risk in this product**, because the agent reads attacker-controllable pages and can reach a publish Actuator. Three barriers — but they are **not** equally strong, and an earlier version of this section claimed "any one of which is sufficient", which was wrong:

1. **The data envelope.** Harvested text is wrapped in explicit markers with a system rule that content inside is data and never instructions. **This barrier was defeatable until it was tested.** `fence()` wrapped untrusted text without escaping the markers themselves, so any crawled page could emit the END marker and everything after it read as trusted — the text equivalent of closing a quote in SQL. Fixed by `prompts.escape_markers`. Note what remains true even now: this barrier asks a model to obey a rule, so it is a mitigation, not a guarantee.
2. **The per-node tool allowlist**, enforced in the runtime (`agents/tools.py`), not documented and hoped for. `HARVEST` and `GENERATE` cannot call an Actuator at all — only `EXPORT` can, and only with an approval token. Two mechanisms for two threats: *our code* asking for an ungranted tool raises (a wiring fault), while a *model's* ungranted call is dropped, logged and recorded in the run state — because an attacker must not be able to end a run by smuggling one sentence into a page.
3. **Human approval** before publish for any business whose policy isn't `auto`.

**Only barrier 2 and the regulated-claim gate hold whatever the model does**, because they are the only two that consult no model. That is asserted, not assumed: the injection corpus includes a router that FULLY complies with the injected page — it returns the `publish` call the page asked for — and the run still cannot publish. Barrier 1 depends on the model complying and barrier 3 depends on a human being attentive.

A 10-payload injection corpus is a test, not a checklist — ten distinct *mechanisms* (instruction override, forged role delimiters, fence escape, encoding, exfiltration, induced claim, induced tool call, hidden text, translate-and-follow, multi-turn), each naming what a failure would look like.

**Text extraction is not a security boundary, and this is measured rather than hoped.** `display:none`, `visibility:hidden` and HTML comments are dropped by the extractor, but **`font-size:0` and off-screen text survive into the page text, and instruction text in an `img alt` survives into the harvested facts.** There are tests asserting both the drops *and* the survivals, specifically so nobody can upgrade this into "hidden instructions never reach the model".

**The request layer.** Body size is capped on the declared `Content-Length` *and* on every chunk as it streams (`core/body_limit.py`) — a header-only limit is "please declare your own size", and the header can lie or be absent. It sits deliberately above `api/leads`' own 8 KiB cap so that endpoint's control still bites. CSRF is Origin/Referer validation (`core/csrf.py`), not double-submit: the frontend calls a *different* origin with `credentials: "include"`, so page script cannot read the API's cookie and the "double" half does not exist — and double-submit's own weak point, a sibling subdomain forging both halves, is exactly the gap `SameSite=Lax` leaves. It fires on cookie *presence*, so the anonymous public lead form (a cross-host write by design) is untouched.

**Other controls.** SSRF: HTTPS only, DNS-resolve and reject private/link-local/metadata ranges, 5 s timeout, 2 MB cap, `robots.txt` respected, redirect chain re-validated at every hop. Secrets: env-only, never in a browser bundle, provider keys never per-tenant in v1. AuthN: argon2 + HMAC session cookie, `httpOnly`, `sameSite=lax`, `__Host-` prefixed wherever `Secure` is possible (which makes the no-`domain` rule browser-enforced rather than convention), and **no `domain` attribute, ever**. Passwords are additionally checked against the HIBP corpus by k-anonymity — five hex characters of the SHA-1 on the wire, comparison local, network off unless configured, failing open on an outage. AuthZ: role checked server-side **on the API** — every route under `/api/v1/admin/*` carries `require_admin` (`api/admin_models.py`, `api/cost.py`), so what is gated is developer mode's DATA, not its shell: `/developer` renders for any signed-in account and shows the 403 as an explanatory card. That is deliberate — a second check in the frontend would put an authorisation decision somewhere that cannot enforce it, and would be a second thing to keep in step with the real one. Audit log: every Actuator call, approval, policy change, and settings edit, append-only. GDPR: EU-region providers with no-train terms, per-business export and delete, documents deletable with their embeddings, retention configurable.

---

## 10. Frontend architecture

`frontend/app/` is **flat** — one directory per screen. There is no route group, no
dynamic business segment, and no public form page. This block is the whole tree;
`ls frontend/app` should turn up nothing that is not in it.

```
app/
├─ layout.tsx                  <html>, pinned light theme, SessionBar. No auth, no redirect
├─ page.tsx                    owner's home: start a run · recent runs · latest leads
├─ login/                      sign in and sign up
├─ onboard/                    crawl a homepage → confirm the business DNA
├─ documents/                  upload, ingest status, reindex
├─ runs/                       the run list
│  └─ [runId]/                 timeline (SSE → polling), review tabs, approve/reject, export
├─ leads/                      inbox with attribution
├─ memory/                     the business preferences the next run's prompt will carry
├─ connections/                platform accounts: status, usability verdict, disconnect
├─ developer/                  layout + nav, then models · runtime · tools · cost
├─ globals.css                 tokens, the light/dark palettes, neumorphic surfaces
├─ components/                 shell, cards, run rows, safe HTML — not routes
└─ lib/                        one typed API client per surface — not routes
```

**Why there is no `businesses/[id]/…` segment.** The business is never in the URL: it
is resolved from the session on every read, and `GET /api/v1/leads` records why it must
be — FastAPI ignores an unknown query parameter silently, so a `businessId` that
"worked" would be a complete cross-tenant read that no test would notice. One owner,
one business, resolved server-side. An id in the path would be a second, weaker source
of truth for tenancy.

**The public lead form is not a page in this tree, and that is the design.** It is
server-rendered by the API: `GET /p/{piece_id}` returns a plain HTML document whose
`<form method="post">` submits to `POST /public/forms/{form_id}`, and the confirmation
is the same document re-rendered with `?sent=1`. Attribution reaches it through the
short-link service — `/l/{code}` (tracked redirect, click written after the 302) and
`/go/{slug}` (bio hub) — not through a Next route. So the "minimal bundle" a form
deserves is not a small bundle here, it is **no bundle at all**: no JavaScript, no
cookie, works with scripting off, cacheable by anything in between, and its markup
comes from the pure `engines/landing.py` renderer so the escaping is unit-tested rather
than reviewed by eye. `api/pages.py` and `api/leads.py` carry the full reasoning.

**Rules.** Every screen is a **client** component, and that is forced rather than
preferred: the API is a different origin (Next `:3100`, FastAPI `:8100`), the session
cookie is `HttpOnly` and host-only to the API, and the Origin-CSRF middleware (§9)
refuses a cookie-bearing request that arrives with no `Origin` header — which is
exactly what `fetch` from a server component sends. Two consequences, both admitted
rather than designed around: authenticated data is never server-rendered, so every
screen carries a real loading state and "the API is unreachable" is a designed state on
all of them; and there are **no server actions** anywhere in this tree. A destructive or
publishing action is therefore a browser `fetch` that confirms inline first (`memory/`,
`connections/`) rather than through `window.confirm`, which cannot be styled and reads
as a browser error. The run timeline streams over `EventSource`, resumes from the last
sequence number it saw, and falls back to a 2 s poll when SSE is unavailable or errors —
a timeline that silently stops updating looks like an agent that silently stopped.
WCAG AA: focus states, contrast, keyboard paths, and `aria-live` on anything that
arrives from a poll rather than from a click.

**Guard placement: there is no route guard in this frontend at all.** No `middleware.ts`
exists, no layout redirects, and no page checks a role. The gate is the API — every
authenticated route carries its own dependency (`CurrentUser`, or `require_admin` on
everything under `/api/v1/admin/*`) — and each screen renders the refusal it gets back.
This follows from the paragraph above rather than being a separate preference: the
session cookie is `HttpOnly` and was set by another origin, so nothing in the Next app —
middleware, layout or page — can read it. A guard would have to ask the API whether the
caller is signed in, which is what every screen already does on its first render.
Duplicating the check would put an authorisation decision somewhere that cannot enforce
it, and leave two things to keep in step with each other.

How a refusal reads differs by surface, and one half is better than the other. The
`/developer` screens share an `ErrorCard`: 401 becomes "Sign in required" with a link to
`/login?next={path}`, and 403 becomes a sentence **that screen** supplies, because the
API's `code` is the load-bearing part of a 403 and the prose is the screen's to get
right. The owner-facing screens render the API's own message in their error panel and
offer no sign-in link of their own; the root layout's `SessionBar` is the way out, and it
renders nothing for a visitor with no session — so a signed-out visitor landing on
`/leads` reads a refusal without being handed the door. That is a real gap, recorded
here rather than described as a decision. What the API-only gate does cost by design:
chrome renders before the refusal arrives instead of a redirect firing before render,
and `/developer` is not hidden from a non-admin's navigation — it answers "Not available
on this account". On an internal console that is the right trade for having exactly one
authority on authorisation.

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
| LangGraph `StateGraph` for the runtime, with the hand-written driver kept behind `agent_runtime` | bare ReAct loop; also: keeping the hand-written driver as the only runtime | Resumability, a defined human interrupt, bounded steps, evaluable nodes — the shape was always this, and until `862e7e9` the LIBRARY was a declared dependency nothing imported, so "LangGraph state machine" was a claim about the shape rather than the code. The nodes needed no change (a node was already `async (AgentState) -> dict`, which is LangGraph's own signature); what the library now owns is the conditional edges, the resume entry and the interrupt. The old driver is selectable by configuration rather than deleted, so the swap is one environment variable away at 3am — and `tests/agents/test_graph.py` runs every branch of the machine against BOTH, because a fallback that is not equivalent is not a fallback. Delete it if it goes a release untouched. |
| Run checkpoints in our own `runs.checkpoint` column, not a LangGraph checkpointer | `BaseCheckpointSaver` over Postgres | The column is what the review screen, the timeline and the resume path already read, and it is written after every node. A second durable store for the same state would be two sources of truth for "where is this run" — the exact class of bug this document spends §14 avoiding. LangGraph gets an `InMemorySaver` per invocation, because `interrupt_before` needs somewhere to pause; it lives for one `ainvoke` and holds nothing anyone else reads. |
| `nap.audit` reads the business's OWN pages (`LocalBusiness` JSON-LD + Impressum) | scraping Gelbe Seiten / Das Örtliche / a Google Business Profile; a paid aggregator | The audit shipped in Phase 4 and the agent never called it, because nothing produced listings. Directory scraping at scale is a terms-of-service and blocking problem and no paid geo/aggregator spend is budgeted, so the honest scope is SELF-consistency: a business's structured data and its Impressum disagree often, that disagreement is checkable and actionable today, and it is the same input Google's entity resolution uses. The payload carries its own scope sentence so a score cannot be read as "your address is consistent online". |
| `web_search` in GENERATE as a bounded two-round tool loop | an open ReAct loop inside the node; or leaving the grant unimplemented | A model part-way through a draft can notice a fact is missing and check it rather than guessing, which is the one place live search earns its cost. Bounded because a model offered a third search will take it and the node needs a page, not a bibliography; the output tool is offered FIRST because the deterministic `FakeProvider` answers with `tools[0]`, which is what keeps the hermetic suite on the normal path. |
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
| Fallback on 403/404 (`ModelUnavailableError`) | treating every non-429 4xx as fatal | a 403/404 is scoped to one *model*, and a chain entry is a (provider, model) pair, so the next entry may be fine. Found live: an OpenRouter account whose data-policy guardrails served `claude-haiku-4.5` and refused `claude-opus-4.8` with a 404, which the router read as a total outage. 401/402 stay fatal — they are credential-scoped, so falling through buries "bad key" under N identical failures |
| `--live` loads `.env` then asserts a real provider | trusting the flag | the flag printed "This spends money" while the router silently served `FakeProvider`, because the runner deliberately never loaded `.env`. A report that *looks* live is worse than no report, so `--live` now exits non-zero rather than measure canned responses |
| Hashtag caps enforced in code (`engines/channel`) | asking the model to obey a count | measured on `gpt-4.1-mini`: the bare instruction `Keine Hashtags` returned 21 of them. Same rule the codebase already applies to length — counting is arithmetic, so Python does it. Limits arrive as arguments, not from a table, because two channel-limit tables already disagree and a third would be worse |
| A `#` inside an identifier is not a hashtag | one regex for all `#` | chunk ids are `<case_id>#<ordinal>`, so `[chunk:plumber-01#0]` was counted as a hashtag by the rubric AND stripped by the formatter. The first penalised the RAG arm *for citing*; the second turned real citations into fabricated ones. Both parsers now exclude citation spans exactly as they already excluded URLs |
| CSRF by `Origin`/`Referer` validation against the CORS allowlist | double-submit cookie token | decided by how this frontend authenticates, not by taste. Next.js on `:3100` calls the API on `:8100` with `credentials: "include"`, and `document.cookie` is per-origin — so page script cannot read a CSRF cookie the API set, and the "double" half of double-submit does not exist here; what people build instead is a synchronizer token with a bootstrap endpoint, a refresh path and a "token invalid, please reload" state. Double-submit's own weak point is also the exact gap being closed: a same-site sibling subdomain can set a cookie on the parent domain and forge both halves, and a sibling subdomain is precisely what `SameSite=Lax` does not stop. Origin validation refuses it directly, reuses the allowlist CORS already has (one list, so they cannot drift), needs no frontend change, and cannot be spoofed by the attacking page because `Origin` is a forbidden header name. Applied only to state-changing requests that CARRY the session cookie: with no ambient credential there is nothing to forge, and checking anyway would break the public lead form, whose whole purpose is to be posted from a landing page on another host. Residual, recorded rather than implied: login CSRF is not closed (a pre-login request has no cookie), and `SameSite=Lax` is what blocks it |
| `__Host-` session cookie, name conditional on `Secure` | one unconditional prefixed name · no prefix at all | the prefix has the BROWSER enforce the no-`Domain` rule this codebase previously enforced by convention plus a test: a `__Host-` cookie carrying a `Domain` is not a weaker cookie, it is one the browser refuses to store, and a sibling subdomain cannot overwrite it either. But `__Host-` requires `Secure`, and `Secure` is deliberately off for local (plain HTTP on localhost never sends it), so an unconditional prefix breaks local login. The name is therefore environment-dependent, which is a real cost — a hardcoded `"sma_session"` now works on a laptop and silently fails in production — paid down by there being exactly one resolver (`core.cookies.session_cookie_name`), no name constant anywhere, and a test suite that resolves the name rather than repeating it |
| Body ceiling enforced at `Content-Length` AND while streaming | trusting `Content-Length` | the header is caller-supplied, and a chunked request declares nothing at all, so a header-only check is a request that attackers be honest. Both halves are needed for a different reason too: FastAPI wraps its body read in a bare `except Exception` and reports `400 There was an error parsing the body`, so the streaming refusal has to be re-asserted on the send side or an oversized upload is bounded correctly while being explained wrongly. A test endpoint that reads `request.body()` itself does not reproduce that — it took a probe against the real app |
| BOTH judged arms: DeepEval in-process, Ragas out-of-process | picking one; or running Ragas in this venv | not taste, a version wall. `ragas>=0.4.3` depends on `instructor`, which caps `openai<3.0.0`, and this project pins `openai>=3.2.0` deliberately — v3 is built on httpx2, which is why `httpx2` is a declared dev dependency. Downgrading the SDK to fit an eval library would change the transport under the shipped provider adapter. `ragas==0.3.1` escapes that pin but imports `langchain_community.chat_models.vertexai`, which no longer exists in langchain-community 1.x, so it fails at import. DeepEval 4.1.8 installs and imports cleanly against our pins, which is why it is the IN-PROCESS arm. Ragas out-of-process was then checked and does work — an isolated venv with `ragas==0.4.3` + `openai==2.54.0` + `langchain-community<0.4` imports its metrics — so it ships as a SUBPROCESS arm (`evals/ragas_arm.py` -> `.venv-ragas`, `make ragas-env`), and DeepEval stays as the in-process one whose judge goes through `ModelRouter`. Running both is the interesting case: two judges reading the same text, where the gap between them measures the judges. The subprocess costs the per-call budget guard and the fallback chain, which the report prints rather than hides; it keeps the resolved model id (so no model is hardcoded at a call site) and the token usage (so the spend is priced with our own table). One thing the boundary forced: Ragas returns `faithfulness = 1.00` for an EMPTY retrieval context -- nothing there to contradict -- so the arm discards that score on the way back, for the same reason `no_answer` is excluded from the share-of-voice denominator |
| The eval judge routes through `ModelRouter` (`DeepEvalBaseLLM` adapter) | DeepEval's own OpenAI client | the default reads `OPENAI_API_KEY` and calls a hardcoded model, which would be a *second* path to a paid model with no routing table, no pre-call budget guard, no cost ledger, and no "a missing credential means the FAKE provider" rule. The adapter asks for `TaskClass.REVIEW` like any other caller. Cost: our provider contract has no structured-output parameter, so the requested JSON schema is appended to the prompt and DeepEval parses the text with its own lenient loader — a reply it cannot parse is reported as *not measured*, never as 0.00 |
| The judged arm is opt-in (`--deepeval`) and excluded from the mean | judging on every run · folding the judged scores into the aggregate | an LLM judge costs ~7 mid-tier calls per case-arm, so it inherits `--live`'s posture exactly: off by default, and the report names the state that produced its numbers. It stays out of the deterministic mean because a model's opinion must not be able to move an arithmetic average — and because it is not reproducible run to run, which is the property the rest of the rubric is chosen for |
| The citation instruction ends the generation prompt | repeating format rules after it | measured both ways: rules last gave format 1.00 / grounding 0.02, passages+citation last gave the reverse. Whatever ends the message is what the model obeys, so the instruction that wins must be the one no code can enforce afterwards — a hashtag count can be fixed downstream, a missing citation cannot |

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
