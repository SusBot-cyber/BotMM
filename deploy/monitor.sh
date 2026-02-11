#!/bin/bash
# BotMM — Health check script
# Run from cron every 5 min: */5 * * * * /home/ec2-user/BotMM/deploy/monitor.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"
SERVICE_NAME="botmm-recorder"
ALERT_SENT_FLAG="/tmp/botmm-alert-sent"

# Load config
if [ -f "${SCRIPT_DIR}/.env" ]; then
    export $(grep -v '^#' "${SCRIPT_DIR}/.env" | grep -v '^\s*$' | xargs)
fi

DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
ALERT_DISK_THRESHOLD="${ALERT_DISK_THRESHOLD:-80}"
DATA_DIR="${RECORDER_OUTPUT_DIR:-${REPO_DIR}/data/orderbook}"

send_discord_alert() {
    local title="$1"
    local message="$2"
    local color="${3:-16711680}"  # red by default

    if [ -z "${DISCORD_WEBHOOK_URL}" ]; then
        echo "[ALERT] ${title}: ${message}"
        return
    fi

    local payload=$(cat <<EOF
{
  "embeds": [{
    "title": "🚨 BotMM Recorder Alert",
    "description": "**${title}**\n${message}",
    "color": ${color},
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "footer": {"text": "$(hostname)"}
  }]
}
EOF
)

    curl -s -H "Content-Type: application/json" \
         -d "${payload}" \
         "${DISCORD_WEBHOOK_URL}" >/dev/null 2>&1
}

ISSUES=()

# 1. Check if service is running
if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
    ISSUES+=("Service ${SERVICE_NAME} is NOT running")

    # Try to restart
    sudo systemctl restart "${SERVICE_NAME}" 2>/dev/null
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        ISSUES+=("Auto-restart SUCCEEDED")
    else
        ISSUES+=("Auto-restart FAILED — manual intervention needed")
    fi
fi

# 2. Check disk usage
DISK_USAGE=$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')
if [ "${DISK_USAGE}" -ge "${ALERT_DISK_THRESHOLD}" ]; then
    ISSUES+=("Disk usage: ${DISK_USAGE}% (threshold: ${ALERT_DISK_THRESHOLD}%)")
fi

# 3. Check data freshness (warn if no new data in last 10 min)
if [ -d "${DATA_DIR}" ]; then
    RECENT_FILES=$(find "${DATA_DIR}" -name "*.csv" -mmin -10 2>/dev/null | head -1)
    if [ -z "${RECENT_FILES}" ]; then
        ISSUES+=("No new CSV data in last 10 minutes")
    fi
fi

# 4. Check WebSocket connectivity to Hyperliquid
if ! curl -s --max-time 5 "https://api.hyperliquid.xyz/info" >/dev/null 2>&1; then
    ISSUES+=("Cannot reach Hyperliquid API")
fi

# 5. Check memory usage
MEM_USAGE=$(free | awk 'NR==2{printf "%.0f", $3*100/$2}')
if [ "${MEM_USAGE}" -ge 90 ]; then
    ISSUES+=("Memory usage: ${MEM_USAGE}%")
fi

# Report issues
if [ ${#ISSUES[@]} -gt 0 ]; then
    ALERT_MSG=$(printf "• %s\n" "${ISSUES[@]}")
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ALERT: ${ALERT_MSG}"

    # Rate-limit alerts: max 1 per 30 min
    if [ ! -f "${ALERT_SENT_FLAG}" ] || [ $(( $(date +%s) - $(stat -c %Y "${ALERT_SENT_FLAG}" 2>/dev/null || echo 0) )) -ge 1800 ]; then
        send_discord_alert "Health Check Failed" "${ALERT_MSG}"
        touch "${ALERT_SENT_FLAG}"
    fi
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] OK — service running, disk ${DISK_USAGE}%, mem ${MEM_USAGE}%"
    # Clear alert flag on recovery
    rm -f "${ALERT_SENT_FLAG}"
fi
