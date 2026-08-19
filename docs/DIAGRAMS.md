# Diagrams — the system, drawn

Every diagram in one place, in Mermaid so it renders on GitHub and stays
diffable. Design rationale lives in [ARCHITECTURE.md](ARCHITECTURE.md); the
agent internals are in [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

Each diagram has a **"what to notice"** line, because a diagram nobody can read
a conclusion from is decoration.

---

## 1. System context

```mermaid
flowchart TB
    subgraph people["People"]
        owner["SMB owner / marketer"]
        agency["Agency user<br/>many businesses"]
        visitor["Website visitor<br/>becomes a lead"]
    end

    subgraph platform["Growth Agent Platform"]
        web["Next.js UI<br/>user mode + developer mode"]
        api["FastAPI<br/>authn · tenancy · rate limit · budget"]
        workers["Workers<br/>agent runtime"]
        stores[("Postgres + pgvector<br/>Redis · object storage")]
    end

    subgraph external["External systems"]
        llm["LLM providers<br/>via OpenRouter"]
        serp["Search / SERP API"]
        answers["AI answer engines<br/>probed, not integrated"]
        cms["WordPress / CMS"]
        mail["Email provider"]
    end

    owner --> web
    agency --> web
    visitor -->|"public form POST<br/>no auth, no cookie"| api
    web --> api
    api --> stores
    api -->|"enqueue"| workers
    workers --> stores
    workers --> llm
    workers --> serp
    workers --> answers
    workers --> cms
    workers --> mail
```

**What to notice:** the lead visitor enters through a *different, unauthenticated
door* than the customer. That public form is the only route into the system that
carries no session, which is why it gets its own rate limit and its own minimal
frontend bundle.

---

## 2. The three component kinds

```mermaid
flowchart TB
    agents["<b>AGENTS</b> · the LLM decides<br/>plan · prioritise · interpret · write<br/><i>non-deterministic, evaluated</i>"]

    engines["<b>ENGINES</b> · read + compute<br/>crawl · parse · score · count<br/><i>no LLM · no DB · no side effects</i>"]

    actuators["<b>ACTUATORS</b> · external side effects<br/>publish · post · send<br/><i>idempotency key · approval · audit</i>"]

    guard{{"tests/test_engine_boundary.py<br/>fails the build on a forbidden import"}}

    agents -->|"read facts"| engines
    agents -->|"request action"| actuators
    engines --> readonly["read-only outside world<br/>HTML · documents · read APIs"]
    actuators --> writes["mutating outside world<br/>CMS · social · email"]
    guard -.->|"enforces"| engines
```

**What to notice:** agents can *request* an action but never perform one. The
boundary is enforced by a test, not a convention — if it rots, every guarantee
built on top of it silently stops being true.

---

## 3. Complete request flow — a content run

The main path through the system, end to end.

```mermaid
sequenceDiagram
    autonumber
    participant U as Browser
    participant A as FastAPI
    participant Q as Redis queue
    participant W as Worker
    participant G as LangGraph runtime
    participant E as Engines
    participant M as Model router
    participant D as Postgres

    U->>A: POST /api/v1/runs {business_id, goal, surfaces}
    A->>A: authn → resolve tenant → rate limit
    A->>D: monthly budget remaining?
    alt budget exhausted
        A-->>U: 402 with the reason
    end
    A->>D: INSERT runs (state=queued, cap=$0.50/14 steps)
    A->>Q: enqueue job(run_id)
    A-->>U: 202 {run_id}
    U->>A: GET /api/v1/runs/{run_id}/events  (SSE)

    Q->>W: dequeue
    W->>G: start(run_id)

    loop each node, bounded by step + cost cap
        G->>G: check caps BEFORE spending
        G->>D: persist run_event(node, started)
        A-->>U: SSE {node, started}

        alt engine node (HARVEST, VALIDATE)
            G->>E: crawl · kb · serp · geo · seo
            E-->>G: typed facts, or typed error
        else agent node (OPPORTUNITY, PLAN, GENERATE, REPACK)
            G->>M: resolve(task_class) → tier + fallback
            M-->>G: completion + token usage
            G->>D: INSERT model_usage(tokens, usd, latency)
        end

        G->>D: checkpoint AgentState  ← resume point
        G->>D: persist run_event(node, done)
        A-->>U: SSE {node, done, cost_so_far}
    end

    G->>D: runs.state = awaiting_approval
    A-->>U: SSE {awaiting_approval}
    Note over W: worker releases the job — nothing is held open<br/>while a human decides
```

**What to notice:** three things. The HTTP request returns in step 7, long before
the work is done — nothing long-running happens inside a request. Every SSE event
is *also written to Postgres*, so a browser reload replays the timeline instead of
showing an empty screen. And the worker is released at the approval point, so a
human taking two days costs no compute.

---

## 4. The agent state machine

```mermaid
stateDiagram-v2
    [*] --> INTAKE

    INTAKE --> HARVEST: DNA present
    INTAKE --> [*]: no business DNA — ask, never guess

    HARVEST --> OPPORTUNITY: facts gathered, partial acceptable

    OPPORTUNITY --> PLAN: opportunity chosen
    OPPORTUNITY --> [*]: none found — return the audit instead

    PLAN --> GENERATE: outline has a target keyword
    PLAN --> PLAN: outline rejected, retry once

    GENERATE --> CONVERT: the article is written
    CONVERT --> VALIDATE: landing page + one CTA per channel

    VALIDATE --> GENERATE: draft failed — score below 85 or a banned claim, max 2 loops
    VALIDATE --> CONVERT: only the landing page failed — the article is fine
    VALIDATE --> REPACK: passed

    REPACK --> REVIEW

    REVIEW --> EXPORT: approved
    REVIEW --> GENERATE: edits requested
    REVIEW --> [*]: rejected, reason feeds the feedback loop

    EXPORT --> MEASURE
    MEASURE --> [*]

    GENERATE --> PARTIAL: step or cost cap hit
    VALIDATE --> PARTIAL: still failing after 2 loops
    VALIDATE --> BLOCKED: a banned claim survived 2 loops
    BLOCKED --> [*]: publication_blocked — REVIEW is never reached, so it cannot be approved
    PARTIAL --> [*]: returned with a stated reason, never an infinite loop
```

**What to notice:** every exit is deliberate and named. There is no path that
loops forever and no path that fails silently — the two ways an autonomous system
usually burns money.

---

## 5. Inside one agent node — the tool-calling loop

This is the mechanism behind "function calling", drawn exactly as implemented.

```mermaid
flowchart TB
    start(["Node begins"]) --> ctx["Build context<br/>Business DNA + memory + prior facts"]
    ctx --> filter["Tool list = node allowlist ∩ business-enabled toggles"]
    filter --> call["Model call via router"]
    call --> kind{"Response"}

    kind -->|"tool_call"| valid{"Arguments valid against<br/>the Pydantic JSON schema?"}
    valid -->|"no"| repair["ONE repair turn,<br/>carrying the validation error verbatim"]
    repair --> valid2{"valid now?"}
    valid2 -->|"no"| skip["Skip the tool<br/>record in errors[]<br/>UI says which data is missing"]
    valid2 -->|"yes"| exec
    valid -->|"yes"| exec["Execute: timeout · traced · costed"]

    exec --> obs["Append result as a tool message"]
    skip --> obs
    obs --> caps{"step cap or<br/>budget exceeded?"}
    caps -->|"no"| call
    caps -->|"yes"| partial["Return partial output<br/>with a stated reason"]

    kind -->|"final answer"| shape["Validate against the node's output schema"]
    shape --> done(["Checkpoint · node ends"])
    partial --> done
```

**What to notice:** validation happens **before** execution, so a tool never sees
malformed input. And a failed tool degrades the run rather than ending it — the
node continues with less evidence and says so.

---

## 6. Agentic RAG — why it is agentic, not a retriever

```mermaid
flowchart TB
    need{"Does this section need<br/>facts about the business?"}
    need -->|"no"| gen_plain["Generate from the plan alone"]
    need -->|"yes"| rewrite["Rewrite the query for retrieval<br/>not the user's words"]

    rewrite --> search["kb.search over pgvector"]
    search --> grade["Grade each chunk:<br/>relevant / partial / irrelevant"]
    grade --> enough{"Enough relevant evidence?"}

    enough -->|"no, attempt < 2"| widen["Rewrite differently or widen"]
    widen --> search
    enough -->|"no, attempts exhausted"| web["Fall back to web_search"]
    web --> gen_cited
    enough -->|"yes"| gen_cited["Generate, citing chunk ids"]

    gen_cited --> check{"Every claim supported?"}
    check -->|"no"| drop["Drop the claim.<br/>Never invent a source."]
    check -->|"yes"| out(["Section complete"])
    drop --> out
    gen_plain --> out
```

**What to notice:** four decisions a plain retriever cannot make — *whether* to
retrieve, *what* to ask, *whether the result is good enough*, and *what to do when
it isn't*. That loop is the whole difference.

---

## 7. Model routing and the budget guard

```mermaid
flowchart LR
    node["Agent node"] --> router["ModelRouter.resolve<br/>(task_class, business_policy)"]
    router --> tier{"Task class"}

    tier -->|"classify · extract · repack"| cheap["cheap tier"]
    tier -->|"plan · prioritise"| mid["mid tier"]
    tier -->|"long-form generation"| strong["strong tier"]
    tier -->|"embeddings"| embed["embedding model"]

    cheap --> guard
    mid --> guard
    strong --> guard
    embed --> guard

    guard{"Budget remaining?"} -->|"no"| refuse["Refuse BEFORE spending<br/>run ends with a stated reason"]
    guard -->|"yes"| adapters

    subgraph adapters["Provider adapters — the abstraction is proven by two"]
        or["OpenRouter"]
        direct["Anthropic direct"]
    end

    adapters --> chain["model A → fallback B → fallback C"]
    chain --> ledger[("model_usage<br/>tokens · usd · latency · prompt_version")]
```

**What to notice:** the guard sits *before* the provider call, not after. Checking
cost once the tokens are spent is accounting, not control.

---

## 8. Approval and publishing — the actuator sequence

```mermaid
sequenceDiagram
    autonumber
    participant U as Owner
    participant A as FastAPI
    participant S as Actuator service
    participant D as Postgres
    participant X as CMS / social / email

    U->>A: POST /api/v1/content/{id}/approve
    A->>D: record approval + mint approval token
    A->>S: resume graph → EXPORT

    S->>D: resolve policy(business, action_type)
    alt policy needs approval and token missing or invalid
        S-->>A: refused_needs_approval
    end

    S->>D: SELECT WHERE idempotency_key = business:workflow:task:action
    alt already succeeded
        S-->>A: replayed — returns the FIRST result, no second publish
    end

    S->>D: INSERT actions(status=in_flight)
    Note over S,D: insert BEFORE the call — the unique index is the lock
    S->>X: publish (timeout, bounded retry)
    X-->>S: external_ref

    S->>D: UPDATE actions(status=succeeded, external_ref)
    S->>D: INSERT audit_log
    S-->>A: executed
    A-->>U: published + link

    Note over S,X: crash between the call and the update leaves in_flight;<br/>the reconciler ASKS the provider what happened,<br/>it never blind-retries
```

**What to notice:** the ordering is the design. Insert-then-call means an
interrupted publish is *detectable*; call-then-insert would make it invisible, and
the retry would double-publish.

---

## 9. Lead capture and attribution

```mermaid
flowchart TB
    content["Content piece<br/>article · social post · email"] --> cta["CTA with a short link<br/>/l/{code}"]

    cta --> ig["Instagram / TikTok<br/><b>captions carry no clickable link</b>"]
    cta --> inline["LinkedIn · Facebook · YouTube · Email<br/>inline link"]

    ig --> hub["Link hub /go/{business}"]
    hub --> short
    inline --> short["Short-link service<br/>302 + record channel, piece, campaign"]

    short --> landing["Landing page with a form"]
    landing --> post["POST /public/forms/{id}<br/>honeypot · rate limit · strict schema"]
    post --> lead[("leads<br/>content_piece_id · utm · fields")]
    lead --> notify["Instant notification<br/>email + webhook"]
    lead --> inbox["Lead inbox<br/>attributed to the piece that caused it"]
    inbox --> loop["Feeds the next opportunity"]
```

**What to notice:** the short-link service is ours, so attribution works even on
channels we cannot publish to. **Attribution is decoupled from publishing** — that
is what makes an export-only channel a fully measurable one.

---

## 10. One message spine, many channel renderings

```mermaid
flowchart TB
    research["ONE research pass<br/>crawl · kb · serp · geo"] --> spine

    spine["<b>MESSAGE SPINE</b> — generated once, strong model<br/>claim · proof · audience · intent<br/>objection · cta_goal · key_facts · entities"]

    spine --> A["A · Article<br/>Google organic"]
    spine --> B["B · Landing page<br/>commercial intent"]
    spine --> C["C · LinkedIn post"]
    spine --> D["D · Short social<br/>Facebook + Instagram"]
    spine --> E["E · Carousel<br/>Instagram + LinkedIn doc"]
    spine --> F["F · Short-video script<br/>TikTok + Reels + Shorts"]
    spine --> G["G · Video metadata<br/>YouTube"]
    spine --> H["H · Email"]
    spine --> I["I · Ads asset pack<br/>+ negative keywords"]

    A --> val
    B --> val
    C --> val
    D --> val
    E --> val
    F --> val
    G --> val
    H --> val
    I --> val

    val["DETERMINISTIC VALIDATION from channel_specs<br/>length · hashtags · link mechanism · banned claims · reading level"]
    val -->|"fail"| single["Regenerate THAT channel only"]
    val -->|"pass"| review(["Human review"])
    single --> val
```

**What to notice:** nine artifacts, one decision about what is being said. F is a
single renderer serving three platforms — rewriting per platform would let the
model re-decide the point each time, which is how brand voice drifts.

---

## 11. Data model

```mermaid
erDiagram
    USERS ||--o{ BUSINESSES : owns
    BUSINESSES ||--o{ DOCUMENTS : has
    DOCUMENTS ||--o{ KB_CHUNKS : "chunked into"
    BUSINESSES ||--o{ CRAWL_PAGES : "crawled from"
    BUSINESSES ||--o{ KEYWORDS : targets
    BUSINESSES ||--o{ COMPETITORS : "compared with"
    BUSINESSES ||--o{ GEO_PROMPTS : probes
    GEO_PROMPTS ||--o{ GEO_RESULTS : "answered by"
    BUSINESSES ||--o{ OPPORTUNITIES : ranks
    OPPORTUNITIES ||--o{ CONTENT_PIECES : produces
    CONTENT_PIECES ||--o{ SOCIAL_POSTS : "repacked into"
    CONTENT_PIECES ||--o{ LEADS : attributed
    CONTENT_PIECES ||--o{ FEEDBACK : rated
    BUSINESSES ||--o{ RUNS : executes
    RUNS ||--o{ ACTIONS : "side effects"
    RUNS ||--o{ MODEL_USAGE : costs
    ACTIONS ||--o| APPROVALS : gated
    BUSINESSES ||--o| LEARNED_STYLE : remembers

    BUSINESSES {
        uuid id PK
        uuid owner_id FK
        string name
        string industry
        jsonb dna "voice, services, banned claims"
    }
    RUNS {
        uuid id PK
        uuid business_id FK
        string state
        jsonb plan_completed_pending
        numeric budget_usd_used
        int resumed_count
    }
    ACTIONS {
        uuid id PK
        string idempotency_key UK "business:workflow:task:action"
        string status "in_flight|succeeded|failed"
        string external_ref
    }
    CONTENT_PIECES {
        uuid id PK
        string surface
        int seo_score
        string status "draft|approved|published"
    }
    LEADS {
        uuid id PK
        uuid content_piece_id FK
        jsonb utm
    }
```

**What to notice:** every business-scoped table carries `business_id`, and
`actions.idempotency_key` is unique-indexed — the isolation guarantee and the
no-double-publish guarantee are both *schema* properties, not code conventions.

---

## 12. Deployment

```mermaid
flowchart TB
    cf["Cloudflare"] --> caddy["Caddy · TLS"]

    caddy --> web["web · Next.js<br/>1 replica"]
    caddy --> api["api · FastAPI<br/>2 replicas, stateless"]
    caddy -->|"/public/forms/*<br/>own rate limit"| api
    caddy -.->|"/api/internal/* → 404"| blocked["never routable<br/>from the internet"]

    api --> pg[("Postgres 16<br/>+ pgvector")]
    api --> redis[("Redis")]
    api --> s3[("Object storage")]

    subgraph pools["Worker pools — separated so a slow crawl cannot starve content"]
        wc["worker-content<br/>LLM-heavy · concurrency 4"]
        wh["worker-harvest<br/>network-bound · concurrency 8"]
        sch["scheduler<br/>1 replica only"]
    end

    redis --> wc
    redis --> wh
    pools --> pg
    pools --> s3
    pools --> lf["Langfuse<br/>traces · cost · scores"]
```

**What to notice:** `scheduler` is deliberately a single replica — its jobs are
not concurrency-safe. And the internal endpoints are 404'd at the edge, so the
token is defence *in depth*, not the only defence.

---

## 13. Degradation — what happens when something fails

```mermaid
flowchart TB
    fail{"What failed?"}

    fail -->|"provider 429 / 5xx"| back["Backoff ×3 → fallback model<br/>→ fail the NODE, not the run"]
    fail -->|"malformed tool args"| rep["One repair turn → skip the tool<br/>→ record in errors[]"]
    fail -->|"SERP quota gone"| kb["Degrade to kb only<br/>UI: 'generated without live research'"]
    fail -->|"competitor page JS-only"| src["Skip the source<br/><b>never fabricate a citation</b>"]
    fail -->|"SEO score stuck < 85"| hint["Return draft + explicit<br/>'needs human edit' list"]
    fail -->|"injection in crawled text"| env["Data envelope holds<br/>UI reports it was ignored"]
    fail -->|"worker crash"| res["Resume at the failed node<br/>resumed_count++"]
    fail -->|"publish retried after<br/>unrecorded success"| idem["Idempotency key returns<br/>the first result"]
    fail -->|"cost / step cap"| part["Terminate with partial output<br/>+ stated reason"]

    back --> honest
    rep --> honest
    kb --> honest
    src --> honest
    hint --> honest
    env --> honest
    res --> honest
    idem --> honest
    part --> honest

    honest(["Always: the user is told what is missing.<br/>Never: a silent partial result."])
```

**What to notice:** every branch converges on the same rule. Degrading is fine;
degrading *quietly* is not.

---

## 14. Multi-agent topology — Track B, only when justified

```mermaid
flowchart TB
    subgraph now["Now — single agent, many tools"]
        one["One agent<br/>one context, one prompt lineage"] --> tools["9 tools"]
    end

    subgraph later["Track B — supervisor, once expertise genuinely diverges"]
        mgr["Growth manager<br/>decides what happens next"]
        mgr --> seo["SEO agent"]
        mgr --> content["Content agent"]
        mgr --> geo["GEO agent"]
        mgr --> local["Local agent"]
        seo --> se["seo + serp engines"]
        content --> ce["kb engine"]
        geo --> ge["geo engine"]
        local --> le["NAP + GBP engines"]
    end

    now -->|"split ONLY when expertise, permissions,<br/>context, model tier, or eval criteria diverge"| later
```

**What to notice:** the split condition is written down so it becomes a decision
rather than a fashion. Twenty-five agents talking to each other is not an
architecture, it is a debugging problem.
