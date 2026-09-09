#!/usr/bin/env bash
set -euo pipefail

failed=0

report_matches() {
  local label="$1"
  local matches="$2"
  if [[ -n "$matches" ]]; then
    echo "ERROR: $label"
    echo "$matches"
    failed=1
  fi
}

tracked_artifacts=""
credential_files=""
while IFS= read -r path; do
  case "$path" in
    .env|*/.env|to_scrape_web_list.json|*/to_scrape_web_list.json|*.warc|*.warc.gz|*.log|*.bak|*.bak_*|*.bak-*|*.db|*.db-shm|*.db-wal|*/debug_html/*|*/asn_debug_html/*|*/results_*/*)
      tracked_artifacts+="${path}"$'\n'
      ;;
  esac
  # Credential / private-key files must never be tracked, whatever directory
  # they land in. .gitignore is not enough on its own — this catches a file
  # that was force-added (git add -f) or committed before the ignore rule
  # existed. .env.example is the one intentional exception.
  case "${path##*/}" in
    .env.example)
      ;;
    *.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|id_rsa|id_dsa|id_ecdsa|id_ed25519|.netrc|.env|.env.*|aws-credentials*|credentials.json|client_secret*.json|*service-account*.json|*service_account*.json|gcp-*.json)
      credential_files+="${path}"$'\n'
      ;;
  esac
done < <(git ls-files)
report_matches "generated/private artifacts are tracked" "${tracked_artifacts%$'\n'}"
report_matches "a credential or private-key file is tracked" "${credential_files%$'\n'}"

internal_refs=$(git grep -I -l -E \
  '(/home/|/Users/|/data/[[:alnum:]_.-]+|blake\.cc\.gatech\.edu|budelli\.cc\.gatech\.edu|zchen798|Claude-Session|caas_jupyter_tools)' \
  -- . ':!scripts/check_public_release.sh' 2>/dev/null || true)
report_matches "tracked text contains an internal path, identity, host, or session marker" "$internal_refs"

secret_refs=$(git grep -I -l -E \
  '(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[0-9A-Za-z-]{10,}|BEGIN [A-Z0-9 ]*PRIVATE KEY)' \
  -- . ':!scripts/check_public_release.sh' 2>/dev/null || true)
report_matches "tracked text contains a possible credential or private key" "$secret_refs"

if [[ -n "${PUBLIC_BASE_REF:-}" ]] && git cat-file -e "${PUBLIC_BASE_REF}^{commit}" 2>/dev/null; then
  commit_markers=$(git log --format='%H %ae%n%B' "${PUBLIC_BASE_REF}..HEAD" | \
    grep -E '(Claude-Session|@blake\.cc\.gatech\.edu|@budelli\.cc\.gatech\.edu|zchen798)' || true)
  report_matches "new commit metadata contains an internal identity, host, or session marker" "$commit_markers"
fi

if [[ "$failed" -ne 0 ]]; then
  exit 1
fi

echo "Public-release checks passed."
