#!/bin/bash
# BotMM — Sync orderbook data to S3 bucket
# Add to crontab: 0 */6 * * * /home/ec2-user/BotMM/deploy/sync_to_s3.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"

# Load config
if [ -f "${SCRIPT_DIR}/.env" ]; then
    export $(grep -v '^#' "${SCRIPT_DIR}/.env" | grep -v '^\s*$' | xargs)
fi

S3_BUCKET="${S3_BUCKET:-botmm-orderbook-data}"
S3_REGION="${S3_REGION:-eu-central-1}"
DATA_DIR="${RECORDER_OUTPUT_DIR:-${REPO_DIR}/data/orderbook}"

if ! command -v aws &>/dev/null; then
    echo "ERROR: AWS CLI not installed. Install with: sudo dnf install -y aws-cli"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Syncing ${DATA_DIR} → s3://${S3_BUCKET}/"
aws s3 sync "${DATA_DIR}" "s3://${S3_BUCKET}/orderbook/" \
    --region "${S3_REGION}" \
    --exclude "*.tmp" \
    --quiet

echo "[$(date '+%Y-%m-%d %H:%M:%S')] S3 sync complete"
