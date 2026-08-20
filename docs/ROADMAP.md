# Social Marketing Agent — Roadmap to a Production-Grade Growth Agent

**Version 2** — restructured around the Engine/Agent split and the three-surface visibility → lead goal.

**Purpose:** Take a business (its website, plus any documents it gives us) and produce the content and instrumentation that wins visibility on **Google**, in **AI answer engines** (ChatGPT / Perplexity / Google AI answers), and on **social media** — with a lead-capture and attribution loop so the output is measured in leads, not vibes.

**Containment rule.** Everything lives inside `Social-Marketing-Agent/` — own `pyproject.toml`, `uv.lock`, `.venv`, `.env`, git history. Never read from, write to, or depend on the parent `TuringCollege/` folder or its sibling projects. Run every command from inside this folder.

---

## 1. What the product actually is

```
   Business + its documents
              │
              ▼
   ┌──────────────────────┐
   │  UNDERSTAND          │  crawl site, ingest docs, classify business,
   │  (engines)           │  extract services/audience/entities → Business DNA
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  FIND THE GAP        │  keyword + SERP + competitor + AI-answer gaps
   │  (engines → agent)   │  → ranked opportunity list
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  PRODUCE             │  blog/landing page (Google) ·
   │  (agents)            │  answer-shaped content (AI engines) ·
   │                      │  4 social posts (social) · CTA + form
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  VALIDATE → APPROVE  │  deterministic scoring, brand rules, then a human
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  PUBLISH             │  CMS draft, social export/schedule, JSON-LD, sitemap ping
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  MEASURE             │  GSC impressions/position · AI share-of-voice ·
   │  (engines)           │  social engagement · form leads → attribution
   └──────────┬───────────┘
              └────────────► next opportunity (the loop is the product)
```

**Three output surfaces, one pipeline.** Same research, three renderings:

| Surface | What wins there | What we generate | Measured by |
|---|---|---|---|
| **Google** | crawlable depth, intent match, entities, internal links, schema | long-form article + service/landing page | GSC impressions, avg position, indexed count |
| **AI answer engines** | quotable, self-contained, factual claims; structured Q→A; being cited | answer-shaped blocks, FAQ + `FAQPage`/`Article` JSON-LD, comparison tables, stats with sources | **AI share-of-voice**: % of a fixed prompt set where the brand is mentioned or cited |
| **Social** | hook, native format, per-platform rules | LinkedIn / X thread / Instagram / Facebook, each pointing at the CTA | clicks + form leads by `utm_source` |

**Target users.** (a) SMB owner with a website and no marketing team — primary. (b) Solo in-house marketer. (c) Small agency running several clients — the reason multi-business exists from day one.

**Why an agent and not a prompt.** The task needs live external state (SERP, competitor pages, the brand's own documents, AI-engine answers, GSC data), deterministic measurement, and a plan whose next step depends on what the research returned. A prompt can't fetch, measure, or re-plan.

---

## 2. Honest constraints — read before promising anything

These shape the product; ignoring them produces a demo that fails.

1. **Google traffic is not demoable.** Indexation: days. Ranking movement: 3–6 months. **So we never promise traffic.** The product's Google KPI is leading indicators — indexed pages, impressions, average position, keyword coverage. Say this in your README and your review.
2. **AI visibility is the wedge because it moves in days.** No official citation API exists. We measure by *probing*: a fixed prompt set (30–50 prompts per business, e.g. "best plumber in Koblenz", "X vs Y", "how much does Z cost") run against 2–3 models, parsed for brand mention + domain citation. This gives a real before/after inside one demo, and it's the metric incumbents don't own.
3. **Leads need a surface.** Content → CTA → landing page with a form → `lead` event with UTM + content id → attribution. Without the form there is no lead, only hope. The form is in scope; a full CRM is not.
4. **AI-generated content at scale is a real risk.** Google's spam policy targets scaled content abuse, not AI per se. Mitigation is built in, not bolted on: every factual claim grounded in a cited source or the business's own documents, deterministic quality gate, human approval before publish, and a volume cap per business per week. This is a design requirement, not a disclaimer.
5. **Scraping has terms and rate limits.** SERP and social data come from a provider (Tavily/Brave/SerpAPI, or Apify actors), not a homemade scraper hammering Google. `robots.txt` respected on competitor crawls, private IP ranges blocked, results cached.
6. **The moat is not the LLM.** Anyone can call a model. The defensible parts are: the business knowledge graph, the deterministic engines, the eval harness, and the measured loop. Build those.

---

## 3. Engines vs Agents — the core architectural rule

> **If the answer is computable, compute it. Only ask a model to decide, interpret, or write.**

| | **Engines** (deterministic Python) | **Agents** (LLM) |
|---|---|---|
| Do | crawl, parse, score, count, fetch APIs, validate, format, publish, diff, track | plan, prioritise, interpret, decide which tool, write prose |
| Properties | testable, cheap, repeatable, zero hallucination | fuzzy, costed, needs evals |
| Failure | exception → typed error | wrong choice → caught by validation |

**Engines (8).** Each is a plain Python package with a typed input/output contract and unit tests. No LLM inside.

| Engine | Responsibility |
|---|---|
| `crawl` | site + competitor crawl: status, canonical, title/meta, heading tree, internal links, images/alt, sitemap, robots, existing schema |
| `kb` | document ingestion (PDF/DOCX/MD/TXT/URL) → chunk → embed → pgvector retrieval; the business's own facts |
| `seo` | on-page scoring 0–100, keyword density, readability, entity coverage, internal-link opportunities, JSON-LD generation + validation |
| `serp` | keyword expansion, SERP snapshot, competitor set, rank tracking over time |
| `geo` | **AI answer-engine visibility**: run the prompt set against N models, parse mention/citation, compute share-of-voice, diff vs last run |
| `social` | per-platform validators (length, hashtag count, link rules), UTM builder, export/schedule payloads |
| `analytics` | GSC + GA4 pull, lead events, content→lead attribution |
| `publish` | CMS adapters (WordPress REST first), idempotent, draft-by-default |

**Agents (5).** Thin. They receive structured engine output and decide.

| Agent | Decides |
|---|---|
| `growth_manager` | given the current state and metrics, what is the highest-value next action |
| `research` | which competitors/keywords/prompts matter; interprets the gap |
| `content` | outline → article/landing page, grounded in `kb` + cited sources |
| `social` | channel-native repack from one source article |
| `geo` | designs the prompt set; interprets why the brand is absent from an AI answer and what content would fix it |

**Example of the split, to use verbatim in your review:**

```
growth_manager: "What's the biggest opportunity?"
      ↓ (no LLM below this line)
crawl.site()        → 43 pages, 6 missing meta, 12 orphaned
serp.expand()       → 210 keywords, 38 with intent match, 9 winnable
geo.probe()         → brand cited in 3/40 AI answers (7.5% SoV)
kb.search()         → 4 service docs, 2 case studies
analytics.gsc()     → 1,200 impressions, avg position 34
      ↓ structured dataset
growth_manager: "Write the 'emergency X in Koblenz' answer page —
                 9 winnable keywords converge on it and AI answers
                 currently cite two competitors we can outdo on specificity."
```

---

## 4. Production seams — build these on day one, they cannot be retrofitted

This is my answer to "build the whole Agent OS first": don't build the *depth*, but do build these **seams**. Each is hours-to-a-day and impossible to add later without a rewrite.

| Seam | Implementation | Why now |
|---|---|---|
| **Resumable run state** | `runs.checkpoint` (JSONB), written by `RunService.checkpoint` after every node; `runs` carries `state`, `current_node`, `resumed_count`, `finished_reason`. **NOT** a LangGraph Postgres checkpointer — see `ARCHITECTURE.md` §14: the column is what the review screen, the timeline and the resume path already read, and a second durable store for the same state would be two answers to "where is this run" | Worker dies at step 4 → resume at step 4, not step 0 |
| **Idempotency** | `idempotency_key = business_id:workflow_id:task_id:action_type`, unique index on `actions`; check-before-execute, return prior result | Publish must never happen twice |
| **Approval policy table** | `action_type → auto | notify | approve | human`, data not code | Turning a capability on later is a row, not a release |
| **Budget + step caps** | per-run USD and step ceiling, checked *before* each model call; per-business monthly cap | Autonomous loops burn money without this |
| **Model router** | task class → tier, with declared fallback chain; never a hardcoded model name | Swapping models must not touch agent code |
| **Trace + cost ledger** | every LLM/tool call: run_id, model, tokens, USD, latency, prompt version | You cannot debug or evaluate what you didn't record |
| **Engine boundary** | engines are importable packages with typed contracts and no LLM import | The whole architecture depends on this staying clean |
| **Tenant scoping** | every row carries `business_id`; a test asserts cross-business reads return nothing | Multi-business is a day-one property, not a migration |

Everything else — the remaining intelligence engines, autonomous scheduling, agency multi-seat, GBP/reviews, ads — is **configuration or a new engine behind an existing seam**. That's the whole point.

---

## 5. Stack

Same libraries you've used before, installed fresh here — nothing inherited from the parent folder.

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.13 · FastAPI · Pydantic v2 | your stack; LangGraph is Python-first |
| Agent runtime | **LangGraph** `StateGraph` | explicit state machine and a human interrupt at a defined point, both now the library's rather than a hand-written driver's (`862e7e9`). Checkpointing is deliberately OURS, in `runs.checkpoint` — see `ARCHITECTURE.md` §14 |
| LLM access | **OpenRouter** (OpenAI-compatible SDK) | one integration = multi-model, per-task cost routing, fallbacks, no lock-in |
| DB | Postgres 16 + **pgvector** · SQLAlchemy · Alembic | app data + RAG in one store |
| Frontend | **Next.js 16 · React 19 · Tailwind 4 · shadcn/ui** | grading criterion is "uses a front-end library"; Streamlit forfeits it |
| Observability | **Langfuse** (docker or free cloud) | self-hostable, EU, cheap at eval volume |
| Eval | **Ragas** + deterministic rubric | faithfulness/relevancy + SEO/brand/format compliance |
| Jobs | FastAPI `BackgroundTasks` + SSE, then **ARQ/Redis** once a run exceeds ~60 s | don't buy a queue before durability is needed |
| Data providers | Tavily *or* Brave (search) · optional **Apify actors** for SERP/social/Maps | buy the scraping, don't build it |
| Tests | pytest · pytest-asyncio · `respx` | every external call faked; no live API calls in CI, ever |

### Repo layout

```
Social-Marketing-Agent/          ← repo root; nothing above this line is ours
├─ pyproject.toml  uv.lock       own dependency set (uv)
├─ .venv/  .env  .env.example  .gitignore  README.md  ROADMAP.md
├─ docs/            ARCHITECTURE.md ROADMAP.md FEATURES.md BUILD_ORDER.md
│                   CHANNELS.md FREE_CHANNELS.md DIAGRAMS.md CRITERIA_MAP.md
│                   AGENT_RUNTIME.md   (PROBLEM.md is at the repo root)
├─ backend/app/
│  ├─ main.py                    FastAPI app, routers, SSE
│  ├─ core/                      config, security, rate_limit, budget, idempotency, errors
│  ├─ engines/                   ← deterministic, NO llm import (enforced by a test)
│  │  ├─ crawl/ kb/ seo/ serp/ geo/ social/ analytics/ publish/
│  ├─ agents/
│  │  ├─ graph.py                LangGraph state machine
│  │  ├─ state.py                typed AgentState
│  │  ├─ nodes/                  intake research plan generate validate repack review export
│  │  └─ prompts/                versioned .md templates (v1, v2 …)
│  ├─ tools/registry.py          name → JSON schema → callable, per-business toggles
│  ├─ models/                    model_router.py, pricing.py
│  ├─ knowledge/                 business_dna.py, retriever.py
│  ├─ db/                        models, session, migrations
│  └─ api/                       auth businesses documents runs opportunities
│                                content approvals metrics feedback settings
├─ frontend/                     Next.js (user mode + /developer)
├─ evals/                        datasets/, run_ragas.py, rubric.py, geo_eval.py, report.md
└─ docker-compose.yml            postgres · redis · langfuse · api · web
```

---

## 6. Data model (the tables that matter)

```
businesses          id, owner_id, name, industry, locale, website, dna(jsonb), created_at
documents           id, business_id, filename, kind, status, chunk_count
kb_chunks           id, business_id, document_id, content, embedding vector(1536), meta
crawl_pages         id, business_id, url, status, title, meta, h_tree(jsonb), links(jsonb), schema(jsonb), crawled_at
keywords            id, business_id, term, intent, volume, difficulty, current_rank, checked_at
competitors         id, business_id, domain, discovered_via, notes
geo_prompts         id, business_id, prompt, category, active
geo_results         id, business_id, geo_prompt_id, model, mentioned bool, cited bool, answer_excerpt, run_at
opportunities       id, business_id, kind, title, rationale, target_keywords[], expected_impact, effort, score, status
content_pieces      id, business_id, opportunity_id, surface, title, slug, body_md, meta(jsonb),
                    seo_score, status(draft|approved|published), published_url, published_at
social_posts        id, content_piece_id, platform, body, hashtags[], utm, status, scheduled_at
leads               id, business_id, content_piece_id, source, utm(jsonb), fields(jsonb), created_at
runs                id, business_id, goal, state, plan(jsonb), completed(jsonb), pending(jsonb),
                    budget_usd, used_usd, error, resumed_count
actions             id, run_id, business_id, action_type, idempotency_key UNIQUE, status, result(jsonb)
approvals           id, action_id, policy, decided_by, decision, reason, decided_at
model_usage         id, run_id, node, model, tokens_in, tokens_out, usd, latency_ms, prompt_version
feedback            id, content_piece_id, user_id, rating, axes(jsonb), reject_reason
learned_style       id, business_id, rules(jsonb), derived_from(jsonb), approved_at
```

Every business-scoped table carries `business_id`; a test asserts a second user reads zero rows.

---

## 7. The agent graph

`AgentState`: `business_dna`, `goal`, `engine_facts`, `opportunity`, `outline`, `draft`, `seo_report`, `social_posts[]`, `geo_plan`, `costs`, `errors[]`, `approval`.

| Node | Kind | Does | Fails how |
|---|---|---|---|
| `INTAKE` | engine + LLM | load DNA, docs status, normalise request, pick surface(s) | DNA missing → ask, never guess |
| `HARVEST` | **engines only** | `crawl`, `kb`, `serp`, `geo.probe`, `analytics.gsc` in parallel | partial failure → continue, record in `errors[]`, UI says which data is missing |
| `OPPORTUNITY` | LLM (mid) | rank gaps into scored opportunities; pick one | none found → return the audit findings instead |
| `PLAN` | LLM (mid) | outline: H-tree, target + secondary keywords, intent, answer blocks for AI engines, internal-link slots, CTA | reject outline with no target keyword |
| `GENERATE` | LLM (strong) | article/landing page section by section, every claim traced to `kb` or a cited source | section retry ×2 → shorter piece |
| `VALIDATE` | **engines only** | `seo.score()` ≥ 85: title 50–60, meta 140–160, density 0.8–2.5%, Flesch ≥ 55, valid H-tree, ≥1 internal + ≥2 external links, alt text, JSON-LD valid; `kb.check_claims()`; brand-rule check against `dna.avoid` | < 85 → back to `GENERATE` with itemised failures, **max 2 loops** |
| `REPACK` | LLM (cheap) + engine | 4 social posts; `social` engine enforces limits and builds UTMs | over-length → deterministic trim + single-platform regen |
| `REVIEW` | `interrupt()` | human approves / edits / rejects with reason | reject reason → feedback loop |
| `EXPORT` | engine | MD/HTML/JSON-LD download; `publish.wordpress()` **draft only**, idempotent; social export | requires approval token |
| `MEASURE` | engine (scheduled) | re-probe `geo`, pull GSC, collect leads, attribute | provider down → skip cycle, don't corrupt series |

**Caps:** 14 node executions and $0.50 per run. Exceeding either ends the run with a partial result and a stated reason — never an infinite loop.

**Review talking points.** *Agent types:* prompt-chain < router < tool-calling agent < multi-agent supervisor; this is a state machine with tool-calling nodes — more controllable than ReAct-until-done, cheaper to evaluate, resumable. Single agent with many tools deliberately; split only when expertise, permissions, context, model tier or eval criteria genuinely diverge. *Function calling:* Pydantic → `model_json_schema()` → model returns `tool_call` → registry validates arguments **before** execution → result appended as tool message → model re-plans; invalid args get one repair turn with the validation error, then the tool is skipped. *Prompt vs RAG vs agent:* prompts for style/format, RAG for the business's own facts (kills hallucinated claims), agent for live external state and adaptive planning — and name where each is used in your app.

---

## 8. Phases

Timeboxes are part-time days. Each phase ends green: ruff + mypy + pytest pass and `docker compose up` works from this folder.

### Track A — the shippable slice (course deliverable)

> **SUPERSEDED by [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md)** (re-sequenced 2026-08-18 for demo-visibility-first delivery: a working UI from day two, memory and agentic RAG promoted, GSC/GA4 analytics cut from the course build). The table below is kept for history — the *scope* is still accurate, the *order* is not. Build from `BUILD_ORDER.md`.

| # | Phase | Days | Output / DoD |
|---|---|---|---|
| **0** | Foundations | 0.5 | `uv init` here, compose (postgres/redis), FastAPI health, Next.js shell, ruff/mypy/pytest, GitHub Actions. **DoD:** `git status` at TuringCollege level shows changes only under `Social-Marketing-Agent/` |
| **1** | Business DNA + documents | 1.5 | `crawl` + `kb` engines. Paste URL → crawl + one extraction call → prefilled DNA form the user confirms. Upload PDF/DOCX → chunk → embed. **DoD:** real business onboarded < 90 s; retriever returns relevant chunks for 5 test queries |
| **2** | Seams: router, ledger, idempotency, budget | 1 | OpenRouter client, task→tier routing with fallback, `model_usage` writes, `actions` idempotency table, per-run/per-business caps. **DoD:** a run shows per-node cost; replaying an action returns the prior result instead of re-executing |
| **3** | `seo` + `serp` engines | 2 | Deterministic scorer (0–100, itemised), JSON-LD builder + validator, keyword expansion, SERP snapshot, competitor discovery. **DoD:** engine tests for success/timeout/malformed; **a test asserts `engines/` imports no LLM module** |
| **4** | `geo` engine — AI visibility | 1.5 | Prompt-set model, probe 2–3 models via OpenRouter, parse mention/citation, share-of-voice score, run-over-run diff. **DoD:** produces a real SoV number for a real brand and a chart of two runs |
| **5** | The graph | 2.5 | All ten nodes, our own checkpoint column, the runtime's `interrupt_before`, caps, SSE stream. **DoD:** one run → article ≥ 85 + 4 social posts + AI-answer blocks, streamed, resumable after approval |
| **6** | Lead loop | 1.5 | CTA + landing-page generation, hosted form endpoint, `leads` table, UTM builder, content→lead attribution view. **DoD:** submit a test form → lead appears attributed to the content piece that produced the link |
| **7** | UI | 2 | **User mode:** onboarding → documents → opportunities → run timeline (nodes, tool calls, live cost) → draft / SEO score / social / AI-answer tabs → edit → approve → export. **Developer mode** `/developer`, role-gated server-side: model picker, temperature & max-tokens sliders, prompt-version selector, tool toggles, raw trace. Brand voice (professional/friendly/concise) sits in *user* mode — it's a brand decision, not an LLM knob. Empty states, skeletons, retryable error toasts, WCAG AA. **DoD:** a non-technical person completes onboarding → approved content unaided |
| **8** | Auth, memory, feedback | 1.5 | JWT cookie + argon2; all data scoped per user+business with a cross-tenant test; short-term = thread checkpoint, long-term = DNA + `learned_style`; thumbs + 4-axis rubric + reject reason. **DoD:** two users cannot see each other's businesses (asserted) |
| **9** | Security hardening | 1 | Prompt-injection: fetched/document text wrapped in a data envelope with an explicit instruction-hierarchy rule; no tool call may be triggered by fetched content; `publish` never reachable from crawled text. SSRF allowlist (HTTPS only, DNS-resolve and block private/link-local, 5 s timeout, 2 MB cap, robots respected). Rate limits per user and IP. Output guard against regulated claims via `dna.avoid`. Secrets env-only, never in the browser. PII scan on uploads. **DoD:** 10-payload injection corpus, all fail to change behaviour, proven by a test |
| **10** | Observability + evaluation | 2 | Langfuse on every LLM/tool call with run_id, prompt version, cost, and user feedback attached as a score. Eval set: 20 business/topic cases. **Ragas** faithfulness + relevancy on grounded sections; deterministic rubric for SEO score, brand violations, format compliance; **GEO eval** = SoV delta. `evals/report.md` compares prompt v1 vs v2 and cheap vs strong model. **DoD:** `python evals/run.py` emits defensible numbers |
| **11** | Agentic RAG + learning loop | 2 | Retrieval becomes a tool the agent chooses, with query rewriting → relevance grading → fallback to `web_search` when graded irrelevant (the grade→re-retrieve→fallback cycle is what makes it agentic). Top-rated approved pieces become retrieved few-shot exemplars; recurring reject reasons distil weekly into proposed `dna` brand rules the user approves — never silently mutated. **DoD:** demonstrate a rejected style issue that stops recurring |
| **12** | Docs + demo | 1 | README (what/why/who, 5-min quickstart, screenshots, architecture diagram, cost table), `DECISIONS.md` (every choice + the rejected alternative), `EVALUATION.md`, `DEMO.md` (6-min script), in-app help assistant answering "how do I…" from the docs via the same RAG stack |

**Track A total ≈ 20 working days part-time.**

**Cut order if time runs short** (each cut is safe, in this order): Phase 11 → Ragas half of 10 (keep Langfuse) → Phase 6 lead loop (keep the CTA and UTMs, drop the hosted form) → Phase 4 down to one model instead of three. **Never cut:** 0–3, 5, 7, 9. Phases 0–5 + 7 alone satisfy every *required* grading criterion; everything past that is bonus points.

### Track B — the product path (after the course)

| Stage | Adds | Nature |
|---|---|---|
| B1 | GSC + GA4 OAuth, real rank tracking over time, weekly report | new engine behind existing seams |
| B2 | **Opportunity engine on a schedule** — nightly diff of rankings, AI SoV, competitor content → ranked action list → autonomous execution of `auto`-policy actions only | ARQ workers + the approval policy table already built |
| B3 | Local SEO: Google Business Profile posts, location pages, `LocalBusiness` schema, review monitoring + drafted responses | new engines + capabilities in config |
| B4 | Industry playbooks (`verticals/{plumber,dentist,restaurant,saas}/playbook.json`: goals, entities, content types, skills, KPIs) then **generated** playbooks from onboarding | pure configuration — no agent code |
| B5 | Agency multi-seat: orgs, RBAC, white-label, per-client budgets, audit log | schema extension |
| B6 | Multi-agent supervisor split (Research / Content / GEO / Local) once expertise, permissions or eval criteria genuinely diverge | refactor, not rewrite |

**Explicitly not building, ever, in this codebase:** a backlink network (that is AutoSEO's moat and a link-scheme risk), paid-ads spend automation, and autonomous publishing without an approval policy.

---

## 9. Optional-task coverage

Requirement for max points: ≥ 2 medium + 1 hard. This plan delivers **6 medium and 3 hard** (plus a 4th).

| Task | Phase |
|---|---|
| Easy 2 personality · 3 model choice · 4 LLM settings · 5 help assistant | 7 · 2 · 7 · 12 |
| Medium 1 cost/tokens · 2 memory · 3 external API tool · 4 auth · 5 feedback · 6 five+ tools with toggles · 7 multi-model · 8 security guard | 2 · 8 · 3 · 8 · 8 · 3+7 · 2 · 9 |
| Hard 1 agentic RAG · 2 observability · 3 eval report · 4 learns from feedback | 11 · 10 · 10 · 11 |

---

## 10. Error scenarios handled explicitly

| Scenario | Handling |
|---|---|
| LLM returns malformed tool args | schema-validate, one repair turn with the error, then skip the tool and record it |
| Provider 429 / 5xx / timeout | backoff ×3 → OpenRouter fallback model → fail the node, not the run |
| Search or SERP quota exhausted | degrade to `kb` only; UI banner "generated without live research" |
| Competitor page is JS-only / paywalled / empty | skip the source, continue; **never fabricate a citation** |
| SEO score stuck < 85 after 2 loops | return draft with an explicit "needs human edit" list |
| Injected instruction in a crawled page or uploaded PDF | data envelope + instruction hierarchy + per-node tool allowlist |
| Worker crash mid-run | checkpointed state; resume at the failed node; `resumed_count` recorded |
| Publish retried after an unrecorded success | idempotency key returns the prior result — no double publish |
| Two concurrent runs on one business | per-business run lock |
| Cost or step cap hit | terminate with partial output and a stated reason |
| AI model refuses or returns an empty answer during `geo` probe | record as `no_answer`, exclude from the SoV denominator, never count as absence |
| Onboarding site is empty or nonsense | refuse to guess; ask the user to complete the DNA form |
| Document upload with no extractable text (scanned PDF) | flag it, offer OCR, don't silently index nothing |

---

## 11. Weaknesses to state yourself in the review

Volunteering these scores higher than being caught by them.

1. Deterministic SEO scoring approximates ranking factors; it cannot see competition strength or backlinks. It's a drafting gate, not a ranking guarantee.
2. AI share-of-voice is a **sample**, not a census: model answers are non-deterministic and change with model updates, so the metric needs a fixed prompt set, a pinned model version, and repeated sampling to be comparable over time.
3. Lead attribution is last-click via UTM — it undercounts multi-touch journeys.
4. Eval set of 20 cases is small; cross-model variance isn't statistically strong.
5. `publish` supports WordPress only.
6. No research cache across runs yet — prompt caching plus a SERP cache is the cheapest next cost win.
7. English-first; multilingual is untested beyond German.
8. Content velocity is deliberately capped; this product cannot and should not do mass-produced content.

---

## 12. Immediate next actions

0. All work stays in `Social-Marketing-Agent/` (§ containment rule).
1. Write `docs/PROBLEM.md` — one page, before any code, using §1 and §2.
2. Get keys: OpenRouter, Tavily *or* Brave, Langfuse. All free tiers.
3. Pick **one real benchmark business** (a local service with a thin website) and one competitor — every phase gets tested against it, not against a toy.
4. Execute Phase 0 today: half a day, unblocks everything.
