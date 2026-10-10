#!/usr/bin/env bash
# Exécuter sur EC2 après préparation de l'environnement et fin des runs actifs.
set +x
set -euo pipefail

AWS_REGION="${AWS_REGION:-eu-west-1}"
ECR_REGISTRY="${ECR_REGISTRY:-870900715939.dkr.ecr.eu-west-1.amazonaws.com}"
ECR_REPOSITORY="${ECR_REPOSITORY:-audit-qualite}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_URI="${IMAGE_URI:-${ECR_REGISTRY}/${ECR_REPOSITORY}:${IMAGE_TAG}}"
CONTAINER_NAME="${CONTAINER_NAME:-audit-qualite}"
# Aucun chemin EC2 confirmé dans le dépôt ; conserver la valeur demandée.
ENV_FILE="${ENV_FILE:-$HOME/.env}"
HOST_PORT="${HOST_PORT:-8501}"
CONTAINER_PORT="${CONTAINER_PORT:-8501}"
HOST_BIND_ADDRESS="${HOST_BIND_ADDRESS:-127.0.0.1}"

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail 'Docker is required.'
docker info >/dev/null 2>&1 || fail 'Docker daemon unavailable or access denied.'
command -v aws >/dev/null 2>&1 || fail 'AWS CLI is required.'
command -v curl >/dev/null 2>&1 || fail 'curl is required to verify Streamlit health.'
[[ -f "$ENV_FILE" && -r "$ENV_FILE" ]] || fail 'Environment file missing or unreadable.'
[[ "$(stat -c '%a' -- "$ENV_FILE")" == 600 ]] || fail 'Protect the environment file with chmod 600.'

# Lire des données, jamais sourcer/exécuter le fichier. Format Docker KEY=value,
# sans guillemets, export, substitution shell ni commentaire après la valeur.
declare -A settings=()
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || fail 'Use KEY=value syntax in the environment file.'
    name="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ ! -v "settings[$name]" ]] || fail 'Duplicate variable in the environment file.'
    case "$name" in
        AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)
            fail 'Remove static AWS credentials from the EC2 environment file; use the IAM role.' ;;
    esac
    settings["$name"]="$value"
done < "$ENV_FILE"
[[ "${settings[STORAGE_MODE]:-}" == s3 ]] || fail 'STORAGE_MODE must be s3.'
openrouter_key="${settings[OPENROUTER_API_KEY]:-}"
case "$openrouter_key" in
    ''|replace*|your_*|\"*|\'*) fail 'Set a real, unquoted OPENROUTER_API_KEY in the private environment file.' ;;
esac
[[ "$openrouter_key" != *[[:space:]]* ]] || fail 'OPENROUTER_API_KEY must not contain whitespace.'
case "${settings[S3_BUCKET]:-}" in
    ''|your-project-bucket|replace*) fail 'Configure S3_BUCKET in the private environment file.' ;;
esac
[[ "${settings[AWS_REGION]:-eu-west-1}" == "$AWS_REGION" ]] || fail 'AWS_REGION differs between the script and environment file.'
for port in "$HOST_PORT" "$CONTAINER_PORT"; do
    [[ "$port" =~ ^[0-9]{1,5}$ ]] || fail 'Ports must be integers from 1 to 65535.'
    (( 10#$port >= 1 && 10#$port <= 65535 )) || fail 'Port out of range.'
done
[[ "$CONTAINER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail 'Invalid container name.'

# AWS CLI doit utiliser le rôle EC2, sans credentials exportés ni profil local.
for name in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE; do
    [[ -z "${!name:-}" ]] || fail 'Unset AWS credentials/profiles in the deployment shell; use the EC2 IAM role.'
done
export AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null
aws sts get-caller-identity --region "$AWS_REGION" --no-cli-pager
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker pull "$IMAGE_URI"

# Ne remplacer que le conteneur nommé, après un pull réussi.
existing_id="$(docker ps -aq --filter "name=^/${CONTAINER_NAME}$")"
if [[ -n "$existing_id" ]]; then
    # Ne pas perdre silencieusement une configuration de volumes préexistante.
    [[ "$(docker inspect -f '{{len .Mounts}}' "$existing_id")" == 0 ]] \
        || fail 'Existing container has mounts. Preserve its mount configuration before replacement.'
    if [[ "$(docker inspect -f '{{.State.Running}}' "$existing_id")" == true ]]; then
        docker stop "$existing_id"
    fi
    docker rm "$existing_id"
fi

docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "${HOST_BIND_ADDRESS}:${HOST_PORT}:${CONTAINER_PORT}" \
    --env-file "$ENV_FILE" \
    "$IMAGE_URI"
# Au plus six essais de dix secondes, espacés de deux secondes (70 s maximum).
healthy=false
for attempt in {1..6}; do
    if health_response="$(curl --fail --silent \
        --connect-timeout 3 --max-time 10 \
        "http://127.0.0.1:${HOST_PORT}/_stcore/health")" \
        && [[ "$health_response" == ok ]]; then
        healthy=true
        break
    fi
    if (( attempt < 6 )); then sleep 2; fi
done
[[ "$healthy" == true ]] || fail 'Streamlit health check failed after six attempts.'
docker ps --filter "name=^/${CONTAINER_NAME}$"
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER_NAME"
# Masquer la clé exacte même si une dépendance la journalise accidentellement.
docker logs --tail 30 "$CONTAINER_NAME" 2>&1 | while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "${line//"$openrouter_key"/[REDACTED]}"
done
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" == true ]] \
    || fail 'Container is not running; deployment incomplete.'
printf 'Deployment completed.\n'
