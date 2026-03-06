# Autocache Borrow Plan for Kiro Gateway

## Goal

Borrow only the parts of `autocache` that improve Anthropic-compatible
prompt-caching behavior for a black-box Kiro upstream.

Do not copy assumptions that only make sense when the upstream is the official
Anthropic API.


## Current Reality

Kiro Gateway now has three cache layers:

1. Exact response cache
2. Read-only tool result cache
3. Anthropic prompt-cache compatibility layer

The third layer currently tracks prompt-cache-compatible request segments and
returns Anthropic-style usage fields, but it does not make upstream Kiro skip
prefill work.


## What Autocache Gets Right

### 1. Request shaping for Anthropic prompt caching

Useful ideas:

- Convert `system: "string"` into Anthropic content-block form when caching is
  needed.
- Treat cacheable prefix material in three groups:
  - system
  - tools
  - message content
- Apply `cache_control` only to the last element of a cacheable array segment.
- Use longer TTL for stable prefix material:
  - system -> `1h`
  - tools -> `1h`
  - dynamic content -> `5m`

Relevance to Kiro Gateway:

- This directly improves Anthropic compatibility at the request boundary.
- This can be implemented without changing upstream Kiro behavior.


### 2. Anthropic-native request model handling

Useful ideas:

- Preserve both `system` formats:
  - plain string
  - array of text blocks with `cache_control`
- Keep marshal/unmarshal logic explicit so the proxy can round-trip Anthropic
  payloads cleanly.

Relevance to Kiro Gateway:

- This reduces lossy request normalization.
- It lets the gateway keep cache semantics separate from Kiro payload building.


### 3. Simple TTL policy

Useful ideas:

- Stable prompt prefix should default to `1h`.
- Volatile content should default to `5m`.

Relevance to Kiro Gateway:

- Our prompt-cache compatibility layer should move from one generic TTL policy
  to segment-based TTL policy.


## What Not To Borrow

### 1. Anthropic-upstream assumption

Autocache injects `cache_control` and forwards to Anthropic. That works because
the upstream understands native prompt caching.

Kiro Gateway cannot assume that. Upstream Kiro is a black box.

Conclusion:

- Do not pretend prompt-cache injection alone produces real upstream prefill
  savings.
- Keep a strict separation between:
  - request compatibility
  - local cache accounting
  - true upstream reuse


### 2. Marketing-style ROI as product logic

Autocache exposes strong ROI headers and scoring, but the current code path is
mostly deterministic ordering with limited real optimization.

Conclusion:

- Do not add ROI theater to core logic.
- If we expose analysis headers, keep them explicitly informational.


### 3. Token-heavy candidate ranking as a first priority

Autocache talks about ROI scoring, but the highest-value behavior for Kiro
Gateway is still deterministic prefix grouping.

Conclusion:

- First finish correct segment semantics.
- Only add breakpoint ranking if we later see real ambiguity.


## Recommended Borrow Order

### Phase 1: Finish Anthropic request compatibility

Status: partially done

Tasks:

1. Preserve cacheable `system` blocks end-to-end in Anthropic-facing request
   handling.
2. Keep prompt-cache semantics outside Kiro payload conversion.
3. Ensure response `usage` always reflects compatibility-layer cache state.

Success criteria:

- Claude CLI style requests round-trip without losing cache annotations at the
  Anthropic boundary.
- Usage fields match observed local prompt-cache compatibility state.


### Phase 2: Segment-aware TTL policy

Status: not fully done

Tasks:

1. Add per-segment TTL handling to the prompt-cache layer:
   - system -> `1h`
   - tools -> `1h`
   - content -> `5m`
2. Respect incoming explicit `ttl` when present.
3. Keep metadata out of prompt-cache keys unless real evidence shows it affects
   cache isolation.

Success criteria:

- System-only changes invalidate only system cache.
- Tool-definition changes invalidate tool cache without destroying unrelated
  segment hits.


### Phase 3: Optional request-side cache-control injection

Status: not started

Tasks:

1. Add an optional Anthropic request enhancer that can inject missing
   `cache_control` blocks for compatible requests.
2. Limit this to Anthropic-compatible routes only.
3. Keep this configurable and off by default unless we intentionally decide the
   gateway should be opinionated.

Success criteria:

- Clients that do not know Anthropic prompt caching can still get compatible
  request shaping.
- User content is not rewritten beyond cache-control annotation.


### Phase 4: Informational analytics headers

Status: not started

Tasks:

1. Add lightweight debug headers for prompt-cache segment decisions.
2. Report:
   - which segments were evaluated
   - which segments were hit or created
   - total compatibility-layer cached tokens
3. Keep these separate from exact-response-cache headers.

Success criteria:

- Debugging cache behavior does not require reading server logs.
- Headers remain clearly descriptive, not misleading about upstream savings.


## Kiro-Specific Constraints

These rules should remain in force:

- The gateway must stay transparent first.
- The gateway must not silently rewrite conversation meaning.
- Prompt-cache compatibility must never break exact request forwarding.
- Any enhancement that changes request structure should remain small,
  explainable, and test-covered.


## Concrete Next Changes

Short list of high-value follow-ups:

1. Upgrade `kiro.prompt_cache` to assign segment TTL by content class.
2. Add tests for explicit `ttl: "1h"` and default `5m` behavior.
3. Add optional request-side cache-control auto-injection for Anthropic route.
4. Add prompt-cache debug headers that explain segment decisions.


## Non-Goals

The following are out of scope for this borrow plan:

- Recreating Anthropic true upstream prompt caching on top of black-box Kiro
- Copying Autocache ROI dashboards
- Building token-optimizer heuristics before compatibility is solid
- Replacing exact response cache or tool-result cache


## Final Position

Autocache is most useful to Kiro Gateway as a source of:

- Anthropic request-shaping patterns
- segment taxonomy
- TTL policy defaults

Autocache is not a model for:

- true upstream caching behavior
- local Redis cache architecture
- proof of real Kiro-side prefill reuse
