# BotMM L2 Recorder — AWS Deployment Guide

Deploy the L2 order book recorder on AWS EC2 free tier for 24/7 data collection.

## 1. AWS Account Setup (Free Tier)

1. **Create AWS account** at [aws.amazon.com](https://aws.amazon.com/) (12 months free tier)
2. **Launch EC2 instance:**
   - AMI: Amazon Linux 2023
   - Instance type: **t2.micro** (1 vCPU, 1GB RAM — free tier)
   - Storage: **8GB gp3 EBS** (30GB included in free tier)
   - Security group: Allow **SSH (port 22)** from your IP only
3. **Create key pair** → download `.pem` file

## 2. Connect & Deploy

```bash
# SSH into EC2
ssh -i botmm-key.pem ec2-user@<public-ip>

# Clone repo
git clone https://github.com/SusBot-cyber/BotMM.git
cd BotMM

# Run setup (installs Python, venv, systemd service, logrotate, cron)
sudo bash deploy/setup_ec2.sh

# Configure
cp deploy/.env.example deploy/.env
nano deploy/.env  # add your Discord webhook URL
```

## 3. Start Recording

```bash
# Start service
sudo systemctl start botmm-recorder
sudo systemctl enable botmm-recorder

# Check status
sudo systemctl status botmm-recorder

# Follow live logs
journalctl -u botmm-recorder -f
```

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

Automated monitoring runs every 5 minutes via cron and sends Discord alerts on issues.

## 5. Download Data (Local Machine)

```bash
# From your local machine — download all orderbook data
scp -i botmm-key.pem -r ec2-user@<ip>:~/BotMM/data/orderbook/ ./data/orderbook/

# Download specific day
scp -i botmm-key.pem -r ec2-user@<ip>:~/BotMM/data/orderbook/BTC/2026-02-15/ ./data/orderbook/BTC/2026-02-15/
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
| Service won't start | `journalctl -u botmm-recorder -e` — check error logs |
| No data files | Check WebSocket connectivity, HL API status |
| Disk full | `find data/orderbook -name "*.csv" -mtime +30 -delete` |
| High CPU | Reduce symbols (`--symbols BTC`) or levels (`--levels 10`) |
| Service keeps restarting | Check `systemctl status botmm-recorder` for exit code |
| Python not found | Verify venv: `ls -la /home/ec2-user/BotMM/venv/bin/python` |
| Permission denied | `chown -R ec2-user:ec2-user /home/ec2-user/BotMM` |
| Memory OOM | Reduce `MemoryMax` in service file or reduce symbols |

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
