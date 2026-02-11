# BotMM L2 Recorder — AWS Deployment Guide

Deploy the L2 order book recorder on AWS EC2 free tier for 24/7 data collection.

**Current deployment:** EC2 t2.micro, eu-central-1 (Frankfurt), Elastic IP: `63.178.163.203`

## 1. AWS Account & EC2 Setup (Free Tier)

1. **Create AWS account** at [aws.amazon.com](https://aws.amazon.com/) (12 months free tier)
2. **Go to EC2:** Search "EC2" in top bar → click **EC2**
3. **Set region:** Top-right corner → **eu-central-1 (Frankfurt)** (low latency to HL)
4. **Launch EC2 instance:** Click **Launch instance**
   - **Name:** `botmm-recorder`
   - **AMI:** Amazon Linux 2023 (Free tier eligible, default)
   - **Instance type:** **t2.micro** (1 vCPU, 1GB RAM — free tier)
   - **Key pair:** Click **Create new key pair**
     - Name: `botmm-key`, Type: RSA, Format: `.pem`
     - **Save the `.pem` file!** You need it for SSH access
   - **Network:** Edit → ✅ Allow SSH traffic → **My IP** (not Anywhere!)
   - **Storage:** **8GB gp3** (default, 30GB included in free tier)
   - Click **Launch instance**

5. **Elastic IP (stałe IP, darmowe gdy przypisane):**
   - Left menu → **Elastic IPs** (under Network & Security)
   - **Allocate Elastic IP address** → **Allocate**
   - Select IP → **Actions** → **Associate Elastic IP address**
   - Select instance `botmm-recorder` → **Associate**

## 2. Connect & Deploy

```bash
# Fix PEM permissions (Windows — required, otherwise SSH rejects key)
icacls botmm-key.pem /inheritance:r /grant:r "%USERNAME%:(R)"

# SSH into EC2
ssh -i botmm-key.pem ec2-user@<elastic-ip>

# Install git (not pre-installed on AL2023)
sudo dnf install -y git

# Clone repo
git clone https://github.com/SusBot-cyber/BotMM.git
cd BotMM

# Run setup (installs Python 3.11, venv, systemd service, logrotate, cron)
sudo bash deploy/setup_ec2.sh

# Configure (optional — Discord webhook for alerts)
nano deploy/.env  # add your Discord webhook URL
```

## 3. Start Recording

```bash
# Start service
sudo systemctl start botmm-recorder

# Check status
sudo systemctl status botmm-recorder

# Follow live logs
journalctl -u botmm-recorder -f
```

Service auto-starts on boot and auto-restarts on crash (30s delay).

## 4. Monitor

```bash
# Check recent logs
journalctl -u botmm-recorder --since "1 hour ago"

# Check disk usage
du -sh data/orderbook/

# Check today's data files
ls -la data/orderbook/BTC/$(date +%Y-%m-%d)/

# Run health check manually
bash deploy/monitor.sh
```

Automated monitoring runs every 5 minutes via cron. Checks: service status, disk usage, data freshness, HL API reachability, memory. Sends Discord alerts on issues (rate-limited to 1 per 30min).

## 5. Download Data (Local Machine)

```bash
# From your local machine (Windows) — download all orderbook data
scp -i botmm-key.pem -r ec2-user@<elastic-ip>:~/BotMM/data/orderbook/ ./data/orderbook/

# Download specific day
scp -i botmm-key.pem -r ec2-user@<elastic-ip>:~/BotMM/data/orderbook/BTC/2026-02-15/ ./data/orderbook/BTC/2026-02-15/
```

## 6. S3 Backup (Optional)

```bash
# Install AWS CLI
sudo dnf install -y aws-cli

# Configure credentials
aws configure

# Test sync
bash deploy/sync_to_s3.sh

# Add to crontab (every 6 hours)
crontab -e
# Add: 0 */6 * * * /home/ec2-user/BotMM/deploy/sync_to_s3.sh >> /home/ec2-user/BotMM/logs/s3sync.log 2>&1
```

## 7. Cost Estimate

| Resource | Free Tier | After Free Tier |
|----------|-----------|-----------------|
| EC2 t2.micro | FREE (750h/month, 12 months) | ~$8.50/month |
| EBS 8GB gp3 | FREE (30GB included) | ~$0.64/month |
| Data transfer | FREE (<100GB/month out) | $0.09/GB |
| S3 (optional) | FREE (5GB, 12 months) | ~$0.023/GB/month |
| **Total** | **$0/month** | ~$9.14/month |

## 8. Storage Estimate

| Data Type | Per Day | Per Month | Per Year |
|-----------|---------|-----------|----------|
| L2 snapshots (20 levels, 3 symbols) | ~50 MB | ~1.5 GB | ~18 GB |
| Trades | ~10 MB | ~300 MB | ~3.6 GB |
| **Total** | **~60 MB** | **~1.8 GB** | **~21.6 GB** |

With 8GB EBS: **~4 months** before cleanup needed. Use S3 sync + local download to offload.

## 9. Service Management

```bash
# Start / stop / restart
sudo systemctl start botmm-recorder
sudo systemctl stop botmm-recorder
sudo systemctl restart botmm-recorder

# View service status
sudo systemctl status botmm-recorder

# View logs (last 100 lines)
journalctl -u botmm-recorder -n 100

# View logs since time
journalctl -u botmm-recorder --since "2026-02-15 10:00"

# Disable auto-start on boot
sudo systemctl disable botmm-recorder
```

## 10. Troubleshooting

| Problem | Solution |
|---------|----------|
| `UNPROTECTED PRIVATE KEY FILE` | Windows: `icacls botmm-key.pem /inheritance:r /grant:r "%USERNAME%:(R)"` |
| `git: command not found` | `sudo dnf install -y git` (not pre-installed on AL2023) |
| `crontab: command not found` | `sudo dnf install -y cronie && sudo systemctl enable crond --now` |
| Service won't start | `journalctl -u botmm-recorder -e` — check error logs |
| No data files | Check WebSocket connectivity, HL API status |
| Disk full | `find data/orderbook -name "*.csv" -mtime +30 -delete` |
| High CPU | Reduce symbols (`--symbols BTC`) or levels (`--levels 10`) |
| Service keeps restarting | Check `systemctl status botmm-recorder` for exit code |
| Python not found | Verify venv: `ls -la /home/ec2-user/BotMM/venv/bin/python` |
| Permission denied | `chown -R ec2-user:ec2-user /home/ec2-user/BotMM` |
| Memory OOM | Reduce `MemoryMax` in service file or reduce symbols |
| SSH timeout | Check Security Group → Inbound rules → SSH from your current IP |
| Changed home IP | Update Security Group: EC2 → Security Groups → Edit inbound → update SSH rule |

## File Structure

```
deploy/
├── README.md              # This file
├── .env.example           # Configuration template
├── setup_ec2.sh           # One-time EC2 setup script
├── botmm-recorder.service # Systemd unit file
├── logrotate.conf         # Log rotation for CSV files
├── sync_to_s3.sh          # S3 backup script (optional)
└── monitor.sh             # Health check script (cron)
```
