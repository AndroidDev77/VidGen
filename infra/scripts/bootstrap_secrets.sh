#!/usr/bin/env bash
# Write the runtime secrets into Key Vault, without any of them being echoed,
# logged, stored in a file or passed on a command line.
#
#   AZURE_SUBSCRIPTION_ID=... VIDGEN_KEY_VAULT_NAME=... ./infra/scripts/bootstrap_secrets.sh
#
# Behaviour:
#   * lists every secret name the deployment requires, and which are optional;
#   * reads each value from a terminal prompt with echo disabled, or from an
#     already-exported environment variable for automation;
#   * writes straight to Key Vault with the Azure CLI;
#   * skips a secret that already exists unless --overwrite is given, so it is
#     safe to rerun;
#   * reports missing required secrets by *name*, never by value.
#
# No value is ever printed, and no value is ever written to disk.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
require_env VIDGEN_KEY_VAULT_NAME

OVERWRITE=0
CHECK_ONLY=0
for argument in "$@"; do
  case "${argument}" in
    --overwrite) OVERWRITE=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fail "unknown argument ${argument}" ;;
  esac
done

# name|required|environment variable|description
SECRETS=(
  "postgres-app-url|required|VIDGEN_SECRET_POSTGRES_APP_URL|SQLAlchemy URL for the least-privileged application role"
  "postgres-migration-url|required|VIDGEN_SECRET_POSTGRES_MIGRATION_URL|SQLAlchemy URL for the migration role (owns the schema)"
  "redis-url|required|VIDGEN_SECRET_REDIS_URL|rediss:// URL including the access key"
  "blob-signing-secret|required|VIDGEN_SECRET_BLOB_SIGNING_SECRET|HMAC secret for the filesystem-store signed URLs"
  "app-signing-secret|required|VIDGEN_SECRET_APP_SIGNING_SECRET|application signing secret"
  "temporal-api-key|required|VIDGEN_SECRET_TEMPORAL_API_KEY|Temporal Cloud API key for the configured namespace"
  "openai-api-key|optional|VIDGEN_SECRET_OPENAI_API_KEY|only when providers.openai is true"
  "runway-api-secret|optional|VIDGEN_SECRET_RUNWAY_API_SECRET|only when providers.runway is true"
  "google-credentials-json|optional|VIDGEN_SECRET_GOOGLE_CREDENTIALS_JSON|workload-identity-federation config for Veo; NOT a static access token"
  "elevenlabs-api-key|optional|VIDGEN_SECRET_ELEVENLABS_API_KEY|only when providers.elevenlabs is true"
  "voicestudio-endpoint|optional|VIDGEN_SECRET_VOICESTUDIO_ENDPOINT|only when providers.voicestudio is true"
  "voicestudio-api-key|optional|VIDGEN_SECRET_VOICESTUDIO_API_KEY|only when providers.voicestudio is true"
  "opensubtitles-api-key|optional|VIDGEN_SECRET_OPENSUBTITLES_API_KEY|only when providers.opensubtitles is true"
  "opensubtitles-username|optional|VIDGEN_SECRET_OPENSUBTITLES_USERNAME|only when providers.opensubtitles is true"
  "opensubtitles-password|optional|VIDGEN_SECRET_OPENSUBTITLES_PASSWORD|only when providers.opensubtitles is true"
  "youtube-oauth-client-secret|optional|VIDGEN_SECRET_YOUTUBE_OAUTH_CLIENT_SECRET|T25: OAuth 2.0 client secret; only when providers.youtube is true. The client ID is NOT a secret and belongs in the parameter file"
  "youtube-token-encryption-key|optional|VIDGEN_SECRET_YOUTUBE_TOKEN_ENCRYPTION_KEY|T25: base64 AES-256 key sealing stored refresh tokens. Generate with: uv run python -c \"from vidgen.security.envelope import generate_key; print(generate_key())\". Rotating it means writing a new value AND bumping youtube.tokenEncryptionKeyVersion"
)
# applicationinsights-connection-string is written by main.bicep from the
# component it creates, so it is deliberately absent from this list.

secret_exists() {
  az keyvault secret show --vault-name "${VIDGEN_KEY_VAULT_NAME}" --name "$1" \
    --query id -o tsv >/dev/null 2>&1
}

write_secret() {
  local name="$1" value="$2"
  # --value on the command line would place the secret in this process's argv,
  # which is world-readable on Linux; --file with /dev/stdin keeps it on a pipe.
  printf '%s' "${value}" | az keyvault secret set \
    --vault-name "${VIDGEN_KEY_VAULT_NAME}" \
    --name "${name}" \
    --file /dev/stdin \
    --output none
}

missing_required=()
written=0
skipped=0

for entry in "${SECRETS[@]}"; do
  IFS='|' read -r name requirement variable description <<< "${entry}"

  if secret_exists "${name}" && [[ "${OVERWRITE}" -eq 0 ]]; then
    log "${name}: already present, leaving unchanged (${description})"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "${CHECK_ONLY}" -eq 1 ]]; then
    if [[ "${requirement}" == "required" ]]; then
      missing_required+=("${name}")
    else
      log "${name}: absent, optional (${description})"
    fi
    continue
  fi

  value="${!variable:-}"
  if [[ -z "${value}" ]]; then
    if [[ -t 0 ]]; then
      printf 'Value for %s (%s, %s) - leave blank to skip: ' \
        "${name}" "${requirement}" "${description}" >&2
      # -s disables echo, so the value never appears on the terminal or in a
      # scrollback buffer, and it is never added to shell history.
      read -rs value
      printf '\n' >&2
    fi
  fi

  if [[ -z "${value}" ]]; then
    if [[ "${requirement}" == "required" ]]; then
      missing_required+=("${name}")
    else
      log "${name}: skipped (optional)"
    fi
    continue
  fi

  write_secret "${name}" "${value}"
  unset value
  log "${name}: written"
  written=$((written + 1))
done

log "secrets written: ${written}, left unchanged: ${skipped}"

if [[ "${#missing_required[@]}" -gt 0 ]]; then
  log "missing required secrets:"
  for name in "${missing_required[@]}"; do
    log "  - ${name}"
  done
  fail "${#missing_required[@]} required secret(s) are missing"
fi

log "every required secret is present"
