#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v docker >/dev/null || { echo "Install Docker Engine with Compose first."; exit 1; }
docker compose version >/dev/null
command -v openssl >/dev/null || { echo "Install openssl first."; exit 1; }
if [[ ! -f .env ]]; then
  read -r -p "Public DNS hostname (e.g. audio.example.com): " domain
  read -r -p "Let's Encrypt account email: " email
  read -r -p "Public WebSocket port [8843]: " port
  port="${port:-8843}"
  [[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$domain" == *.* ]] || { echo "Invalid DNS hostname"; exit 1; }
  [[ "$email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "Invalid email"; exit 1; }
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo "Invalid port"; exit 1; }
  port=$((10#$port))
  (( port >= 1 && port <= 65535 )) || { echo "Port must be 1-65535"; exit 1; }
  echo "Use a Production Classic Developer Key, not OTE/test credentials or a PAT."
  read -r -s -p "GoDaddy API key (hidden): " gd_key
  printf '\n'
  read -r -s -p "GoDaddy API secret (hidden): " gd_secret
  printf '\n'
  [[ "$gd_key" =~ ^[A-Za-z0-9_-]+$ && "$gd_secret" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "Invalid classic key/secret format"; exit 1; }
  umask 077
  {
    printf 'RELAY_DOMAIN=%s\nRELAY_PORT=%s\nACME_EMAIL=%s\n' "$domain" "$port" "$email"
    printf 'GODADDY_API_TOKEN=%s:%s\n' "$gd_key" "$gd_secret"
    printf 'SENDER_KEY=%s\n' "$(openssl rand -hex 32)"
    printf 'RECEIVER_KEY=%s\n' "$(openssl rand -hex 32)"
  } > .env
  unset gd_key gd_secret
  echo "Created private .env with DNS credentials and separate random access keys."
else
  echo "Using existing .env. It must include GODADDY_API_TOKEN=production-key:production-secret."
fi
if ! grep -q '^ADMIN_PASSWORD=.' .env; then
  umask 077
  printf '\nADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
  echo "Generated a dashboard password in .env (username: admin)."
fi
chmod 600 .env
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d
echo "Services started. Check certificate issuance with: docker compose logs --tail=100 caddy"
echo "Allow inbound TCP on your configured relay port only. Ports 80 and 443 are NOT needed for DNS validation."
echo "Allow outbound HTTPS and DNS. DNS must be hosted by GoDaddy authoritative nameservers."
echo "Caddy renews certificates automatically. Keep its volumes; do not use docker compose down -v."
echo "Read the hostname, port and role keys from .env and enter them in the Windows clients."

echo "Dashboard: https://YOUR_RELAY_DOMAIN:YOUR_RELAY_PORT/admin"
echo "Username: admin. View the password privately with: grep '^ADMIN_PASSWORD=' .env"
