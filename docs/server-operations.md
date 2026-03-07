# Server Operations

This layout keeps the git worktree clean on the server while making runtime
state easy to inspect, back up, migrate, and restore.

For a stateless LLM handoff, read [../LLM_HANDOFF.md](../LLM_HANDOFF.md) first.
That file also defines the required "four-end sync" checks before closing work.

## Goals

- Keep `/root/kiro-gateway` for tracked code only
- Keep runtime data in one external directory
- Avoid editing tracked compose files on the server
- Give LLMs and operators one repeatable workflow for status, backup, restore,
  and migration

## Recommended Layout

Example server layout:

```text
/root/kiro-gateway                # git clone, should stay clean
/opt/kiro-gateway/runtime         # debug logs, usage-accounts.json, backups
/opt/kiro-gateway/runtime/redis   # Redis AOF data
/opt/kiro-creds                   # mounted AWS SSO cache directory
```

Use the compose variables from [deploy/server.env.example](../deploy/server.env.example):

```env
KIRO_GATEWAY_PORT_BIND="127.0.0.1:8000:8000"
KIRO_GATEWAY_RUNTIME_DIR="/opt/kiro-gateway/runtime"
KIRO_GATEWAY_REDIS_DATA_DIR="/opt/kiro-gateway/runtime/redis"
KIRO_GATEWAY_SSO_CACHE_DIR="/opt/kiro-creds"
DEBUG_DIR="/app/runtime/debug_logs"
KIRO_USAGE_ACCOUNTS_FILE="/app/runtime/usage-accounts.json"
```

## One-Time Migration From Legacy Server Layout

If your server previously kept `usage-accounts.json`, `.proxy_api_key`, or
`docker-compose.override.yml` in the repo root:

```bash
cd /root/kiro-gateway
KIRO_GATEWAY_RUNTIME_DIR=/opt/kiro-gateway/runtime \
KIRO_GATEWAY_REDIS_DATA_DIR=/opt/kiro-gateway/runtime/redis \
./scripts/ops/kiro-runtime.sh migrate-legacy
```

This command:

- creates the runtime directory structure
- writes a timestamped backup before moving anything
- moves `usage-accounts.json` into the runtime directory
- moves `.proxy_api_key` into `runtime/secrets/`
- archives `docker-compose.override.yml` into `runtime/legacy/`

After that, copy the compose variables from
[deploy/server.env.example](../deploy/server.env.example) into `.env`, then
rebuild:

```bash
docker compose up -d --build
```

## Daily Operations

Check current state:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh status
```

Create a backup:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh backup
```

Restore from a backup:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh restore /opt/kiro-gateway/runtime/backups/20260307-120000
docker compose up -d --build
```

## Sync Workflow

Recommended update flow for a server that should stay clean:

```bash
cd /root/kiro-gateway
./scripts/ops/kiro-runtime.sh backup
git pull --ff-only
docker compose up -d --build
./scripts/ops/kiro-runtime.sh status
```

If you use another sync method, keep the same rule: runtime state should no
longer live inside the tracked worktree.

## Troubleshooting

Useful commands:

```bash
docker compose ps
docker compose logs --tail=100
docker compose config
curl http://127.0.0.1:8000/health
```

Debug files are stored under `runtime/debug_logs/` when `DEBUG_MODE` is enabled.
