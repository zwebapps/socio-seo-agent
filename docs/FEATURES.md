# Feature Catalogue — and how each one actually produces a lead

Companion to [ROADMAP.md](ROADMAP.md). This document answers two questions: **what can we build**, and **which of it actually generates leads for the customer**.

---

## 0. The lead chain — why the grouping matters

A lead requires all five links. Break any one and the leads are zero, no matter how good the others are.

```
REACH ──► RELEVANCE ──► CONVERSION ──► ATTRIBUTION ──► COMPOUNDING
  │            │             │              │               │
someone     it matches    there is a     we know which    we do more
finds       what they     way to ask     content caused   of what worked
them        wanted        for it         it
```

**Competitors (AutoSEO, BabyLoveGrowth) sell REACH only** — "30 articles/month". A business that publishes 30 articles with no CTA, no form, and no attribution gets traffic and no leads, then churns. Our differentiation is owning the whole chain, and being able to *prove* link 4.

**The most important consequence — the fastest leads are not from new content.** They are from fixing CONVERSION on pages that already receive traffic. New Google content takes 6–12 weeks minimum. So the feature priority is deliberately: fix conversion → social + lead magnet → AI answers → then Google content.

---

## 1. Lead-generating features (the money list)

These produce leads **directly**. If we build nothing else, we still have a product.

| # | Feature | How it makes a lead | Time to first lead |
|---|---|---|---|
| L1 | **Conversion audit of existing pages** — is there a CTA above the fold, a form, a phone/WhatsApp link, a reason to act? | Converts traffic the business *already pays for or earns*. Moving 1% → 3% on 300 visits/mo = +6 leads/mo | **Days** |
| L2 | **Hosted lead form** + instant notification (email/webhook) | The actual capture surface. Without it nothing else is a lead | Days |
| L3 | **Lead magnet generated from their own documents** — checklist, buyer's guide, price/quote explainer, comparison sheet | Trades value for contact details; converts 3–5× a bare contact form | Days |
| L4 | **Local landing pages: service × city** with `LocalBusiness` schema, map, hours, click-to-call | Highest commercial intent that exists for a local business ("emergency plumber Koblenz"), and ranks far faster than blog content | 3–8 weeks |
| L5 | **Money/service page generation + rewrite** (commercial intent, objection handling, proof, pricing framing) | These are the pages that actually convert; blogs mostly don't | 4–10 weeks |
| L6 | **Social posts with mandatory UTM'd CTA** → landing page | Reaches an existing audience immediately, no waiting on indexation | **Days** |
| L7 | **Click-to-call / WhatsApp / booking-link blocks** | For local service the lead is a phone call, not a form. Ignoring this loses most of them | Days |
| L8 | **AI-answer coverage on high-intent prompts** ("best X in Y", "how much does X cost", "X vs Y") | Being the cited brand in an AI answer sends pre-qualified traffic; the fastest-moving new channel | 2–6 weeks |
| L9 | **Internal linking engine** — routes authority from blog posts to money pages | Makes existing content work for the pages that convert | 4–8 weeks |
| L10 | **Lead inbox with attribution** (content piece → lead) | Not a lead source, but it's how the customer *believes* the leads are real — which is what stops churn | Immediate |

---

## 2. Understand — foundation (no leads directly, everything depends on it)

| # | Feature | Engine/Agent | Phase |
|---|---|---|---|
| U1 | Website crawl + technical audit (status, canonical, titles, meta, heading tree, internal links, images/alt, sitemap, robots, existing schema) | `crawl` | 1 |
| U2 | **Document ingestion** — PDF/DOCX/MD/URL: service sheets, price lists, case studies, brochures → chunked, embedded knowledge base | `kb` | 1 |
| U3 | Business DNA extraction from the site + docs → services, audience, locations, USPs, tone, banned claims — user confirms | `crawl` + `research` | 1 |
| U4 | Competitor discovery + content gap analysis | `serp` + `research` | 3 |
| U5 | Keyword universe with intent classification (commercial / local / informational / comparison) | `serp` | 3 |
| U6 | AI-answer baseline probe — where the brand stands today | `geo` | 4 |
| U7 | Conversion audit (= **L1**) | `crawl` + `seo` | 3 |
| U8 | Scanned-PDF detection + OCR offer (don't silently index nothing) | `kb` | 1 |

---

## 3. Attract — Google

| # | Feature | Lead effect | Phase |
|---|---|---|---|
| G1 | SEO blog post generation — intent-matched, grounded in `kb`, every claim cited | Indirect (top of funnel) | 5 |
| G2 | Service/money page generation (= **L5**) | **Direct** | 5 |
| G3 | Local service × city pages (= **L4**) | **Direct** | 5 |
| G4 | Comparison / alternatives / pricing pages | **Direct** — highest-intent informational format | 5 |
| G5 | FAQ blocks + JSON-LD (`Article`, `FAQPage`, `LocalBusiness`, `Service`) | Indirect + AI-citation eligibility | 3 |
| G6 | On-page fix list for **existing** pages (titles, meta, H-tree, thin content, missing alt) | Indirect, fast — improves pages already indexed | 3 |
| G7 | Internal linking engine (= **L9**) | **Direct** | 3 |
| G8 | Deterministic SEO score 0–100 with itemised failures, gate at 85 | Quality guarantee | 3 |
| G9 | Sitemap + indexing ping after publish | Speeds time-to-index | 5 |
| G10 | Rank tracking over time per keyword | Measurement | B1 |

---

## 4. Attract — AI answer engines (the wedge)

| # | Feature | Lead effect | Phase |
|---|---|---|---|
| A1 | **AI share-of-voice tracking** — fixed 30–50 prompt set × 2–3 models, mention + citation parsed, scored, trended | Measurement, and the demo that sells the product | 4 |
| A2 | Competitor share-of-voice comparison on the same prompt set | Sales/renewal argument | 4 |
| A3 | **Answer-shaped content blocks** — self-contained, quotable, one claim per paragraph, stat + source | **Direct** — this is what gets cited | 5 |
| A4 | Citation-gap → content brief loop: "absent from these 12 answers → here is the page that fixes it" | **Direct** | 5 |
| A5 | Entity consistency pass — brand name, NAP, services stated identically across pages so models resolve the entity | Indirect but foundational to being cited at all | 5 |
| A6 | `no_answer` / refusal handling so a model outage never counts as absence | Metric integrity | 4 |

---

## 5. Attract — Social

| # | Feature | Lead effect | Phase |
|---|---|---|---|
| S1 | One source article → 4 channel-native posts (LinkedIn, X thread, Instagram, Facebook) | **Direct** via L6 | 5 |
| S2 | Per-platform rule enforcement — length, hashtag count, link placement, emoji policy (deterministic, post-generation) | Quality | 3 |
| S3 | Hook variants (3 per post) for A/B testing | Lifts click rate | 5 |
| S4 | **Mandatory UTM tagging** on every outbound link | Without it, attribution is impossible | 6 |
| S5 | Content calendar + export (CSV/JSON) / scheduled posting | Consistency | 7 / B2 |
| S6 | Repurposing loop — best-performing angle generates more of itself | Compounding | 11 |

---

## 6. Convert (the link everyone else skips)

| # | Feature | Phase |
|---|---|---|
| C1 | Intent-matched CTA generation (informational → lead magnet; commercial → quote/call) | 6 |
| C2 | Landing page generation with embedded form | 6 |
| C3 | Hosted form endpoint, spam-protected (honeypot + rate limit + optional captcha) | 6 |
| C4 | Lead magnet generation from the customer's own documents (= **L3**) | 6 |
| C5 | Instant lead notification — email + webhook (Zapier/Make/CRM-ready) | 6 |
| C6 | Click-to-call / WhatsApp / booking link blocks (= **L7**) | 6 |
| C7 | Conversion-copy review of existing money pages | 6 |
| C8 | Thank-you page + follow-up email draft | B3 |

---

## 7. Measure & compound

| # | Feature | Phase |
|---|---|---|
| M1 | Lead inbox: source, UTM, attributed content piece, submitted fields | 6 |
| M2 | Per-content-piece metrics — GSC impressions/position, clicks, social clicks, leads | 10 / B1 |
| M3 | AI share-of-voice trend chart | 4 |
| M4 | Weekly growth report: what changed, what worked, what's next | B1 |
| M5 | **Opportunity engine** — ranked next actions with expected impact and effort | B2 |
| M6 | Feedback loop: rating + reject reasons → learned brand style + few-shot exemplars | 11 |
| M7 | Cost per lead, per business | B1 |

---

## 8. Platform features the customer pays for but doesn't call "features"

| # | Feature | Phase |
|---|---|---|
| P1 | Human approval before anything publishes; approval policy table (auto / notify / approve / human) | 5 |
| P2 | Brand rule enforcement — banned claims, regulated-claim guard (no medical/financial guarantees) | 9 |
| P3 | Multi-business support with tenant isolation (asserted by test) | 8 |
| P4 | Cost + token transparency, per-run and per-business budget caps | 2 |
| P5 | Multi-model choice with fallback chain (OpenRouter) | 2 |
| P6 | Resumable runs + idempotent publishing (never publishes twice) | 2 / 5 |
| P7 | Full traces (Langfuse) + evaluation reports | 10 |
| P8 | Prompt-injection defence on crawled pages and uploaded documents | 9 |
| P9 | Developer mode separated from user mode | 7 |
| P10 | Volume cap per business per week — deliberately cannot mass-produce | 5 |

---

## 9. First 30 days for a new customer — the sequence that produces leads fastest

This is the onboarding playbook, and it's the opposite of "start publishing 30 articles".

| Days | Action | Expected effect |
|---|---|---|
| 1 | Crawl site, ingest documents, confirm Business DNA, baseline AI SoV | — |
| 1–3 | **Conversion audit + fix CTAs/forms/call links on the pages that already get traffic** | **Leads within days** from existing traffic |
| 3–5 | Build one lead magnet from their own documents + landing page + form | Leads within days |
| 3–7 | First 8 social posts, all UTM'd to that landing page | Leads within days |
| 7–14 | 3 local service × city pages + entity consistency pass | Google leads at 3–8 weeks |
| 7–14 | Answer-shaped content for the 5 highest-intent AI prompts | AI citations at 2–6 weeks |
| 14–30 | 4 blog posts feeding internal links into the money pages | Google leads at 6–12 weeks |
| 30 | First growth report: SoV delta, impressions, leads by source | Renewal decision |

---

## 10. Honest lead math (illustrative, not a promise)

Worked example — a local trade business in a mid-size German city. Ranges depend entirely on market size and competition; publish this as a model, never as a guarantee.

| Source | Mechanism | Realistic monthly leads |
|---|---|---|
| Conversion fixes on existing traffic | 300 existing visits/mo, 1% → 3% | **+4 to +8, from week 1** |
| Social (4 posts/week + lead magnet) | 200–600 clicks/quarter, 2–5% convert | 2–5 |
| Local service × city pages (10–12 pages) | 800–1,500 combined searches, positions 5–9, 3–6% CTR, 5–12% convert on high intent | 2–8, from month 3 |
| AI answer citations | pre-qualified but low volume today, growing | 0–3 |
| Blog content | top of funnel, converts via lead magnet | 1–4, from month 3 |

**Two things this table teaches:** the week-one win is conversion, not content — and the compounding win is the local/money pages, not the blog. Price and pitch accordingly.

---

## 11. Deliberately not building

| Not building | Why |
|---|---|
| Backlink network / link buying | AutoSEO's actual moat, a link-scheme risk, and a business we're not equipped to run |
| Mass content production (30+ articles/month) | Google's scaled-content-abuse policy; and it breaks the chain at RELEVANCE |
| Autonomous publishing with no approval policy | One hallucinated claim on a customer's site ends the relationship |
| Paid ads spend automation | Real money, needs its own guardrails and licensing thought |
| Full CRM / email marketing platform | Integrate via webhook; don't rebuild HubSpot |
| Review-buying or fake reviews | Illegal in the EU |

---

## 12. Which of this is in the course build

**In (Track A, ~20 days):** U1–U8, G1–G9, A1–A6, S1–S4, C1–C7, M1/M3/M6, P1–P10 — i.e. all ten lead-generating features L1–L10.

**Later (Track B):** rank tracking over time, GSC/GA4 OAuth, weekly report, opportunity engine, scheduled posting, GBP/reviews, industry playbooks, agency multi-seat.
