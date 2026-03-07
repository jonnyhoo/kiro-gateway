# Kiro Gateway Endpoint Investigation

Date: 2026-03-07

Model under test: `claude-sonnet-4.6`

Direct endpoints used:

- internal gateway: `http://127.0.0.1:8000`
- public reverse proxy: `https://api.10010074.xyz/kiro`

Scope:

- direct `kiro-gateway` endpoint behavior only
- no `sub2api`
- primary focus on Anthropic-compatible `/v1/messages`, because that is the
  path Claude Code depends on


## Executive Summary

This endpoint is not a raw Anthropic-compatible Claude endpoint.

It behaves like a Kiro-backed Anthropic facade with:

- a large hidden upstream prefix/environment cost
- local exact-response cache
- local prompt-cache compatibility accounting
- local read-only tool-result hydration
- multiple Anthropic compatibility gaps that matter in agentic usage

The most important live findings are:

1. Tiny requests carry a hidden upstream input overhead of about `+2042`
   tokens even when `/v1/messages/count_tokens` says the request is only `16`
   input tokens.
2. `max_tokens=1` is ignored in practice.
3. `tool_choice` is ignored in practice.
4. `stop_sequences` are not enforced in practice.
5. Long-document reading works reliably up to about `131k` user-side
   `count_tokens`, then fails hard around `150k` user-side `count_tokens`.
6. Plain long text output works up to at least `2000` numbered lines, but a
   `3000`-line request silently caps at `2048` lines with `stop_reason=end_turn`.
7. `Write` tool payload generation is much more fragile than plain text output:
   `1500`- and `1600`-line content survived, but `1700+` lines degraded into an
   empty `{}` tool input while still returning `stop_reason=tool_use`.
8. Prompt-cache headers are local telemetry, not proof of real upstream Kiro
   prompt caching.
9. The deployed debug hooks were broken by runtime directory permissions until
   the debug directory was temporarily chmodded writable for this investigation.


## Test Setup

Runtime facts during investigation:

- server repo path: `/root/kiro-gateway`
- runtime dir: `/opt/kiro-gateway/runtime`
- tested server commit before this report write: `7458f7c`
- direct gateway health was green before and after testing

Method:

- black-box live requests against the internal gateway
- one equivalence check against the public `/kiro` reverse-proxy path
- white-box inspection of the gateway code at
  `E:\VIBE_CODING_WORK\kiro-gateway`
- one controlled `DEBUG_MODE=all` capture to inspect the generated Kiro request


## Live Findings

### 1. Hidden upstream prompt/environment overhead is real

Baseline request:

- request: one short user message, `Reply with exactly OK`
- gateway `/v1/messages/count_tokens`: `16`
- actual `/v1/messages` response usage `input_tokens`: `2058`
- implied hidden overhead: about `+2042` tokens

This means user-visible Anthropic input size and upstream Kiro/Claude input size
are not the same thing.

With one small tool:

- gateway `/v1/messages/count_tokens`: `87`
- actual `/v1/messages` response usage `input_tokens`: `2711`

So the hidden envelope grows further when tools are involved.


### 2. Exact response cache is real and very fast

Same non-streaming request twice:

- run 1: `x-kiro-gateway-cache=miss`, about `4.0s`
- run 2: `x-kiro-gateway-cache=hit`, about `0.008s`

This is a true local proxy-side exact response cache.


### 3. Prompt-cache headers are local accounting, not proof of upstream reuse

Observed:

- plain Anthropic requests without explicit `cache_control` still returned
  `x-kiro-gateway-prompt-cache-source=shadow`
- explicit `cache_control` requests returned segment-level headers like:
  - `system[0]:miss:1h:74002`
  - `system[0]:hit:1h:74002`

Latency test with the same `~74k` token stable prefix and different suffixes:

- explicit markers, first request: about `5.566s`
- explicit markers, second/third request: about `3.763s`, `3.513s`
- fresh no-explicit control prefix, first request: about `6.459s`
- fresh no-explicit control prefix, second request: about `5.320s`

Interpretation:

- there is some warm-path latency reduction
- but a similar effect appears without explicit `cache_control`
- the code path says the local prompt-cache layer does not modify the upstream
  Kiro request
- therefore this is not enough evidence to claim real provider-side prompt
  caching inside Kiro

Practical conclusion:

- treat `x-kiro-gateway-prompt-cache*` as local gateway telemetry only
- do not assume they prove real upstream prefill reuse


### 4. Tool calling works, including parallel calls

Single-tool test:

- tool: `echo_args`
- prompt requested tool use only
- result: `stop_reason=tool_use`
- returned correct structured args:
  - `city=Shanghai`
  - `unit=C`

Parallel tool test:

- tools: `get_weather`, `get_time`
- prompt asked to use both in one turn
- result: `stop_reason=tool_use`
- returned `2` tool calls in one response

So the underlying model/tool path can do multi-tool planning in a single turn.


### 5. Multi-step read/write agent loop works, but only when tool payloads stay modest

Simulated file-edit loop:

1. user asked to change `/app/main.py` from `print(1)` to `print(2)`
2. model called `Read` with `/app/main.py`
3. supplied tool result `print(1)`
4. model called `Write` with `print(2)\n`
5. supplied success tool result
6. model finalized correctly

This is compatible with a basic Claude Code style read-then-write loop.


### 6. Read-only tool-result cache really hydrates missing tool results, but only inside the same scope

Probe design:

- seed request with tool history for `Read /tmp/demo.txt` and actual tool result
- second request reused same scope but sent empty tool result content
- third request used a different scope and sent empty tool result content

Observed:

- seed request: `x-kiro-gateway-tool-cache=miss`
- same scope + empty tool result: `x-kiro-gateway-tool-cache=reused`
- different scope + empty tool result: `x-kiro-gateway-tool-cache=bypass`

So the gateway can inject previously observed read-only tool output into a later
request when scope matches. That is useful, but it is also gateway-side content
injection rather than raw passthrough.


### 7. Long-document read limit is much lower than “raw user tokens” suggest

Synthetic document read test:

| Sections | `/count_tokens` input | Actual result |
|----------|-----------------------|---------------|
| 1000 | 17027 | success |
| 4000 | 74027 | success |
| 5000 | 93027 | success |
| 6000 | 112027 | success |
| 7000 | 131027 | success |
| 8000 | 150027 | HTTP 400, context limit reached |

At `7000` sections:

- gateway `count_tokens`: `131027`
- actual response usage `input_tokens`: `196068`
- answer was still correct for the final marker

At `8000` sections:

- HTTP `400`
- error: `Model context limit reached. Conversation size exceeds model capacity.`

Practical reading boundary for this deployment:

- roughly safe through `~131k` user-side `count_tokens`
- fails around `~150k` user-side `count_tokens`

Because of the hidden upstream envelope, you should leave meaningful headroom
below the failure point.


### 8. Plain long text output has a silent ceiling

Plain-text numbered output test:

| Requested lines | Result |
|-----------------|--------|
| 1000 | exact success |
| 2000 | exact success |
| 3000 | silently stopped at `2048` lines |

For the `3000`-line case:

- `stop_reason=end_turn`
- `output_tokens=9419`
- output ended at `OUT2048`

This is important:

- the request did not return `max_tokens`
- the request did not return an explicit truncation error
- the model/output path simply stopped early

So plain long-form writes can truncate silently.


### 9. Large `Write` tool payloads are much more fragile than plain text output

`Write` tool generation test:

| Requested content lines | Result |
|-------------------------|--------|
| 500 | exact success |
| 1500 | exact success |
| 1600 | exact success |
| 1700 | `tool_use` returned with empty `{}` input |
| 2000 | `tool_use` returned with empty `{}` input |
| 2500 | `tool_use` returned with empty `{}` input |

Important details:

- at `1600` lines:
  - `Write.input.content` length was `12799`
  - last line was `ROW1600`
- at `1700+` lines:
  - `stop_reason` still said `tool_use`
  - returned tool input was just `{}`
  - there was no explicit client-visible truncation error

This is a critical agentic risk:

- plain text output can exceed what `Write` tool JSON can carry safely
- once the tool JSON gets too large, the gateway/upstream path degrades to an
  empty tool payload instead of surfacing a clear failure


### 10. Anthropic compatibility gaps are visible in live behavior

#### `max_tokens` is ignored

Request:

- `max_tokens=1`
- prompt asked for ten words

Observed:

- full ten-word output returned
- `usage.output_tokens=11`
- `stop_reason=end_turn`

So the user-facing Anthropic `max_tokens` setting is not actually constraining
generation in this deployment.

#### `stop_sequences` is ignored

Request:

- `stop_sequences=['BBB']`
- prompt: `Reply exactly with: AAA BBB CCC`

Observed:

- output: `AAA BBB CCC`
- `stop_reason=end_turn`
- `stop_sequence=null`

So stop sequences are not being honored as Anthropic clients expect.

#### `tool_choice` is ignored

Request:

- tools: `alpha_tool`, `beta_tool`
- `tool_choice={"type":"tool","name":"beta_tool"}`
- prompt asked to use the required tool now

Observed:

- no tool call returned
- model replied in plain text asking which tool to use

So `tool_choice` semantics are not wired through in practice.


### 11. Public reverse proxy looked behaviorally transparent for a basic probe

Direct request to:

- `https://api.10010074.xyz/kiro/v1/messages`

Observed:

- `200 OK`
- `server=nginx/1.18.0 (Ubuntu)`
- gateway cache/prompt-cache headers still present
- response semantics matched the internal endpoint

That is enough to treat nginx as a thin transport layer for this investigation,
not the root cause of the main behavior differences.


## Controlled Debug Capture

One controlled debug run was performed with `DEBUG_MODE=all`.

### Debug hook problem in current deployment

At first, debug capture failed with:

- `[Errno 13] Permission denied: '/app/runtime/debug_logs/request_body.json'`
- `[Errno 13] Permission denied: '/app/runtime/debug_logs/kiro_request_body.json'`

The mounted runtime directory existed, but the container user could not write to
it. Debug capture only worked after temporarily making the host debug directory
writable.

This is a real operational issue in the current deployment, even though
`DEBUG_MODE` normally stays off.

### What the debug capture proved

Request sent:

- `system` as Anthropic content-block array
- one tool with description length `14959`
- user asked for `DEBUG_OK_2`

Captured `kiro_request_body.json` showed:

- `conversationState.currentMessage.userInputMessage.content` was:
  - `SYSTEM_ALPHA`
  - `SYSTEM_BETA`
  - blank line
  - user text
- therefore Anthropic `system` was flattened into ordinary message text

It also showed:

- `toolSpecification.description_len = 10000`
- tail ended with `... [truncated by gateway]`

So long tool descriptions are definitely truncated before the gateway forwards
them to Kiro.


## White-Box Findings That Matter To Live Behavior

These came from code inspection and support the live results.

### 1. Prompt cache layer is compatibility-only

`kiro/prompt_cache.py` explicitly says it does not change the upstream Kiro
request and only reports Anthropic-compatible cache accounting.

Implication:

- prompt-cache headers are not proof of real Kiro-side prefill savings

### 2. `tool_result.is_error` is dropped

Anthropic `tool_result.is_error` is accepted by the schema, but converter logic
drops it and forwards tool completion as success.

Implication:

- a Claude client can tell the gateway “the tool failed”
- the upstream model may still see a successful tool result

### 3. `tool_choice` is modeled but not actually forwarded

This matches the live failure of forced tool choice.

### 4. Sampling/output-control parameters are not forwarded into the Kiro payload builder

That is consistent with live evidence that `max_tokens` and `stop_sequences`
were ignored. The modeled Anthropic/OpenAI surface is broader than what the
gateway actually enforces upstream.

### 5. Tool-call truncation falls back to empty JSON

Parser code marks likely-truncated tool JSON and substitutes empty arguments when
recovery is off. This matches the live `Write` `{}` result once tool payloads
got large enough.

### 6. Truncation recovery is off by default, but if enabled it mutates future requests

This was not active during live tests, but if turned on it injects synthetic
notices into later requests. That is important for anyone expecting raw
Anthropic-compatible semantics.


## Risks For Claude Code / Agentic Use

### High risk

- Hidden upstream prompt/tool envelope means context headroom is much smaller
  than user-side token estimates suggest.
- `max_tokens`, `stop_sequences`, and `tool_choice` cannot be relied on.
- Large `Write` tool payloads can silently degrade to `{}`.
- `tool_result.is_error` semantics are not preserved.

### Medium risk

- Plain long output can silently stop early with `end_turn`.
- Tool-result cache can inject prior read-only tool output when scope matches.
- Count-tokens results do not represent true upstream input cost.
- Debug capture is currently broken unless runtime directory permissions are
  fixed.

### Lower risk but still important

- Exact response cache can hide underlying model behavior unless requests are
  varied.
- Prompt-cache headers can mislead operators into believing upstream prompt
  caching exists when the gateway only recorded a local compatibility hit.


## Practical Operating Guidance

For this deployment, assume the following:

1. Keep a large safety margin on context.
   A request that looks like `130k` user-side tokens is already close to the
   real cliff.

2. Do not trust Anthropic `max_tokens`, `stop_sequences`, or `tool_choice` to
   control the model.

3. For code writing, prefer chunked edits or patch-style writes.
   Do not ask the model to place very large file bodies into one `Write` tool
   call.

4. Treat `Write` tool payloads above about `12.8KB` / `1600` short numbered
   lines as dangerous.

5. Treat prompt-cache headers as diagnostics only.

6. If you need debug logs, fix runtime directory ownership/permissions first.

7. If you test anything twice, vary the request enough to avoid accidental exact
   response-cache hits.


## Recommended Fix Priorities

1. Wire through or explicitly reject unsupported Anthropic controls:
   - `max_tokens`
   - `stop_sequences`
   - `tool_choice`

2. Preserve `tool_result.is_error`.

3. Expose truncation honestly instead of returning `tool_use` with `{}`.

4. Add explicit write-size safeguards for large tool payloads.

5. Fix `DEBUG_MODE` runtime permissions so request/response capture actually
   works in production.

6. Make the hidden upstream prompt/tool envelope more visible to operators.
   At minimum, document that `/count_tokens` is a gateway-side estimate, not the
   same thing as upstream billed/context tokens.


## Bottom Line

This endpoint is usable for Claude Code style work, but only if you treat it as
a Kiro-shaped proxy with compatibility gaps, not as a faithful Anthropic
endpoint.

The biggest practical hazards are:

- hidden context overhead
- ignored control parameters
- silent long-output truncation
- silent large-`Write` tool degradation to `{}`

Those are the behaviors most likely to cause confusing failures in real agentic
workflows.
