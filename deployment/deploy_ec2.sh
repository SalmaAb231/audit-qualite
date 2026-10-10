#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-audit-qualite-pfa:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-audit-qualite}"
ENV_FILE="${ENV_FILE:-$HOME/audit-qualite.env}"
HOST_PORT="${HOST_PORT:-8501}"
CONTAINER_PORT="${CONTAINER_PORT:-8501}"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

require_env_value() {
  local key="$1"
  awk -F= -v expected="$key" '
    /^[[:space:]]*#/ { next }
    {
      name = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name != expected) { next }
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value != "" && value != "\"\"" && value != "\047\047") { found = 1 }
    }
    END { exit(found ? 0 : 1) }
  ' "$ENV_FILE" || fail "${key} is missing or empty in ${ENV_FILE}."
}

command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker info >/dev/null 2>&1 || fail "Docker daemon is not running or is not accessible."
[[ -f "$ENV_FILE" ]] || fail "Environment file not found: ${ENV_FILE}."
require_env_value "STORAGE_MODE"
require_env_value "OPENROUTER_API_KEY"
docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || fail "Docker image not found: ${IMAGE_NAME}."

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  printf 'Replacing existing container: %s\n' "$CONTAINER_NAME"
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" == "true" ]]; then
    docker stop "$CONTAINER_NAME" >/dev/null
  fi
  docker rm "$CONTAINER_NAME" >/dev/null
fi

docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --env-file "$ENV_FILE" \
  "$IMAGE_NAME"

sleep 5
docker ps --filter "name=^/${CONTAINER_NAME}$"
printf 'Restart policy: '
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER_NAME"
docker logs --tail 30 "$CONTAINER_NAME"
printf 'Deployment completed.\n'
