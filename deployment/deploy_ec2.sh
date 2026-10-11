#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="eu-west-1"
AWS_ACCOUNT_ID="239280167189"
ECR_REPOSITORY="audit-qualite-pfa"
ECR_REGISTRY="239280167189.dkr.ecr.eu-west-1.amazonaws.com"
CONTAINER_NAME="audit-qualite"
HOST_PORT="8501"
CONTAINER_PORT="8501"
ENV_FILE="${ENV_FILE:-$HOME/audit-qualite.env}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

IMAGE_URI="${ECR_REGISTRY}/${ECR_REPOSITORY}:${IMAGE_TAG}"
BACKUP_NAME="${CONTAINER_NAME}-old"
HEALTH_URL="http://127.0.0.1:${HOST_PORT}"
HEALTH_ATTEMPTS=15
HEALTH_INTERVAL_SECONDS=3
HAVE_BACKUP=false

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_is_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]
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

show_failed_container_logs() {
  if container_exists "$CONTAINER_NAME"; then
    printf 'Last logs from the failed container:\n' >&2
    docker logs --tail 100 "$CONTAINER_NAME" >&2 || true
  fi
}

rollback() {
  show_failed_container_logs

  if container_exists "$CONTAINER_NAME"; then
    docker update --restart=no "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi

  if [[ "$HAVE_BACKUP" == "true" ]] && container_exists "$BACKUP_NAME"; then
    if docker rename "$BACKUP_NAME" "$CONTAINER_NAME" \
      && docker update --restart=unless-stopped "$CONTAINER_NAME" >/dev/null \
      && docker start "$CONTAINER_NAME" >/dev/null; then
      printf 'DEPLOYMENT FAILED - ROLLBACK COMPLETED\n' >&2
      exit 1
    fi

    printf 'DEPLOYMENT FAILED - ROLLBACK FAILED; manual intervention required.\n' >&2
    exit 1
  fi

  printf 'DEPLOYMENT FAILED - NO PREVIOUS CONTAINER TO RESTORE\n' >&2
  exit 1
}

printf 'Checking local deployment prerequisites...\n'
command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
command -v aws >/dev/null 2>&1 || fail "AWS CLI is not installed."
command -v curl >/dev/null 2>&1 || fail "curl is not installed."
docker info >/dev/null 2>&1 || fail "Docker daemon is not running or is not accessible."
[[ -f "$ENV_FILE" ]] || fail "Environment file not found: ${ENV_FILE}."
require_env_value "STORAGE_MODE"
require_env_value "OPENROUTER_API_KEY"

printf 'Authenticating Docker to Amazon ECR with the EC2 IAM role...\n'
if ! aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null; then
  fail "Amazon ECR authentication failed."
fi

printf 'Pulling image %s...\n' "$IMAGE_URI"
docker pull "$IMAGE_URI"

if container_exists "$CONTAINER_NAME"; then
  if container_exists "$BACKUP_NAME"; then
    printf 'Removing the previous stopped backup container %s...\n' "$BACKUP_NAME"
    docker update --restart=no "$BACKUP_NAME" >/dev/null 2>&1 || true
    docker stop "$BACKUP_NAME" >/dev/null 2>&1 || true
    docker rm "$BACKUP_NAME" >/dev/null
  fi

  printf 'Preserving the current container as %s...\n' "$BACKUP_NAME"
  CURRENT_WAS_RUNNING=false
  if container_is_running "$CONTAINER_NAME"; then
    CURRENT_WAS_RUNNING=true
    docker stop "$CONTAINER_NAME" >/dev/null \
      || fail "Unable to stop the current container."
  fi
  if ! docker update --restart=no "$CONTAINER_NAME" >/dev/null; then
    if [[ "$CURRENT_WAS_RUNNING" == "true" ]]; then
      docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    fail "Unable to disable automatic restart on the current container."
  fi
  if ! docker rename "$CONTAINER_NAME" "$BACKUP_NAME"; then
    docker update --restart=unless-stopped "$CONTAINER_NAME" >/dev/null 2>&1 || true
    if [[ "$CURRENT_WAS_RUNNING" == "true" ]]; then
      docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    fail "Unable to preserve the current container for rollback."
  fi
  HAVE_BACKUP=true
elif container_exists "$BACKUP_NAME"; then
  printf 'Reusing the existing backup container %s for rollback...\n' "$BACKUP_NAME"
  docker update --restart=no "$BACKUP_NAME" >/dev/null 2>&1 || true
  if container_is_running "$BACKUP_NAME"; then
    docker stop "$BACKUP_NAME" >/dev/null
  fi
  HAVE_BACKUP=true
fi

printf 'Starting the new container %s...\n' "$CONTAINER_NAME"
if ! docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --env-file "$ENV_FILE" \
  "$IMAGE_URI" >/dev/null; then
  rollback
fi

printf 'Waiting for the local HTTP health check...\n'
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  if ! container_is_running "$CONTAINER_NAME"; then
    printf 'The new container stopped before becoming healthy.\n' >&2
    rollback
  fi

  if curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
    printf 'Health check succeeded on attempt %s/%s.\n' "$attempt" "$HEALTH_ATTEMPTS"
    printf 'DEPLOYMENT SUCCESS\n'
    exit 0
  fi

  if ((attempt < HEALTH_ATTEMPTS)); then
    printf 'Health check attempt %s/%s failed; retrying in %ss...\n' \
      "$attempt" "$HEALTH_ATTEMPTS" "$HEALTH_INTERVAL_SECONDS"
    sleep "$HEALTH_INTERVAL_SECONDS"
  fi
done

printf 'The new container did not pass the HTTP health check.\n' >&2
rollback
