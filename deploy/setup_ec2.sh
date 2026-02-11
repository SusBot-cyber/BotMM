#!/bin/bash
# BotMM L2 Recorder — EC2 Setup Script
# Run as: sudo bash deploy/setup_ec2.sh
# Tested on: Amazon Linux 2023 (t2.micro / t3.micro)
set -euo pipefail

REPO_DIR="/home/ec2-user/BotMM"
VENV_DIR="${REPO_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/botmm-recorder.service"
LOGROTATE_FILE="/etc/logrotate.d/botmm-recorder"

echo "=== BotMM L2 Recorder — EC2 Setup ==="

# 1. System updates
echo "[1/10] Updating system packages..."
dnf update -y -q

# 2. Install Python 3.11+, pip, git, cron
echo "[2/10] Installing Python, pip, git, cron..."
dnf install -y -q python3.11 python3.11-pip python3.11-devel git cronie
systemctl enable crond
systemctl start crond

# 3. Verify repo is present
echo "[3/10] Checking BotMM repository..."
if [ ! -d "${REPO_DIR}" ]; then
    echo "ERROR: BotMM repo not found at ${REPO_DIR}"
    echo "Clone it first: git clone https://github.com/SusBot-cyber/BotMM.git ${REPO_DIR}"
    exit 1
fi

# 4. Create virtualenv
echo "[4/10] Creating virtual environment..."
python3.11 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

# 5. Install Python dependencies (minimal set for recorder)
echo "[5/10] Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet numpy websockets requests python-dotenv aiohttp

# 6. Create data directories
echo "[6/10] Creating data directories..."
mkdir -p "${REPO_DIR}/data/orderbook"
mkdir -p "${REPO_DIR}/logs"
chown -R ec2-user:ec2-user "${REPO_DIR}/data" "${REPO_DIR}/logs"

# 7. Create .env from template if not exists
echo "[7/10] Setting up configuration..."
if [ ! -f "${REPO_DIR}/deploy/.env" ]; then
    cp "${REPO_DIR}/deploy/.env.example" "${REPO_DIR}/deploy/.env"
    echo "  → Created deploy/.env — edit with your Discord webhook URL"
fi

# 8. Install systemd service
echo "[8/10] Installing systemd service..."
cp "${REPO_DIR}/deploy/botmm-recorder.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable botmm-recorder

# 9. Install logrotate config
echo "[9/10] Setting up log rotation..."
cp "${REPO_DIR}/deploy/logrotate.conf" "${LOGROTATE_FILE}"

# 10. Set up monitoring cron
echo "[10/10] Setting up monitoring cron..."
chmod +x "${REPO_DIR}/deploy/monitor.sh"
chmod +x "${REPO_DIR}/deploy/sync_to_s3.sh"

# Install crontab for ec2-user (monitor every 5 min)
CRON_ENTRY="*/5 * * * * ${REPO_DIR}/deploy/monitor.sh >> ${REPO_DIR}/logs/monitor.log 2>&1"
(crontab -u ec2-user -l 2>/dev/null | grep -v "monitor.sh"; echo "${CRON_ENTRY}") | crontab -u ec2-user -

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config:    nano ${REPO_DIR}/deploy/.env"
echo "  2. Start recorder: sudo systemctl start botmm-recorder"
echo "  3. Check status:   sudo systemctl status botmm-recorder"
echo "  4. View logs:      journalctl -u botmm-recorder -f"
echo ""
echo "Optional: Set up S3 backup cron:"
echo "  crontab -e  # add: 0 */6 * * * ${REPO_DIR}/deploy/sync_to_s3.sh"
