#!/usr/bin/env bash
# Atlas server install for a fresh Ubuntu 24.04 VPS. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/bilchukvolodymyr-cmyk/atlas/main/deploy/install.sh -o install.sh
#   bash install.sh                # address <ip>.sslip.io (free, no domain needed)
#   bash install.sh atlas.my.it    # or your own domain (DNS A record -> this server)
# Safe to run again (updates the code, keeps the database, token and key).
set -euo pipefail

REPO="${ATLAS_REPO:-https://github.com/bilchukvolodymyr-cmyk/atlas.git}"
APP=/opt/atlas/app
DATA=/var/lib/atlas
ENV_FILE=/etc/atlas/env

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

echo "== packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv git caddy ufw sqlite3

echo "== firewall: only SSH and HTTPS"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null    # needed once for the HTTPS certificate
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== user and folders"
id atlas >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin atlas
install -d -o atlas -g atlas -m 700 "$DATA" "$DATA/backups"
install -d -m 755 /opt/atlas /etc/atlas

echo "== code"
if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone -q "$REPO" "$APP"; fi
[ -d /opt/atlas/venv ] || python3 -m venv /opt/atlas/venv
/opt/atlas/venv/bin/pip install -q --upgrade pip
/opt/atlas/venv/bin/pip install -q "anthropic>=2.14"

echo "== secrets (kept only in $ENV_FILE, readable by root and atlas)"
if [ ! -f "$ENV_FILE" ]; then
  read -r -s -p "Paste ANTHROPIC_API_KEY (input hidden): " KEY; echo
  [ -n "$KEY" ] || { echo "key is empty"; exit 1; }
  TOKEN="$(openssl rand -hex 24)"
  umask 077
  printf 'ATLAS_TOKEN=%s\nANTHROPIC_API_KEY=%s\nATLAS_EFFORT=medium\n' "$TOKEN" "$KEY" > "$ENV_FILE"
  chown root:atlas "$ENV_FILE"; chmod 640 "$ENV_FILE"
fi

echo "== HTTPS address"
HOST="${1:-}"
if [ -z "$HOST" ]; then
  IP="$(curl -4 -fsS https://api.ipify.org)"
  HOST="${IP//./-}.sslip.io"
fi
sed "s/ATLAS_HOST/$HOST/" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
install -d -o caddy -g caddy /var/log/caddy

echo "== services"
cp "$APP/deploy/atlas.service" "$APP/deploy/atlas-backup.service" "$APP/deploy/atlas-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now atlas.service atlas-backup.timer
systemctl restart atlas.service
systemctl reload caddy || systemctl restart caddy

echo "== check"
for i in $(seq 1 30); do
  if curl -fsS "https://$HOST/health" >/dev/null 2>&1; then OK=1; break; fi
  sleep 2
done
if [ "${OK:-0}" = 1 ]; then
  echo "Atlas is up: https://$HOST"
else
  echo "Atlas service: $(systemctl is-active atlas.service); HTTPS not answering yet - see: journalctl -u caddy -n 50"
fi
echo "Address for the iPhone shortcut: https://$HOST/ingest"
echo "Token for the shortcut: sudo grep ATLAS_TOKEN $ENV_FILE"
