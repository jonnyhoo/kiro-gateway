# Lynkr Borrow Plan for Kiro Gateway

## Goal

Extract the useful ideas from `Lynkr` without copying its terminology
confusion.

In particular, separate these concerns clearly:

1. exact proxy-side response reuse
2. provider cache hit-rate improvement
3. semantic/fuzzy answer reuse


## What Lynkr Actually Implements

`Lynkr` talks about "cache" as if it were one feature, but the repository
contains three different mechanisms with different tradeoffs.


### 1. `src/cache/prompt.js` is exact response cache

What it does:

- stores full responses in SQLite
- keys by exact request shape
- defaults to `PROMPT_CACHE_TTL_MS=300000`
- only stores safe final responses
- avoids tool-calling/intermediate turns

What it does not do:

- it does not create true provider-side prompt caching
- it does not skip upstream prefill unless the proxy fully serves the request
- it is not Anthropic-native prompt cache behavior

Best mapping in Kiro Gateway:

- equivalent to our `kiro/response_cache.py`

Conclusion:

- useful as validation that the common "5 minute cache" story often means
  proxy-side response reuse, not provider-native prompt caching


### 2. `src/cache/semantic.js` is fuzzy response cache

What it does:

- embeds the prompt
- looks for semantically similar prior prompts
- requires same conversation-context hash
- defaults to `ttlMs=3600000`

What it risks:

- false positives
- stale or context-inappropriate answers
- unsafe reuse in tool-heavy agent loops

Best mapping in Kiro Gateway:

- no direct equivalent today, and that is good

Conclusion:

- do not borrow this for `kiro-gateway`
- a transparent proxy should not silently replace fresh model output with a
  "similar enough" prior answer


### 3. `headroom` is request shaping, not a cache store

What it does:

- compresses context before the upstream call
- includes a documented "Cache Aligner" concept
- tries to stabilize volatile content like UUIDs and timestamps
- includes CCR storage with a default 300-second TTL for compressed payload
  retrieval
- imports the real transform implementation from the external `headroom-ai`
  package rather than keeping it in this repository

What it does not do:

- it is not the same thing as `src/cache/prompt.js`
- its 5-minute CCR TTL is not a whole-response cache TTL
- it does not magically make a black-box upstream support Anthropic prompt
  caching

Best mapping in Kiro Gateway:

- conceptually adjacent to our prompt-cache compatibility layer
- partially adjacent to our tool-result cache when large read-only tool output
  is the volatility source

Conclusion:

- the only genuinely interesting part here is the cache-alignment idea


## Key Takeaway for the "5 Minute TTL" Question

`Lynkr` shows that "5 minute cache" can mean multiple unrelated things:

1. exact full-response cache for identical requests
2. compressed-content retrieval TTL
3. provider cache hit-rate improvement through request stabilization

These are not interchangeable.

For `kiro-gateway`, our current Redis layers map like this:

1. `RedisResponseCache` -> same class of idea as `Lynkr` `prompt.js`
2. `RedisToolResultCache` -> local reuse of read-only tool observations
3. `RedisPromptCache` -> Anthropic-style prompt-cache compatibility accounting

So our current architecture is already more explicit than `Lynkr`'s naming.


## What To Borrow

### 1. Better cache taxonomy in docs and debugging

Borrow:

- always name the cache type explicitly
- do not say "cache hit" without saying which cache
- expose separate metrics/headers for:
  - exact response cache
  - tool-result cache
  - prompt-cache compatibility

Why it matters:

- it prevents the exact confusion that happened here around "5-minute cache"


### 2. Volatility-aware request analysis

Borrow:

- inspect tool results and prompt blocks for volatile values such as:
  - UUIDs
  - timestamps
  - request IDs
  - ephemeral paths or temp filenames

Best first use in `kiro-gateway`:

- analysis/debug only
- surface why a request likely missed prompt-cache compatibility reuse

Why not full rewrite yet:

- silent content rewriting conflicts with the proxy's transparency goals
- we should understand the miss causes before mutating payloads


### 3. Conservative cache-store boundaries

Borrow:

- only exact-cache safe final responses
- never cache intermediate tool-use turns as final completions
- keep agentic flows out of broad response cache paths

Status in `kiro-gateway`:

- already aligned


## What Not To Borrow

### 1. Semantic cache for agentic traffic

Do not borrow:

- embedding-based fuzzy response reuse

Reason:

- too risky for tool-heavy Kiro and Claude Code usage
- breaks the transparency contract
- makes failures hard to diagnose


### 2. Heavy sidecar architecture as the first move

Do not borrow:

- Docker-managed compression sidecar
- extra service just to get cache-alignment behavior

Reason:

- too much operational surface area for too little immediate value
- we can implement lighter-weight observability and normalization locally first


### 3. Marketing language that conflates caches

Do not borrow:

- presenting exact response cache as if it were provider-native prompt caching

Reason:

- the user needs to know whether we saved an upstream call, improved upstream
  prefix reuse, or only returned a local cached answer


## Recommended Borrow Order

### Phase 1: Finish current prompt-cache layer correctly

Tasks:

1. make segment TTL defaults explicit:
   - system -> `1h`
   - tools -> `1h`
   - message text -> `5m`
2. keep debug output clear about segment hits vs misses
3. keep metadata excluded unless evidence proves otherwise

Why first:

- this improves the system we already built
- it directly addresses Anthropic-compatible behavior at the gateway boundary


### Phase 2: Add volatility diagnostics

Tasks:

1. detect volatile patterns in cacheable prompt segments
2. expose lightweight debug headers or logs such as:
   - `volatile_uuid`
   - `volatile_timestamp`
   - `volatile_temp_path`
3. report which segment was destabilized

Why second:

- this gives practical insight without silently rewriting user content


### Phase 3: Consider minimal deterministic stabilization

Possible future scope:

- normalize obviously non-semantic volatile substrings in local cache-key
  analysis only
- or add a narrowly-scoped Anthropic-compatible stabilizer for tool-result text

Guardrails:

- no hidden semantic changes to forwarded user content
- any rewrite must be small, deterministic, and test-covered
- keep the stabilized form separate from the forwarded payload unless we choose
  to become more opinionated


## Final Position

`Lynkr` is useful mostly as a cautionary example plus one good idea.

Useful to borrow:

- strict cache taxonomy
- conservative exact-response cache boundaries
- volatility analysis inspired by Cache Aligner

Not useful to borrow:

- semantic answer cache
- terminology that calls every optimization "prompt cache"
- heavy sidecar orchestration as the first implementation step

The best immediate path for `kiro-gateway` is still:

1. finish segment-aware Redis prompt-cache compatibility
2. add better cache diagnostics
3. only then evaluate whether a minimal cache stabilizer is worth adding
