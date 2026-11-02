#!/usr/bin/env bash
# Install or update the public demo on the server with one command:
#
#   sudo ./deploy/update.sh
#
# What it does:
#   1. downloads the latest code from GitHub
#   2. makes sure deploy/.env has a database password, an API key and the owner password
#      (created once, then kept; the owner password is printed the first time)
#   3. builds the image and (re)starts database, setup job, API and web app
#   4. gives the server's Caddy a site file for https://wareneinsatz.<server-ip>.sslip.io and reloads it
# Data in the database is never deleted.
#
# Needs: Docker with Compose, and the shared Caddy from the Berlin transit stack (it creates the
# Docker network "edge" and reads site files from /opt/caddy-sites).

set -euo pipefail
cd "$(dirname "$0")"   # deploy/

get_env() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
set_env() {
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}

# Latest code first, then restart this script so the new version of it runs
if [ -z "${UPDATE_SH_REEXEC:-}" ]; then
  echo "==> Getting the latest code"
  git pull --ff-only
  UPDATE_SH_REEXEC=1 exec "$0" "$@"
fi

touch .env
chmod 600 .env   # secrets: readable by root only

ensure_secret() {
  if [ -z "$(get_env "$1")" ]; then
    echo "==> Creating $1"
    set_env "$1" "$2"
  fi
}
ensure_secret POSTGRES_PASSWORD "$(openssl rand -hex 24)"
ensure_secret API_KEY "$(openssl rand -hex 32)"
if [ -z "$(get_env UI_PASSWORD)" ]; then
  set_env UI_PASSWORD "$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  echo "==> Your owner password for the web app (sign in to upload and approve):"
  echo "      $(get_env UI_PASSWORD)"
  echo "    It stays in deploy/.env on the server: sudo grep UI_PASSWORD deploy/.env"
fi

if [ -z "$(get_env PUBLIC_HOST)" ]; then
  ip="$(curl -fsS https://api.ipify.org)"
  set_env PUBLIC_HOST "wareneinsatz.${ip//./-}.sslip.io"   # sslip.io: free DNS name for any IP
fi
host="$(get_env PUBLIC_HOST)"

docker network inspect edge > /dev/null 2>&1 || docker network create edge > /dev/null

echo "==> Building the image (a few minutes the first time)"
docker compose build

echo "==> Starting database, setup, API and web app"
docker compose up -d --remove-orphans --wait

echo "==> HTTPS route via Caddy: https://$host"
sites="/opt/caddy-sites"
mkdir -p "$sites"
sed "s|__HOST__|$host|" wareneinsatz.caddy > "$sites/wareneinsatz.caddy"
caddy="$(docker ps -q --filter label=com.docker.compose.service=caddy | head -n 1)"
if [ -n "$caddy" ]; then
  docker exec "$caddy" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
else
  echo "    No Caddy container is running. Update the transit stack first:"
  echo "      cd /opt/berlin-transit-delays && sudo ./deploy/update.sh --public"
fi

docker compose ps --format 'table {{.Service}}\t{{.Status}}'
echo "==> Done: https://$host"
