#!/usr/bin/env bash
# Instala a Central de Bots num Raspberry Pi OS (Bookworm, 64 bits). Corre com: sudo bash deploy/install_pi.sh
# Antes: copia o projeto para /opt/bots/app (git clone ou rsync), sem a pasta .venv nem data/.
set -euo pipefail

apt-get update
apt-get install -y python3-venv sqlite3 chrony
id bots >/dev/null 2>&1 || useradd -r -m -d /var/lib/bots -s /usr/sbin/nologin bots
mkdir -p /opt/bots/app /var/lib/bots/data /var/lib/bots/keys /var/lib/bots/backups
chown -R bots:bots /opt/bots /var/lib/bots
chmod 700 /var/lib/bots/data /var/lib/bots/keys

sudo -u bots python3 -m venv /opt/bots/venv
sudo -u bots /opt/bots/venv/bin/pip install -r /opt/bots/app/requirements.txt

[ -f /etc/bots.env ] || install -m 600 -o bots -g bots /opt/bots/app/deploy/bots.env.example /etc/bots.env
cp /opt/bots/app/deploy/bots-*.service /opt/bots/app/deploy/bots-*.timer /etc/systemd/system/
timedatectl set-ntp true
systemctl daemon-reload
systemctl enable --now bots-painel bots-corredor bots-backup.timer bots-vigia.timer

echo "Feito. Painel em http://127.0.0.1:5000 (acede por um túnel SSH ou por VPN: ssh -L 5000:127.0.0.1:5000 pi)."
echo "Cria a palavra-passe no primeiro acesso (só é permitido a partir do próprio Pi ou do túnel)."
echo "Logs:  journalctl -u bots-corredor -f"
