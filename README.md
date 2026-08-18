# Social Marketing Agent

An AI growth agent for small businesses. Give it a website and any documents the
business has, and it produces the content and instrumentation that earns
visibility on **Google**, in **AI answer engines**, and on **social media** — with
a lead-capture and attribution loop, so the output is measured in leads rather
than in vibes.

**Status: Phase 0 complete** — foundations, quality gates, and infrastructure.
No agent yet; Phase 1 is the first end-to-end slice.

---

## Why this exists

A small business owner knows they need content marketing but has neither the SEO
expertise nor the time. The alternatives are a €500–850/month tool stack they
must operate themselves, or a €2,000–5,000/month agency. Generic ChatGPT output
is off-brand, ignores search intent, and still has to be reformatted for every
channel by hand.

Existing "AI SEO" products sell **reach** — "30 articles a month". A business that
publishes 30 articles with no call to action, no form, and no attribution gets
traffic and no leads, then cancels. This project owns the whole chain:

```
REACH → RELEVANCE → CONVERSION → ATTRIBUTION → COMPOUNDING
```

**Users:** SMB owners without a marketing team (primary), solo in-house
marketers, and small agencies running several clients.

## Documentation

| Document | What it answers |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Strategy, scope, honest constraints |
| [FEATURES.md](FEATURES.md) | Every feature, and how each one produces a lead |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical design; architecture → business benefit |
| [docs/DIAGRAMS.md](docs/DIAGRAMS.md) | All 14 diagrams: context, request flow, state machine, data model, deployment |
| [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md) | How the agents work: state, nodes, tool loop, prompts, memory, caps |
| [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md) | **The execution plan — build from this** |
| [docs/CHANNELS.md](docs/CHANNELS.md) | What we can actually publish, and per-platform content |
| [docs/FREE_CHANNELS.md](docs/FREE_CHANNELS.md) | Free presence and citations; why Wikipedia is excluded |
| [docs/CRITERIA_MAP.md](docs/CRITERIA_MAP.md) | Evidence map, agent concepts, claims discipline |

## Quickstart

Requires Python 3.13, Node 22, Docker, and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
make install          # uv sync + pnpm install
make up               # postgres :5435, redis :6381
make api              # terminal 1 — FastAPI on :8100
make web              # terminal 2 — Next.js on :3100
```

Open http://localhost:3100. The page reports live backend status; stop the API
and press **Re-check** to see the designed failure state.

```bash
make check            # lint + types + tests — exactly what CI runs
make images           # build both Docker images
```

## Architecture in one rule

> **If the answer is computable, compute it. Only ask a model to decide,
> interpret, or write.**

Three component kinds, and the separation is enforced by a test rather than by
convention:

| | Does | Determinism |
|---|---|---|
| **Engines** | crawl, parse, score, count, read APIs | total — no LLM, no DB, no side effects |
| **Actuators** | publish, post, send — anything with an external effect | total, given the idempotency key |
| **Agents** | plan, prioritise, interpret, write | none — bounded and evaluated |

`tests/test_engine_boundary.py` walks the AST of every module under
`backend/app/engines/` and fails the build on a forbidden import. The rule was
installed before the first engine existed, so it can never need retrofitting.

## Layout

```
backend/app/
├─ api/          FastAPI routes (thin)
├─ core/         config, security, budgets
└─ engines/      deterministic computation — guarded
tests/           pytest; every external call faked, no network in CI
frontend/        Next.js 16 · React 19 · Tailwind 4
docs/            design and planning
```

## Technical decisions

| Decision | Why | Rejected |
|---|---|---|
| **LangGraph** state machine (Phase 6) | resumable, bounded steps, human interrupt, per-node evaluation | unbounded ReAct loop |
| **OpenRouter** + a second direct adapter | one integration for many models; the abstraction is proven by two implementations, not asserted | per-provider SDK sprawl |
| **Postgres + pgvector** | app data and RAG in one store to operate and back up | a separate vector database |
| **Deterministic SEO scoring** | counting is not a language task; it must be testable and free | an LLM judging SEO |
| **Probe-based AI visibility** | no citation API exists; probing is measurable today | waiting for a vendor API |
| **Next.js** | production UX and real front-end work | Streamlit / Gradio |
| **pnpm pinned via `packageManager`** | host, CI and Docker must apply the same supply-chain policy | unpinned `corepack enable` |
| **`nodeLinker: hoisted`** | Next `standalone` tracing cannot follow pnpm store symlinks | patching the standalone output |

Full reasoning, including alternatives, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §14.

## Honest limits

- Deterministic SEO scoring gates drafts; it does not predict rankings.
- AI share-of-voice is a **sample**, not a census — model answers are
  non-deterministic and shift with model updates.
- Lead attribution is last-click via UTM, so multi-touch journeys are undercounted.
- Google movement takes 6–12 weeks; the product reports leading indicators, and
  never promises traffic.
- Content velocity is capped by design. This will not mass-produce, and shouldn't.
