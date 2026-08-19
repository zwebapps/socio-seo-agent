# Evaluation report

Generated 2026-08-19 14:00 UTC · `uv run python evals/run.py --live`

## What produced these numbers

> **Live run.** Real providers were called and real money was spent. Model providers configured: openrouter.

- **Model providers:** Model providers configured: openrouter.
- **Generation tier:** **cheap** (overridden by `--tier`) — chain in preference order: `openrouter/openai/gpt-4.1-mini`, `anthropic/claude-haiku-4-5`, `openrouter/google/gemini-2.5-flash`. Entries whose provider has no credential are skipped, and a 403/404 on one model falls through to the next, so the model that served is not always the first listed — the per-case rows carry the actual model.
- **Tracing:** Tracing is not configured, so every span is served by the no-op tracer and nothing leaves the process. Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to send traces to Langfuse.
- **Ragas faithfulness / answer relevancy:** n/a (ragas not installed). The columns are reserved and rendered empty; no value is estimated from the deterministic scores, because a faithfulness number that is really a rubric average would be a fabrication.
- **Cases:** 20 of 20 in the eval set.
- **Scoring:** deterministic (`evals/rubric.py`). No model is used as a judge.

## Per case

One row per case per arm. **`—`** means the dimension does not apply to the channel: a social post has no title tag, so scoring one would produce a number about nothing. **`n/e`** means *not exercised* — the cell scored 1.00 only because there was nothing to check (no figures to trace, no banned claim configured), which is an absence of risk rather than a pass.

| case | channel | arm | seo | brand | format | grounding | coverage | mean | ragas faithfulness | ragas relevancy |
|---|---|---|---|---|---|---|---|---|---|---|
| `plumber-01` | blog_article | rag_off | 0.84 | 1.00 | 1.00 | 0.00 | 1.00 | **0.77** | — | — |
| `plumber-01` | blog_article | rag_on | 0.89 | 1.00 | 1.00 | 0.21 | 1.00 | **0.82** | — | — |
| `plumber-02` | linkedin | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `plumber-02` | linkedin | rag_on | — | 1.00 | 1.00 | 0.50 | 1.00 | **0.88** | — | — |
| `plumber-03` | instagram_caption | rag_off | — | 1.00 | 1.00 | 0.00 | 0.50 | **0.62** | — | — |
| `plumber-03` | instagram_caption | rag_on | — | 1.00 | 1.00 | 0.00 | 0.50 | **0.62** | — | — |
| `plumber-04` | facebook_post | rag_off | — | 1.00 | 1.00 | 1.00 n/e | 1.00 | **1.00** | — | — |
| `plumber-04` | facebook_post | rag_on | — | 1.00 | 1.00 | 0.50 | 1.00 | **0.88** | — | — |
| `dentist-01` | blog_article | rag_off | 0.84 | 1.00 | 1.00 | 0.00 | 1.00 | **0.77** | — | — |
| `dentist-01` | blog_article | rag_on | 0.78 | 1.00 | 1.00 | 0.80 | 1.00 | **0.92** | — | — |
| `dentist-02` | facebook_post | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `dentist-02` | facebook_post | rag_on | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `dentist-03` | instagram_caption | rag_off | — | 1.00 | 1.00 | 1.00 n/e | 1.00 | **1.00** | — | — |
| `dentist-03` | instagram_caption | rag_on | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `dentist-04` | email | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `dentist-04` | email | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `bakery-01` | blog_article | rag_off | 0.78 | 1.00 | 1.00 | 0.00 | 0.67 | **0.69** | — | — |
| `bakery-01` | blog_article | rag_on | 0.83 | 1.00 | 1.00 | 0.00 | 0.67 | **0.70** | — | — |
| `bakery-02` | instagram_caption | rag_off | — | 1.00 | 1.00 | 0.00 | 0.67 | **0.67** | — | — |
| `bakery-02` | instagram_caption | rag_on | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `bakery-03` | facebook_post | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `bakery-03` | facebook_post | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `bakery-04` | email | rag_off | — | 1.00 | 1.00 | 0.00 | 0.67 | **0.67** | — | — |
| `bakery-04` | email | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `steuerberater-01` | linkedin | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `steuerberater-01` | linkedin | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `steuerberater-02` | blog_article | rag_off | 0.91 | 1.00 | 1.00 | 0.00 | 1.00 | **0.78** | — | — |
| `steuerberater-02` | blog_article | rag_on | 0.85 | 1.00 | 1.00 | 0.40 | 1.00 | **0.85** | — | — |
| `steuerberater-03` | email | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `steuerberater-03` | email | rag_on | — | 1.00 | 1.00 | 0.50 | 1.00 | **0.88** | — | — |
| `steuerberater-04` | facebook_post | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `steuerberater-04` | facebook_post | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `saas-01` | blog_article | rag_off | 0.84 | 1.00 | 1.00 | 0.00 | 1.00 | **0.77** | — | — |
| `saas-01` | blog_article | rag_on | 0.84 | 1.00 | 1.00 | 0.64 | 0.67 | **0.83** | — | — |
| `saas-02` | linkedin | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `saas-02` | linkedin | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `saas-03` | email | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `saas-03` | email | rag_on | — | 1.00 | 1.00 | 0.67 | 1.00 | **0.92** | — | — |
| `saas-04` | facebook_post | rag_off | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |
| `saas-04` | facebook_post | rag_on | — | 1.00 | 1.00 | 0.00 | 1.00 | **0.75** | — | — |

## Aggregate

`not exercised` counts the cells that scored 1.00 with nothing to check. Read the mean against it: a high average carried by untested dimensions is not a high average.

| arm | cases | mean | dimensions passed | failed | not exercised |
|---|---|---|---|---|---|
| rag_off | 20 | **0.76** | 59 | 26 | 2 |
| rag_on | 20 | **0.84** | 59 | 26 | 0 |

Per dimension:

| dimension | rag_off | rag_on |
|---|---|---|
| brand | 1.00 | 1.00 |
| coverage | 0.93 | 0.94 |
| format | 1.00 | 1.00 |
| grounding | 0.10 | 0.41 |
| seo | 0.84 | 0.84 |

### Deterministic format enforcement

Hashtag counts are enforced by `backend/app/engines/channel` before scoring, because counting is arithmetic and a model will not do it reliably — measured on `gpt-4.1-mini`, the bare instruction `Keine Hashtags` still produced 21. **Read the `format` row above together with this one:** a correction is work the model left for the renderer, so a clean `format` score with a high correction count is the renderer's competence and not the model's. **Zero corrections is therefore the good outcome, not a sign the check is idle** — it means the prompt carried the rule on its own. Length is deliberately *not* enforced: truncating or padding copy would be editing it rather than formatting it, so `format` can still fail on length.

| arm | pieces corrected | hashtags removed | pieces left short of the minimum |
|---|---|---|---|
| rag_off | 0 of 20 | 0 | 0 |
| rag_on | 0 of 20 | 0 | 0 |

## RAG off vs RAG on vs oracle

Grounding is the dimension RAG exists to move, so it is reported under three evidence conditions. **`rag_off`** offers the scorer no chunks. **`rag_on`** offers exactly the chunks the shipped agentic retrieval loop kept. **`oracle`** offers every fact in the case — the ceiling perfect retrieval would allow.

The oracle column is the honest part: without it, a two-column table cannot distinguish *retrieval found nothing* from *there was nothing to find*.

| case | retrieval outcome | kept | generated off | generated on | generated oracle | reference off | reference on | reference oracle |
|---|---|---|---|---|---|---|---|---|
| `plumber-01` | sufficient | 3 | 0.00 | 0.21 | 0.21 | 0.00 | 1.00 | 1.00 |
| `plumber-02` | fallback_to_web | 2 | 0.00 | 0.50 | 0.50 | 0.00 | 1.00 | 1.00 |
| `plumber-03` | fallback_to_web | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 | 1.00 |
| `plumber-04` | sufficient | 1 | 1.00 | 0.50 | 0.50 | 0.00 | 0.50 | 1.00 |
| `dentist-01` | sufficient | 1 | 0.00 | 0.80 | 0.80 | 0.00 | 0.50 | 1.00 |
| `dentist-02` | sufficient | 1 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 1.00 |
| `dentist-03` | sufficient | 1 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 1.00 |
| `dentist-04` | sufficient | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 0.50 | 1.00 |
| `bakery-01` | sufficient | 2 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 1.00 |
| `bakery-02` | sufficient | 1 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 1.00 |
| `bakery-03` | sufficient | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 1.00 | 1.00 |
| `bakery-04` | sufficient | 2 | 0.00 | 0.67 | 0.67 | 0.00 | 1.00 | 1.00 |
| `steuerberater-01` | sufficient | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 0.50 | 1.00 |
| `steuerberater-02` | fallback_to_web | 1 | 0.00 | 0.40 | 0.40 | 0.00 | 0.50 | 1.00 |
| `steuerberater-03` | sufficient | 1 | 0.00 | 0.50 | 0.50 | 0.00 | 1.00 | 1.00 |
| `steuerberater-04` | fallback_to_web | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 1.00 | 1.00 |
| `saas-01` | sufficient | 2 | 0.00 | 0.64 | 0.64 | 0.00 | 1.00 | 1.00 |
| `saas-02` | fallback_to_web | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 1.00 | 1.00 |
| `saas-03` | sufficient | 1 | 0.00 | 0.67 | 0.67 | 0.00 | 0.67 | 1.00 |
| `saas-04` | sufficient | 1 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 1.00 |

Retrieval kept **26 chunk(s) across 20 case(s)**, grounding 20 of them.

## Rubric self-check

A rubric nobody has seen fail is a rubric nobody should believe. Each case carries a human-written correct answer and a mutation of it with a banned claim appended, so the rubric has to pass one and fail the other.

- Reference answers passing the brand check: **20/20**
- Violating mutations correctly rejected: **20/20**

SEO and format discrimination are covered by unit tests (`backend/tests/evals/test_rubric.py`) rather than here, because they need markup and channel fixtures rather than prose.

## Unpublishable outputs

Listed separately from the averages on purpose: a banned claim, a channel that would reject the post, or a citation to a chunk that was never retrieved cannot be averaged away.

- none

## What this harness cannot measure

Stated here so the report cannot be read as claiming more than it does (`docs/CRITERIA_MAP.md` §7).

1. **Whether the copy is any good.** Nothing here judges persuasion, tone, register or German grammar. A fluent, on-brand, useless paragraph scores the same as a good one.
2. **Semantic faithfulness.** `score_grounding` checks that the *figures* in a claim appear in a cited chunk. A sentence that cites a real chunk and then misdescribes it in words passes. That is the gap Ragas would close, and Ragas is not installed.
3. **Rankings.** The SEO column is a deterministic on-page audit. It does not predict Google positions, and a 1.00 is not a promise of traffic.
4. **The article renderer.** It does not exist yet (Phase 6). For article cases the harness wraps the generated body in a minimal HTML skeleton built from the case, so the title, meta, link and schema rules score the skeleton and not a shipped renderer.
5. **Retrieval quality beyond term overlap.** The harness embeds with a hashed bag of words, so a synonym does not retrieve. A retrieval miss here may be the embedder rather than the query rewrite.
6. **Channel limits.** `CHANNEL_LIMITS` mirrors `docs/CHANNELS.md` §6, whose own instruction is to verify every number against provider documentation. Until the `channel_specs` config table lands, the rubric holds a second copy of those limits, which is exactly the drift risk the table exists to prevent.
7. **Prompt v1 vs v2 and cheap vs strong model.** `docs/BUILD_ORDER.md` Phase 12 asks for both comparisons. Neither is implemented: there is one harness prompt per arm and the router picks the tier. The columns are absent rather than invented.
