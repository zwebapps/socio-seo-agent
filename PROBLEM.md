# The problem, the users, and why this is worth building

Three documents point here for three graded criteria — clear agent purpose, why it is
useful, and who it is for — and this one had been referenced without being written.
Everything below is drawn from [docs/FEATURES.md](docs/FEATURES.md) §0–1 and
[docs/ROADMAP.md](docs/ROADMAP.md); nothing here is new claim-making.

---

## 1. The problem, in one sentence

A small business pays for content and cannot tell whether any of it produced a
customer — so it either keeps paying on faith or stops, and in both cases the money
was not spent on the thing that would have worked.

## 2. Why that happens, precisely

A lead needs five links in a chain, and breaking any one makes the leads zero no
matter how good the others are:

```
REACH ──► RELEVANCE ──► CONVERSION ──► ATTRIBUTION ──► COMPOUNDING
  │            │             │              │               │
someone     it matches    there is a     we know which    we do more
finds       what they     way to ask     content caused   of what worked
them        wanted        for it         it
```

**The tools in this market sell link 1 only.** "30 articles a month" is a REACH
product. A business that publishes 30 articles with no CTA, no form and no
attribution gets some traffic, no leads, and churns — having concluded, reasonably,
that content marketing does not work for them.

Two consequences shape this entire product, and both are counter-intuitive enough to
state plainly:

- **The fastest leads are not from new content.** They come from fixing CONVERSION on
  pages that already get traffic. New Google content takes 6–12 weeks to rank at
  best. So the build order is deliberately conversion → social + lead magnet → AI
  answers → *then* Google content, which is the reverse of how these tools are
  normally sold.
- **Attribution is not a reporting feature, it is the anti-churn feature.** A lead
  inbox that names the content piece that earned each lead is what makes the customer
  believe the leads are real. That belief is the difference between renewing and
  cancelling, and it is why attribution here is deliberately decoupled from
  publishing — the short link works whether we posted the caption or the owner pasted
  it by hand.

## 3. Who it is for

| User | What they actually have | What they need from this |
|---|---|---|
| **SMB owner** (primary) — a plumber, dentist, bakery, tax adviser | No marketing person. A website somebody built years ago. Existing documents (price lists, service sheets) that already contain the answers customers ask for. Minutes, not hours. | Something that reads their own material, produces publishable content, and tells them in plain numbers whether it earned a phone call. |
| **In-house marketer** at a small company | Owns five channels and has time for one. Can judge copy but cannot write for all of them weekly. | Volume with a review gate, and per-channel variants that are actually shaped for each channel rather than one post copy-pasted five times. |
| **Small agency** | Ten clients, each needing the same work, margin destroyed by doing it manually. | Multi-tenant isolation they can trust, and an attribution report they can put in front of a client. |

The common thread: **none of them can be asked to trust the output blindly, and none
of them has time to check everything.** That is the requirement that produced the
human-approval gate, the deterministic scoring, and the refusal to publish a piece
carrying a regulated claim — not a preference for caution.

## 4. Why an agent, rather than a script or a chat window

A script cannot do it because the work is a *decision sequence* that depends on what
it finds: which opportunity is worth writing about depends on the crawl, the
competitors and the keyword intent; whether to retrieve more depends on whether what
came back was relevant. A chat window cannot do it because the work is long,
resumable, and has to be *measured* — and because a human cannot be in the loop for
every one of a hundred steps.

What is genuinely an agent decision here is narrow, and the architecture is built
around keeping it narrow:

> **If the answer is computable, compute it. Only ask a model to decide, interpret,
> or write.**

So counting characters, scoring on-page SEO, checking a hashtag limit and matching a
banned claim are all Python, not prompts — deterministic, free, and testable. The
model chooses opportunities, interprets documents, and writes copy. That split is
enforced by a test that walks the imports, not by convention.

## 5. What would make this a failure

Stated here because the honest version of "why it is useful" needs the opposite too:

- If it produces content nobody publishes, it is a REACH tool with extra steps.
- If the attribution is wrong even once in a visible way, the customer stops trusting
  every number in the product, and the anti-churn mechanism inverts.
- If it publishes a regulated claim on a dentist's or tax adviser's behalf, the cost
  to that customer is far larger than the subscription.
- If the numbers it reports flatter it — a share-of-voice that counts a model outage
  as an absence, a quality score produced against canned responses — then the product
  is measuring itself and the customer is the one who finds out.

Each of those has a specific control, and each control has a test. See
[docs/CRITERIA_MAP.md](docs/CRITERIA_MAP.md) for which, and
[BACKLOG.md](BACKLOG.md) for what is still open — including the weaknesses this
project's own tooling found rather than the ones that were easy to anticipate.
