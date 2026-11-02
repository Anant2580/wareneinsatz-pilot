# Deploying the public demo

The demo runs on a small Linux server with Docker, next to other projects, behind the server's
shared Caddy for HTTPS (see decision 38 in `docs/decisions.md`).

```
Internet ── 443 ── Caddy (transit stack) ──edge network── ui (Streamlit) ── api (FastAPI) ── db (PostgreSQL)
                                                          └──────────── project network "wareneinsatz" ─────┘
```

## First install

```bash
# 1. once: the transit stack's Caddy gets the shared network and the sites folder
cd /opt/berlin-transit-delays && sudo ./deploy/update.sh --public

# 2. this project
sudo git clone https://github.com/Anant2580/wareneinsatz-pilot /opt/wareneinsatz-pilot
cd /opt/wareneinsatz-pilot && sudo ./deploy/update.sh
```

`update.sh` prints the owner password once (sign in on the web app to upload, review and
approve) and the address, `https://wareneinsatz.<server-ip>.sslip.io`.

## Updates

```bash
cd /opt/wareneinsatz-pilot && sudo ./deploy/update.sh
```

Pulls the code, rebuilds the image, applies migrations and restarts. Data stays.

## Useful commands

```bash
cd /opt/wareneinsatz-pilot/deploy
sudo docker compose ps                       # status
sudo docker compose logs -f api ui           # logs
sudo grep UI_PASSWORD .env                   # the owner password
sudo docker compose down                     # stop (data stays in the volumes)
```

| File | What it is |
|---|---|
| `compose.yml` | database, setup job, API and web app; only the web app joins `edge` |
| `setup.sh` | migrations, ingredients, recipes, demo data, one invoice for the review screen |
| `update.sh` | install / update with one command; creates `deploy/.env` with the secrets |
| `wareneinsatz.caddy` | the Caddy site (HTTPS, security headers, upload size limit) |
| `.env` | secrets, created on the server, never in git |
