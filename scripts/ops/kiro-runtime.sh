#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

read_env_value() {
  local key="$1"
  local default_value="${2:-}"

  if [[ -f "${ENV_FILE}" ]]; then
    local line
    line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    if [[ -n "${line}" ]]; then
      line="${line#*=}"
      line="${line%\"}"
      line="${line#\"}"
      line="${line%\'}"
      line="${line#\'}"
      printf '%s\n' "${line}"
      return 0
    fi
  fi

  printf '%s\n' "${default_value}"
}

resolve_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    printf '%s\n' "${value}"
  else
    printf '%s\n' "${REPO_ROOT}/${value}"
  fi
}

compose_cmd() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

RUNTIME_DIR_RAW="${KIRO_GATEWAY_RUNTIME_DIR:-$(read_env_value KIRO_GATEWAY_RUNTIME_DIR ./runtime)}"
REDIS_DATA_DIR_RAW="${KIRO_GATEWAY_REDIS_DATA_DIR:-$(read_env_value KIRO_GATEWAY_REDIS_DATA_DIR ./runtime/redis)}"
PORT_BIND_VALUE="${KIRO_GATEWAY_PORT_BIND:-$(read_env_value KIRO_GATEWAY_PORT_BIND 8000:8000)}"
RUNTIME_DIR="$(resolve_path "${RUNTIME_DIR_RAW}")"
REDIS_DATA_DIR="$(resolve_path "${REDIS_DATA_DIR_RAW}")"
BACKUP_ROOT="${RUNTIME_DIR}/backups"
LEGACY_USAGE_FILE="${REPO_ROOT}/usage-accounts.json"
LEGACY_PROXY_KEY_FILE="${REPO_ROOT}/.proxy_api_key"
LEGACY_OVERRIDE_FILE="${REPO_ROOT}/docker-compose.override.yml"

ensure_dirs() {
  mkdir -p "${RUNTIME_DIR}" \
           "${RUNTIME_DIR}/debug_logs" \
           "${RUNTIME_DIR}/legacy" \
           "${RUNTIME_DIR}/secrets" \
           "${BACKUP_ROOT}" \
           "${REDIS_DATA_DIR}"
}

copy_if_present() {
  local source="$1"
  local dest="$2"
  if [[ -e "${source}" ]]; then
    mkdir -p "$(dirname "${dest}")"
    cp -a "${source}" "${dest}"
  fi
}

move_if_present() {
  local source="$1"
  local dest="$2"
  if [[ -e "${source}" ]]; then
    mkdir -p "$(dirname "${dest}")"
    mv "${source}" "${dest}"
    printf 'moved %s -> %s\n' "${source}" "${dest}"
  fi
}

create_backup() {
  ensure_dirs
  local target="${1:-${BACKUP_ROOT}/$(date +%Y%m%d-%H%M%S)}"
  mkdir -p "${target}"

  copy_if_present "${ENV_FILE}" "${target}/repo/.env"
  copy_if_present "${LEGACY_USAGE_FILE}" "${target}/repo/usage-accounts.json"
  copy_if_present "${LEGACY_PROXY_KEY_FILE}" "${target}/repo/.proxy_api_key"
  copy_if_present "${LEGACY_OVERRIDE_FILE}" "${target}/repo/docker-compose.override.yml"

  if [[ -d "${RUNTIME_DIR}" ]]; then
    mkdir -p "${target}/runtime"
    tar -C "${RUNTIME_DIR}" --exclude='./backups' -cf - . | tar -C "${target}/runtime" -xf -
  fi

  if [[ -d "${REDIS_DATA_DIR}" && "${REDIS_DATA_DIR}" != "${RUNTIME_DIR}/redis" ]]; then
    mkdir -p "${target}/redis"
    tar -C "${REDIS_DATA_DIR}" -cf - . | tar -C "${target}/redis" -xf -
  fi

  {
    printf 'created_at=%s\n' "$(date -Iseconds)"
    printf 'repo_root=%s\n' "${REPO_ROOT}"
    printf 'runtime_dir=%s\n' "${RUNTIME_DIR}"
    printf 'redis_data_dir=%s\n' "${REDIS_DATA_DIR}"
    printf 'port_bind=%s\n' "${PORT_BIND_VALUE}"
  } > "${target}/manifest.txt"

  printf '%s\n' "${target}"
}

restore_backup() {
  local backup_dir="${1:-}"
  if [[ -z "${backup_dir}" || ! -d "${backup_dir}" ]]; then
    echo "usage: $0 restore <backup_dir>" >&2
    exit 1
  fi

  ensure_dirs

  if [[ -d "${backup_dir}/runtime" ]]; then
    find "${RUNTIME_DIR}" -mindepth 1 -maxdepth 1 ! -name backups -exec rm -rf {} +
    tar -C "${backup_dir}/runtime" -cf - . | tar -C "${RUNTIME_DIR}" -xf -
  fi

  if [[ -d "${backup_dir}/redis" ]]; then
    rm -rf "${REDIS_DATA_DIR}"
    mkdir -p "${REDIS_DATA_DIR}"
    tar -C "${backup_dir}/redis" -cf - . | tar -C "${REDIS_DATA_DIR}" -xf -
  fi

  copy_if_present "${backup_dir}/repo/.env" "${ENV_FILE}"
  copy_if_present "${backup_dir}/repo/usage-accounts.json" "${LEGACY_USAGE_FILE}"
  copy_if_present "${backup_dir}/repo/.proxy_api_key" "${LEGACY_PROXY_KEY_FILE}"
  copy_if_present "${backup_dir}/repo/docker-compose.override.yml" "${LEGACY_OVERRIDE_FILE}"
}

print_status() {
  echo "repo_root=${REPO_ROOT}"
  echo "env_file=${ENV_FILE}"
  echo "runtime_dir=${RUNTIME_DIR}"
  echo "redis_data_dir=${REDIS_DATA_DIR}"
  echo "port_bind=${PORT_BIND_VALUE}"
  echo

  echo "[git]"
  git -C "${REPO_ROOT}" status --short --branch
  echo

  echo "[legacy]"
  for path in "${LEGACY_USAGE_FILE}" "${LEGACY_PROXY_KEY_FILE}" "${LEGACY_OVERRIDE_FILE}"; do
    if [[ -e "${path}" ]]; then
      echo "present ${path}"
    else
      echo "missing ${path}"
    fi
  done
  echo

  echo "[runtime]"
  ensure_dirs
  ls -la "${RUNTIME_DIR}"
  echo

  echo "[docker]"
  compose_cmd ps || true
}

migrate_legacy() {
  local backup_dir
  backup_dir="$(create_backup)"
  echo "backup_created=${backup_dir}"

  ensure_dirs
  move_if_present "${LEGACY_USAGE_FILE}" "${RUNTIME_DIR}/usage-accounts.json"
  move_if_present "${LEGACY_PROXY_KEY_FILE}" "${RUNTIME_DIR}/secrets/proxy_api_key.txt"
  move_if_present "${LEGACY_OVERRIDE_FILE}" "${RUNTIME_DIR}/legacy/docker-compose.override.yml"

  cat <<EOF
legacy migration complete
next_steps:
1. add compose variables from deploy/server.env.example into ${ENV_FILE}
2. run: $(basename "$0") status
3. rebuild: docker compose up -d --build
EOF
}

case "${1:-}" in
  status)
    print_status
    ;;
  prepare)
    ensure_dirs
    echo "prepared ${RUNTIME_DIR}"
    ;;
  backup)
    create_backup "${2:-}"
    ;;
  restore)
    restore_backup "${2:-}"
    ;;
  migrate-legacy)
    migrate_legacy
    ;;
  *)
    cat <<EOF
usage: $0 <command>

commands:
  status
  prepare
  backup [target_dir]
  restore <backup_dir>
  migrate-legacy
EOF
    exit 1
    ;;
esac
