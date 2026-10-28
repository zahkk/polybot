#!/usr/bin/env bash
# Run once on a fresh Ubuntu 24.04 VPS as root:  bash deploy/setup.sh
set -euo pipefail
apt-get update
apt-get install -y python3 python3-venv python3-pip ufw
id polybot >/dev/null 2>&1 || adduser --disabled-password --gecos "" polybot
SRC="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$SRC" != /home/polybot/polybot ]; then
  rm -rf /home/polybot/polybot && cp -r "$SRC" /home/polybot/polybot
fi
cd /home/polybot/polybot
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
chmod 600 .env
chown -R polybot:polybot /home/polybot/polybot
cp deploy/polybot.service /etc/systemd/system/polybot.service
systemctl daemon-reload
systemctl enable polybot
# Firewall: SSH only. The dashboard stays on localhost; use an SSH tunnel to see it.
ufw allow OpenSSH
ufw --force enable
echo
echo "Installed. Next: edit /home/polybot/polybot/.env, then: systemctl start polybot"
echo "Logs: journalctl -u polybot -f"
