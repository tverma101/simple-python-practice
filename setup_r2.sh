#!/usr/bin/env bash
# One-time setup for Cloudflare R2 upload credentials.
#
# Creates an R2 API token at:
#   Cloudflare dashboard → R2 → Manage R2 API Tokens → Create API Token
#   Permissions: Object Read & Write, scoped to the ducky release bucket.
# You'll get three values: Access Key ID, Secret Access Key, and the
# endpoint URL https://<account-id>.r2.cloudflarestorage.com
#
# This script stores them in the standard AWS shared-credentials files so
# that upload_to_r2.py can read them with no env vars set:
#   ~/.aws/credentials   → [r2]  aws_access_key_id / aws_secret_access_key
#   ~/.aws/config        → [profile r2]  region + s3.endpoint_url
set -euo pipefail

CREDS_FILE="${HOME}/.aws/credentials"
CONFIG_FILE="${HOME}/.aws/config"
PROFILE="r2"

# Defaults — override via env if you ever change buckets.
# Bucket name is "ducky"; pub-be8cbd… is the public custom domain, not the bucket.
DEFAULT_BUCKET="ducky"
BUCKET="${R2_BUCKET:-$DEFAULT_BUCKET}"

say()  { printf "\033[1;36m[R2 setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[R2 setup]\033[0m %s\n" "$*" >&2; }

mkdir -p "${HOME}/.aws"
chmod 700 "${HOME}/.aws"

# ── Collect values (env wins, else prompt) ──────────────────────
say "Enter your R2 API token values (from dash.cloudflare.com → R2 → Manage R2 API Tokens)."
say "The secret access key is shown only once — paste it now."

read -rp "  Access Key ID:       " ACCESS_KEY
read -rsp "  Secret Access Key:   " SECRET_KEY; echo
read -rp "  Cloudflare Account ID (hex, from the endpoint URL): " ACCOUNT_ID

if [[ -z "$ACCESS_KEY" || -z "$SECRET_KEY" || -z "$ACCOUNT_ID" ]]; then
  warn "All three values are required. Aborting."
  exit 1
fi

ENDPOINT="https://${ACCOUNT_ID}.r2.cloudflarestorage.com"

# ── Strip any existing [r2] blocks, then append fresh ones ─────
strip_profile() {
  local file="$1" marker="$2"
  if [[ -f "$file" ]]; then
    # Drop the named section ( awk is safe here for a small config file )
    awk -v sec="$marker" '
      $0 == sec { in_sec = 1; next }
      /^\[/ { in_sec = 0 }
      !in_sec { print }
    ' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
  fi
}

say "Writing [r2] profile to $CREDS_FILE ..."
strip_profile "$CREDS_FILE" "[$PROFILE]"
cat >> "$CREDS_FILE" <<EOF
[$PROFILE]
aws_access_key_id = $ACCESS_KEY
aws_secret_access_key = $SECRET_KEY
EOF
chmod 600 "$CREDS_FILE"

say "Writing [profile r2] to $CONFIG_FILE ..."
strip_profile "$CONFIG_FILE" "[profile $PROFILE]"
cat >> "$CONFIG_FILE" <<EOF
[profile $PROFILE]
region = auto
s3.endpoint_url = $ENDPOINT
EOF

# ── Verify ─────────────────────────────────────────────────────
say ""
say "Configured:"
say "   bucket:   $BUCKET"
say "   endpoint: $ENDPOINT"
say "   profile:  $PROFILE"
if command -v aws >/dev/null 2>&1; then
  say "Verifying access with: aws s3 ls s3://$BUCKET/ --profile r2"
  if aws s3 ls "s3://${BUCKET}/" --profile "$PROFILE" --endpoint-url "$ENDPOINT" >/dev/null 2>&1; then
    say "\033[1;32mOK — credentials work. Run: python3 upload_to_r2.py\033[0m"
  else
    warn "List failed. Check the token's bucket scope and account ID."
    warn "Re-run: bash setup_r2.sh"
    exit 1
  fi
else
  say "(aws CLI not installed — skipping live check. upload_to_r2.py uses pure stdlib, so it's not required.)"
  say "\033[1;32mDone. Run: python3 upload_to_r2.py\033[0m"
fi
