# Free Channels — presence, citations, and free distribution

Answers: **why isn't Wikipedia here**, and **which free platforms to target first**.

Companion to [CHANNELS.md](CHANNELS.md) (paid/social publishing) · [BUILD_ORDER.md](BUILD_ORDER.md).

---

## 1. Wikipedia — excluded on purpose

Five independent reasons, any one of which is disqualifying:

1. **Notability.** Wikipedia requires significant coverage in independent reliable sources. A local plumber, dentist, restaurant or small SaaS **does not qualify**, and an article about one gets deleted — usually within days. The customers this product serves are precisely the ones who can't have an article.
2. **Paid and conflict-of-interest editing.** Wikimedia's Terms of Use require disclosure of paid contributions. Editing on behalf of a client without disclosure is a **Terms of Use violation**, and promotional editing gets reverted regardless of disclosure.
3. **Automated promotional editing is the exact abuse Wikipedia polices hardest.** The realistic outcome is not a rejected edit — it is the **client's domain added to the spam blacklist**, which then blocks it across all Wikimedia projects and signals low quality elsewhere. We would be actively damaging the customer.
4. **The links are `nofollow`.** Even if all of the above were solved, there is no direct SEO link value. The value people imagine is not there.
5. **It's a human PR job, not an agent task.** A genuinely notable business earns an article through press coverage. That's earned media — a person's work, on a months-long timeline.

**So: no Wikipedia editing capability. Ever.** If the loop is ever instructed to build one, treat it as a bug in the instruction.

### The legitimate version of that instinct

The reason Wikipedia feels attractive is real: it's heavily cited in AI answers and feeds Google's Knowledge Panel. But the mechanism you actually want is **entity resolution** — making the business unambiguously identifiable to search engines and language models — and Wikipedia is one of several inputs, not the lever.

The lever is: **consistent identity everywhere + structured data on your own site + Google Business Profile.** That is achievable, free, safe, and almost nobody does it properly.

**Wikidata** is the one adjacent target that is sometimes legitimate — it's structured, has a lower bar than Wikipedia, and directly feeds knowledge graphs. But it still has notability rules, needs verifiable external sources, and must be human-reviewed. It sits in Track B with a human gate, never as an autonomous action.

---

## 2. Reframing: "free platforms" are three different jobs

Lumping them together is why most SEO tools do all three badly.

```
JOB 1 — PRESENCE & CITATIONS        JOB 2 — ENTITY RESOLUTION       JOB 3 — FREE DISTRIBUTION
be findable where intent lands      make machines know who you are   put content where people are

Google Business Profile             consistent NAP everywhere        YouTube
Bing Places                         schema.org on own site           LinkedIn / Facebook / Instagram
Apple Business Connect              Wikidata (human-gated)           Pinterest
local + industry directories        review-platform profiles         Medium/Substack (canonical!)
review platforms                    entity-consistent naming         email (owned)

→ drives LEADS directly             → drives AI CITATIONS            → drives REACH
→ highest ROI for local             → the actual "Wikipedia" goal     → slowest to compound
```

**Job 1 is where the leads are, and it's where to start.** Job 2 is what you were reaching for with Wikipedia. Job 3 is what everyone builds first and it's the least immediately valuable for a local business.

---

## 3. Ranked free platforms — by lead value per hour of work

### Tier 1 — do these before anything else

| Platform | Free? | Why it's first | Automatable? |
|---|---|---|---|
| **Google Business Profile** | yes | For a local business this **outperforms every other free channel combined**. It's where "plumber near me" intent lands: posts, photos, Q&A, reviews, direct call button. API exists for posts, Q&A, reviews | **Yes — posts, Q&A responses, review replies (draft), photos** |
| **Their own website** | owned | Money pages, local pages, schema, link hub. Full control, no platform rules | yes — already core |
| **Email to an owned list** | ~free | Highest conversion rate of anything here | yes — already core |
| **Bing Places** | yes | Small direct traffic, but **Bing's index has historically fed AI assistants** — so it's an AI-visibility play, not a traffic play | partial — submission is manual, content generated |
| **Apple Business Connect** | yes | Apple Maps + Siri. Genuinely underused, so the competitive gap is wide | partial |

### Tier 2 — citations and reviews (boring, cheap, effective)

Local ranking and entity resolution both depend on **NAP consistency**: name, address, phone identical across every listing. Inconsistency actively suppresses local ranking, and it's extremely common.

German market specifically: **Das Örtliche · Gelbe Seiten · 11880 · meinestadt.de · Cylex · Yelp DE · Trustpilot · ProvenExpert · WerKennt DenBesten**, plus the trade association or industry directory for that vertical (which is usually the highest-quality citation available and always overlooked).

| What the agent does | What a human does |
|---|---|
| Generate one canonical NAP record + category mapping + 3 description lengths per directory | Submit / claim listings (captchas, phone verification, postal PINs) |
| **Audit existing listings for inconsistency** — this is the valuable half, and it's fully automatable | Fix disputed or duplicate listings |
| Draft review responses in brand voice | Approve and post them |
| Monitor for new reviews and rating changes | Handle anything requiring judgement |

**The audit is the product here.** Telling a business "your phone number differs across four listings and your name has three spellings" is concrete, verifiable, immediately actionable, and no tool they own does it.

### Tier 3 — free distribution and AI-citation surfaces

| Platform | Value | Automation boundary |
|---|---|---|
| **YouTube** | free, permanent, second-largest search engine, and a blog outline already *is* a video outline | generate script + metadata; **human films** |
| **Pinterest** | underrated: links are clickable (unlike IG/TikTok), content half-life is months not hours, strong for home services, food, trades | generate pin copy + board strategy; needs an image |
| **LinkedIn / Facebook / Instagram** | covered in CHANNELS.md | render, export or publish |
| **Reddit / Quora** | **heavily cited by AI answers** and increasingly visible in Google | **research only — never automated posting.** Automated promotional posting violates site rules and gets accounts and domains banned. The agent mines these for the *questions real people ask*, which becomes content on our own site. That's the safe, high-value use |
| **Medium / Substack / dev.to** | free reach for republished content | **only with a `rel=canonical` back to the original**, or you compete with your own page |
| **Industry forums / trade communities** | high-quality, high-intent | human participation only |

---

## 4. Never build these

| Tactic | Why not |
|---|---|
| Wikipedia editing / article creation | §1 — ToU violation risk, deletion, domain blacklisting |
| Automated Reddit / Quora / forum posting | Against site rules; bans the account and taints the domain |
| Blog-comment, forum-profile or guestbook links | Link scheme; Google's own guidelines name these |
| Article directories, PBNs, link exchanges | Link scheme, and it's what AutoSEO's backlink network flirts with — explicitly out of scope |
| Fake or incentivised reviews | Illegal in the EU, and platform-fatal |
| Mass-created near-duplicate location pages | Scaled-content abuse. Local pages must be genuinely distinct or not exist |

**The pattern:** every "free" tactic that scales by volume rather than by quality is a link scheme or a ToS violation. The free channels worth building are the ones that scale by *consistency* — and consistency is exactly what software is good at and humans are bad at.

---

## 5. What ships when

| Capability | Track A (course build) | Track B |
|---|---|---|
| **NAP consistency audit** across Tier 2 directories | **yes — high value, pure engine work, no API keys** | auto-monitoring + change alerts |
| Canonical NAP record + per-directory description variants | **yes — generation** | submission assistance |
| `LocalBusiness` / `Service` / `FAQPage` schema on own site | **yes** | validation monitoring |
| Google Business Profile posts + Q&A + review replies | **generate + export pack** | GBP API direct publish |
| Bing Places / Apple Business Connect data pack | **generate** | submission workflow |
| Reddit/Quora **question mining** → content briefs | **yes — this is a research engine, safe and useful** | trend monitoring |
| Pinterest pin copy | Track B | with image generation |
| Medium/Substack republish with canonical | Track B | — |
| Wikidata entity | Track B, human-gated | — |
| Wikipedia | **never** | **never** |

**Recommended addition to Phase 4** (the `seo`/`serp` engine phase): the **NAP audit**. It's deterministic engine work, needs no paid API, produces an immediately convincing screen, and it demonstrates the Engine/Agent split perfectly — Python finds the inconsistencies, the LLM only explains what they cost and what to do about them.

---

## 6. Why "free first" is the right instinct — and its real constraint

Free is correct on cash: Tier 1 and Tier 2 cost nothing but time, and for a local business they outperform paid social. But note what the constraint becomes:

> **Free platforms don't cost money. They cost human hours and they enforce rules.**

So on free channels the agent's job is mostly **preparation, consistency and monitoring** — not posting. That's a narrower job than "publish everywhere", and it's a much more defensible one: it's the work that is genuinely tedious for a person, genuinely easy for software, and genuinely never done.

Nobody churns from a tool that keeps telling them true things about their own business that they didn't know.
