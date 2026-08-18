# Channels — what we can actually publish, and how content is made for each

Answers two questions honestly: **can we publish to these platforms?** and **how is content generated per platform?**

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) · [BUILD_ORDER.md](BUILD_ORDER.md) · [../FEATURES.md](../FEATURES.md).

> **Free platforms — presence, citations and entity resolution (Google Business Profile, directories, NAP consistency, Reddit question mining, and why Wikipedia is excluded) are in [FREE_CHANNELS.md](FREE_CHANNELS.md).** Start there: for a local business those channels outperform everything in this document.

---

## 1. The correction that matters most

The proposed matrix marked UTM tracking ✅ for all seven channels. **That is not true for Instagram or TikTok**, and the difference is structural, not a detail:

> **Instagram feed captions and TikTok captions do not render clickable links.** A URL in an Instagram caption is plain text. So a UTM-tagged link in a caption is not a broken link — it is *no link at all*, and the lead never happens.

This isn't a tracking problem, it's a conversion problem, and it changes the design. The fix is in §5: a **link service we own** plus a **link hub**, which also decouples attribution from publishing — meaning we can attribute clicks on channels we cannot publish to at all.

Second correction: **"publish" means three different things** across that table, and collapsing them hides the real project risk.

---

## 2. Honest publish-capability matrix

| Channel | API exists | What it actually requires | Realistic verdict |
|---|---|---|---|
| **Facebook Page** | yes | `pages_manage_posts` + Page token + **Meta App Review** (screencast, privacy policy, business verification). Personal profiles: never. Groups: removed. | **Direct publish — after App Review** |
| **Instagram** | yes | Business/Creator account linked to a Page, `instagram_content_publish` + **App Review**. Media must be at a **publicly fetchable URL** (Meta pulls it). ~50 posts/24 h. No native scheduling — we schedule. | **Direct publish — after App Review** |
| **LinkedIn** | yes | Personal posting via `w_member_social` is attainable. **Company-page posting needs Marketing Developer Platform approval** — a slow, frequently-refused application. | **Personal: yes. Company page: gated** |
| **TikTok** | yes | Content Posting API has two modes: *direct post* and *upload-to-drafts*. **Unaudited apps are restricted to private/self-only visibility.** | **Upload-to-drafts is the shippable path** |
| **YouTube** | yes | `videos.insert` works, but costs ~1,600 units of a 10,000/day default quota → **~6 uploads/day per project**. Unaudited apps upload as private. | **Metadata/description: cheap. Uploads: quota-bound** |
| **Google Ads** | yes | Developer token with approved access, and it **spends real money** | **Assets only — never auto-spend** |
| **Email** | yes | Resend/Postmark. Real constraints are legal, not technical: consent, SPF/DKIM/DMARC, list ownership | **Direct send — the easiest real channel** |
| X / Twitter | yes | API v2 posting now sits behind a **paid tier** | **Export only — not worth the fee at this stage** |
| **Google Business Profile** | yes | Not in the original list, but for a local business a GBP post outperforms Instagram for leads | **Recommended addition, Track B** |

### The three tiers this collapses into

```
TIER 1 — DIRECT PUBLISH        we call the API, it goes live
         Facebook Page · Instagram · LinkedIn · Email · WordPress
         cost: App Review per platform

TIER 2 — DRAFT HANDOFF         we push it into their account; a human hits post
         TikTok (drafts) · YouTube (private upload) · Instagram (if unreviewed)
         cost: low. Honest, and often what the customer prefers anyway

TIER 3 — EXPORT PACK           copy-paste ready, or handed to their scheduler
         X · Google Ads assets · anything new
         cost: none. Works on day one, on every platform, forever
```

**Tier 2 is underrated.** A restaurant owner posting the draft themselves takes ten seconds, keeps them in control of their own brand account, and removes an entire class of failure (token expiry, App Review rejection, platform policy change). Sell it as a feature, because for many customers it is one.

---

## 3. The real schedule risk: App Review is calendar time, not work time

The code to post to Instagram is an afternoon. **Getting permission is two to six weeks**, needs a public privacy policy, a working demo, a screencast, and business verification — and it can be refused with a form letter.

You cannot plan a 20-day project around three third-party approval queues.

**Therefore:**

| | Decision |
|---|---|
| Course build (Track A) | **Tier 3 export packs + Email + WordPress drafts.** Zero approvals, zero blockers, and the full pipeline is provably real |
| Started in parallel from week one | Meta App Review submission (Facebook + Instagram), because it costs waiting, not working |
| Track B, once approved | Promote Facebook + Instagram to Tier 1; LinkedIn personal to Tier 1; TikTok/YouTube to Tier 2 |
| Never | Google Ads spend automation without an explicit human money gate |

### Aggregator vs direct — a real decision, made now

A social aggregator (Ayrshare, Postiz, Buffer's API, Late) collapses five App Reviews into one integration and one credential.

| | Direct integrations | Aggregator |
|---|---|---|
| App Reviews | 3–5, weeks each | zero |
| Per-post cost | free | per-profile subscription |
| Failure surface | 5 APIs, 5 token refresh flows | 1 API, 1 vendor |
| Dependency risk | none | vendor pricing and survival |
| Feature depth | full | lowest common denominator |

**Decision: Tier 3 export for the course build; an aggregator behind the same `publish` Actuator interface as the first Track-B step; direct integrations only for the one or two channels that prove they drive leads.** The Actuator contract in ARCHITECTURE.md §3 already makes this a swap rather than a rewrite — an aggregator and a direct API are two adapters behind one interface, and nothing upstream knows the difference.

---

## 4. Content generation architecture — one atom, many renderings

Do **not** generate seven pieces with seven prompts. That produces seven inconsistent messages, costs seven times as much, and makes brand-voice drift inevitable.

```
                      ONE RESEARCH PASS
                   (engines: crawl · kb · serp · geo)
                              │
                              ▼
                     ┌──────────────────┐
                     │  MESSAGE SPINE   │  ← the atom, generated once
                     │                  │
                     │  claim           │  the single thing being said
                     │  proof           │  stat / quote / case, with source
                     │  audience        │  who it's for
                     │  intent          │  informational | commercial | local
                     │  objection       │  what stops them acting
                     │  cta_goal        │  call | form | booking | download
                     │  key_facts[]     │  from their own documents, cited
                     │  entities[]      │  service, city, brand
                     └────────┬─────────┘
                              │
        ┌──────────┬──────────┼──────────┬──────────┬─────────┐
        ▼          ▼          ▼          ▼          ▼         ▼
     Article   LinkedIn   Instagram   TikTok     YouTube    Email
     (Google)             + Reel      script     desc.      
        │          │          │          │          │         │
        └──────────┴──────────┴──────────┴──────────┴─────────┘
                              │
                    DETERMINISTIC VALIDATION
        length · hashtags · link mechanism · banned claims · reading level
                              │
                     fail → single-channel regen
```

**Why a spine and not "rewrite the article for LinkedIn":** rewriting drifts, because the model re-decides the point each time. A spine fixes *what is being said* and lets each renderer decide only *how to say it there*. It also makes the per-channel call cheap — a cheap-tier model can render a spine into a caption; only the spine and the long-form article need a strong model.

**Renderer = one LLM call + deterministic post-processing.** The model writes; Python enforces. Never ask a model to count characters or hashtags — it will be wrong, and the platform will reject the post.

### Channel specs live in a config table, not in code

Platform limits change without notice. So `channel_specs` is a database/config table (same principle as operational config elsewhere: **rules that change belong in data, not in a deploy**), validated at render time.

```python
class ChannelSpec(BaseModel):
    channel: str
    max_chars: int
    hard_max_chars: int  # platform reject threshold
    hashtags_min: int
    hashtags_max: int
    link_in_body: bool  # ← the Instagram/TikTok truth
    link_mechanism: Literal[
        "inline", "bio_hub", "story_sticker", "first_comment", "description", "none"
    ]
    media_required: Literal["none", "image", "video", "either"]
    aspect_ratios: list[str]
    emoji_policy: Literal["none", "sparing", "native"]
    tone_shift: str  # how this channel differs from base brand voice
    hook_style: str
    updated_at: datetime  # verify against provider docs
```

Values below are **starting values to verify against provider documentation at build time** — treat any number here as a default to check, never as truth.

---

## 5. The link and attribution layer (the fix to §1)

Because two major channels can't carry a clickable link, attribution cannot depend on publishing. Three owned components:

**1. Short-link service — `/l/{code}`**
Every generated CTA gets a short link that 302s to the destination and records `{channel, content_piece_id, campaign, ts, ua_hash}`. It works whether we published the post or the owner pasted the caption by hand. **This is what makes attribution independent of publishing**, and it's the single highest-leverage thing in this document.

**2. Link hub — `/go/{business_slug}`**
A fast, zero-JS page listing the business's current CTAs, used as the Instagram/TikTok bio link. Each entry is a tracked short link, so "link in bio" becomes measurable rather than a black hole.

**3. Per-channel UTM policy**
`utm_source=instagram · utm_medium=social_organic · utm_campaign={content_slug} · utm_content={variant}`. Built by the `social` engine, never hand-written, so a channel comparison is actually comparable.

| Channel | Link mechanism | Attribution route |
|---|---|---|
| Facebook | inline URL | short link inline |
| LinkedIn | inline URL (or first comment to protect reach) | short link |
| Instagram feed | **no clickable link** | bio → link hub · Story sticker where available |
| Instagram Story | link sticker | short link |
| TikTok | **no clickable link** | bio → link hub |
| YouTube | description link | short link, first line |
| Email | inline | short link, per-recipient campaign tag |
| Google Ads | final URL | UTM on the final URL |

---

## 6. Per-platform content specification

Verify all numbers at build time; enforce from `channel_specs`, not constants.

### Instagram
- **Feed caption:** hook in the first ~125 characters (the rest is behind "more"); ~2,200 char ceiling; 3–5 relevant hashtags, not 30 — a wall of tags reads as spam and doesn't help discovery the way it did years ago.
- **Reel:** 15–45 s script with a 3-second visual hook, on-screen text lines (separate field — most viewers watch muted), and a spoken script.
- **Carousel:** 5–8 slides, one idea per slide, slide 1 is the hook, last slide is the CTA.
- **Link:** bio hub or Story sticker. Never a caption URL.
- **Lead mechanism:** DM keyword ("comment PRICE"), bio hub, Story sticker.
- **Media:** required. 1:1 or 4:5 feed, 9:16 Reels/Stories.

### Facebook Page
- Short post (~80–150 words) — long posts underperform; the link preview does the work.
- Inline link with short link + UTM. Local businesses: include city and a call button.
- **Lead mechanism:** link click, Messenger, call button.
- Media: image or video strongly preferred.

### LinkedIn
- 1,300–1,700 characters, first two lines above the "see more" fold, short paragraphs, no hashtag walls (3 max).
- Professional register — this is the one channel where the *tone_shift* is real: more specific, less exclamatory.
- **Link placement is a real trade-off:** inline links historically suppress reach; first-comment placement preserves it but costs clicks. Make it a per-business setting with the trade-off stated, not a silent choice.
- **Lead mechanism:** link click, DM, comment.
- Media: optional; a document carousel ("PDF post") performs well.

### TikTok
- **Video only — this is a script deliverable, not a post deliverable.** Output: 3-second hook, 20–40 s beat-by-beat script, on-screen text lines, trending-format suggestion, spoken voiceover text, plus 3–5 hashtags.
- **Link:** bio hub only.
- **Lead mechanism:** bio hub, comment-driven DM.
- Publishing: upload-to-drafts is the honest path until audited.

### YouTube
- **Long-form:** title ≤100 chars written for search intent, description with the value proposition and short link **in the first two lines** (the rest is truncated), chapters, tags, and a script outline.
- **Shorts:** the TikTok script deliverable, re-rendered 9:16.
- **This is the strongest channel for repurposing a blog post**, because the article outline already *is* a video outline.
- **Lead mechanism:** description link, pinned comment, end screen.
- Publishing: quota-bound — treat as Tier 2.

### Google Ads (assets only, never spend)
Generate and export, never launch:
- **Responsive Search Ad:** 15 headlines ≤30 chars, 4 descriptions ≤90 chars, with the required variety (brand / benefit / proof / CTA / local).
- Keyword list with match types, **plus a negative-keyword list** — which is the thing that actually saves the money, and the thing agencies most often skip.
- Sitelink, callout and structured-snippet extensions; final URL with UTM.
- Export as CSV / Google Ads Editor format for a human to review and launch.
- **Hard rule:** creating or funding a campaign is a human action behind an explicit money gate. The agent proposes; a person spends.

### Email
- Subject ≤~50 chars for mobile, preheader as a second hook, one CTA, plain-text alternative always generated.
- **Legal before creative:** consent basis recorded per recipient, unsubscribe in every send, sender identity, SPF/DKIM/DMARC on the sending domain. **Never send to a scraped or purchased list** — this is the one channel where a mistake is a fine, not a bad metric.
- **Lead mechanism:** the highest-converting channel we have, because the list is owned. Sequence: value email → case study → offer.

---

## 7. Video: what we honestly produce

TikTok, Reels and Shorts need video. This is a text and research engine. Three options, and I'd take the first:

| Option | What it means | Verdict |
|---|---|---|
| **Script + shot list + on-screen text** | they film it in ten minutes on a phone | **Ship this.** Honest, genuinely useful, zero cost, and phone-filmed footage outperforms polished stock on these platforms anyway |
| Slideshow-to-video | ffmpeg over their images + captions + optional TTS | Cheap Track B; fine for carousels and quote cards, weak as a Reel |
| AI-generated video | third-party generation | Costly, quality is inconsistent, and it reads as synthetic — which on TikTok is actively penalised by the audience |

**Never claim we produce video.** The deliverable is a *shootable script*. That framing survives contact with the customer; "AI video generation" does not.

---

## 8. What ships in the course build

| Channel | Track A | Track B |
|---|---|---|
| Blog / landing page → WordPress | **draft publish (Tier 1)** | full publish, more CMSs |
| Email | **generate + send via Resend** | sequences, segmentation |
| LinkedIn | **render + export pack** | personal API posting |
| Facebook | **render + export pack** | direct publish after App Review |
| Instagram | **render + export pack** (feed, Reel script, carousel) | direct publish after App Review |
| TikTok | **script deliverable** | upload-to-drafts |
| YouTube | **description + script deliverable** | metadata API, then uploads |
| Google Ads | **RSA assets + keywords + negatives, CSV export** | Ads API read-only reporting; spend stays human |
| **Short links + link hub** | **built — this is what makes it all measurable** | per-channel dashboards |

**Net for the demo:** every channel produces real, correctly-formatted, brand-consistent, validated content with tracked links — and two channels (own site, email) publish for real. That demonstrates the whole pipeline without a single third-party approval on the critical path.

---

## 9. Failure modes specific to publishing

| Scenario | Handling |
|---|---|
| Token expired / revoked | mark the channel disconnected, degrade to export pack, notify the owner — never silently drop a post |
| App Review rejected | the channel stays Tier 3; the product still works. No feature depends on approval |
| Platform changes a limit | `channel_specs` config edit, no redeploy; render-time validation catches it either way |
| Post rejected by the platform | store the provider error verbatim, surface it, keep the content editable and retryable |
| Media URL unreachable when Meta fetches it | pre-flight the public URL before calling the API; fail before publishing, not during |
| Same post published twice | idempotency key on the Actuator; a reconciler asks the platform before any retry |
| Rate limit hit (IG ~50/24 h, YouTube quota) | queue with a per-channel token bucket in Redis, shared across workers |
| Owner edits the caption after we scheduled it | the scheduled payload is a snapshot; an edit creates a new version and cancels the old job |
| Video required but absent | the channel renders a script, and the run reports "needs filming" rather than posting something broken |
