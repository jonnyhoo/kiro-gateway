# LLM Handoff

Last updated: 2026-03-07

This file is the shortest reliable handoff for a stateless LLM or operator.
Read this first before changing deployment or cache behavior.

## Current State

- Local repo path: `E:\VIBE_CODING_WORK\kiro-gateway`
- Server repo path: `/root/kiro-gateway`
- Public reverse-proxy base: `https://api.10010074.xyz/kiro/`
- Local health endpoint on server: `http://127.0.0.1:8000/health`
- Runtime data directory on server: `/opt/kiro-gateway/runtime`
- Redis persistence directory on server: `/opt/kiro-gateway/runtime/redis`
- AWS SSO mount on server: `/opt/kiro-creds`
- Current sync point must be verified with `git log --oneline -1` on both local and server

## Four-End Sync

When this project says "four-end sync", it means all four of these must match:

1. Local repo at `E:\VIBE_CODING_WORK\kiro-gateway`
2. Remote `origin/main`
3. Server repo at `/root/kiro-gateway`
4. Running server deployment behind `https://api.10010074.xyz/kiro/`

Completion criteria:

- local `git status` is clean
- `origin/main` contains the intended commit
- server `git status` is clean and `git log -1` matches the same commit
- `docker compose ps` is healthy on the server
- both health checks succeed:
  - `http://127.0.0.1:8000/health`
  - `https://api.10010074.xyz/kiro/health`

## Local Root Hygiene

The repo root should stay free of generated junk.

Safe-to-delete generated paths:

- `.pytest_cache/`
- `.ruff_cache/`
- any `__pycache__/`
- any `*.pyc` or `*.pyo`

Tracked root files are intentional unless a human explicitly decides otherwise.

## What Changed Recently

The gateway already includes:

- Redis exact response cache
- Redis read-only tool-result cache
- Redis Anthropic prompt-cache compatibility/accounting
- Anthropic message-level `cache_control` preservation
- Tool docs no longer mirrored into forwarded system prompt

The deployment layout was then cleaned up so the server git worktree stays clean:

- tracked `docker-compose.yml` is now generic
- server-specific port binding is controlled by `.env`
- runtime files no longer live in repo root
- `usage-accounts.json` now lives under `/opt/kiro-gateway/runtime/`
- legacy `.proxy_api_key` and `docker-compose.override.yml` were migrated out of repo root
- helper script added: `scripts/ops/kiro-runtime.sh`

## Files To Read First

1. `LLM_HANDOFF.md`
2. `docs/server-operations.md`
3. `docker-compose.yml`
4. `scripts/ops/kiro-runtime.sh`
5. If working on Anthropic caching:
   `kiro/prompt_cache.py`
   `kiro/routes_anthropic.py`
   `kiro/tool_result_cache.py`
   `kiro/response_cache.py`

## Deployment Model

Server code should stay in:

- `/root/kiro-gateway`

Server runtime state should stay outside the git worktree:

- `/opt/kiro-gateway/runtime`
- `/opt/kiro-gateway/runtime/redis`
- `/opt/kiro-gateway/runtime/debug_logs`
- `/opt/kiro-gateway/runtime/backups`

Server `.env` should define:

```env
KIRO_GATEWAY_PORT_BIND="127.0.0.1:8000:8000"
KIRO_GATEWAY_RUNTIME_DIR="/opt/kiro-gateway/runtime"
KIRO_GATEWAY_REDIS_DATA_DIR="/opt/kiro-gateway/runtime/redis"
KIRO_GATEWAY_SSO_CACHE_DIR="/opt/kiro-creds"
DEBUG_DIR="/app/runtime/debug_logs"
KIRO_USAGE_ACCOUNTS_FILE="/app/runtime/usage-accounts.json"
```

## Standard Commands

Clean local generated junk before review or sync:

```bash
cd E:\VIBE_CODING_WORK\kiro-gateway
python scripts/ops/clean_generated.py
```

Check status:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh status
```

Create backup before risky work:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh backup
```

Rebuild service:

```bash
cd /root/kiro-gateway
docker compose up -d --build
```

Verify health:

```bash
curl http://127.0.0.1:8000/health
curl https://api.10010074.xyz/kiro/health
```

## Sync Workflow

Use this when local contains the intended commit:

### 1. Local

```bash
cd E:\VIBE_CODING_WORK\kiro-gateway
python scripts/ops/clean_generated.py
git status --short --branch
git push origin main
```

### 2. Server Repo + Running Service

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh backup
git pull --ff-only
docker compose up -d --build
./scripts/ops/kiro-runtime.sh status
```

### 3. Final Verification

```bash
git -C E:\VIBE_CODING_WORK\kiro-gateway log --oneline -1
git -C /root/kiro-gateway log --oneline -1
curl http://127.0.0.1:8000/health
curl https://api.10010074.xyz/kiro/health
```

## Recovery Workflow

List backups:

```bash
ls -la /opt/kiro-gateway/runtime/backups
```

Restore one backup:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh restore /opt/kiro-gateway/runtime/backups/<timestamp>
docker compose up -d --build
```

## Invariants

Do not casually break these:

- Do not store runtime state back into `/root/kiro-gateway`
- Do not edit tracked compose files on the server just to change deployment details
- Do not reintroduce `docker-compose.override.yml` in repo root for normal operation
- Keep tool-result cache scope-aware; never reuse across unrelated workspace/session scope
- Do not warm prompt-cache accounting on exact response-cache hits
- Before declaring "synced", verify all four ends, not just git push success

## Server Facts Verified On 2026-03-07

- `/root/kiro-gateway` git worktree is clean
- server repo and local repo matched at the last full sync check
- `kiro-gateway` container is healthy
- `kiro-gateway-redis` container is healthy
- public `/kiro/health` reverse proxy is healthy

## If You Need More Context

Read these next:

- `docs/autocache-borrow-plan.md`
- `docs/lynkr-borrow-plan.md`
- `docs/server-operations.md`

If you are asked “what happened before this”, start from `git log --oneline -10`
and the latest backup directory under `/opt/kiro-gateway/runtime/backups/`.
