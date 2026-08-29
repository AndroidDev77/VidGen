#!/usr/bin/env bash
# Secret-pattern scan over the infrastructure and workflow sources.
#
# It looks for the shapes a credential actually takes in this repository - a
# provider key prefix, a PostgreSQL URL with a password, a storage account key,
# a Temporal API key, a PEM block - rather than for the word "password", which
# appears legitimately in parameter names and comments.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

TARGETS=(
  "${REPO_ROOT}/infra"
  "${REPO_ROOT}/.github/workflows"
  "${REPO_ROOT}/Dockerfile"
  "${REPO_ROOT}/apps/web/Dockerfile"
)

# Each entry is a POSIX extended regular expression.
PATTERNS=(
  'sk-[A-Za-z0-9_-]{20,}'                      # OpenAI style key
  'key-[A-Za-z0-9]{32,}'                       # Runway style key
  'AccountKey=[A-Za-z0-9+/=]{40,}'             # Storage account key
  'DefaultEndpointsProtocol=.*AccountKey='     # Storage connection string
  'postgres(ql)?(\+[a-z]+)?://[^:@/[:space:]]+:[^@/[:space:]]+@'  # URL with a password
  'redis[s]?://[^:@/[:space:]]*:[^@/[:space:]]+@'                 # Redis URL with a password
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'         # PEM private key
  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'  # JWT
  '"?client_secret"?[[:space:]]*[:=][[:space:]]*"[^"[:space:]]{8,}"'
)

# Matches that are known not to be secrets. Kept narrow and explicit rather
# than broad: an allowlist that swallows a real credential is worse than no
# scan at all.
#   * the scanner's own pattern list;
#   * the local development credential used by docker-compose and CI, which is
#     "vidgen:vidgen" against localhost or a compose service name and grants
#     access to nothing outside a developer's machine.
ALLOW=(
  "infra/scripts/scan_secrets.sh:"
  "@localhost"
  "@127.0.0.1"
  "@postgres:"
  "@redis:"
)

is_allowed() {
  local line="$1" allowed
  for allowed in "${ALLOW[@]}"; do
    if [[ "${line}" == *"${allowed}"* ]]; then
      return 0
    fi
  done
  return 1
}

found=0
for pattern in "${PATTERNS[@]}"; do
  # --exclude-dir keeps the scan off build output; the scan is over sources.
  if matches="$(grep -rInE --binary-files=without-match \
      --exclude-dir=node_modules --exclude-dir=.git \
      "${pattern}" "${TARGETS[@]}" 2>/dev/null)"; then
    while IFS= read -r line; do
      [[ -n "${line}" ]] || continue
      if is_allowed "${line}"; then
        continue
      fi
      # Report the location only. The matched text is never printed, because
      # printing it would move a real secret into a build log.
      log "possible secret at ${line%%:*}: pattern '${pattern}'"
      found=$((found + 1))
    done <<< "${matches}"
  fi
done

[[ "${found}" -eq 0 ]] || fail "${found} possible secret value(s) found"
log "no secret-shaped values found"
