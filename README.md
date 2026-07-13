# Self-Hosted Media Stack

Docker Compose stack for a media server with:

- Jellyfin
- Tailscale
- Prowlarr
- Radarr
- Sonarr
- Bazarr
- FlareSolverr
- qBittorrent
- Jellyseerr
- Duplicati
- Dozzle
- Beszel

This README captures the setup decisions and troubleshooting notes for future maintenance.

## 1. Repo files

- `docker-compose.yml`: full stack definition
- `.env`: local runtime values (not committed)
- `.env.example`: template for `.env`
- `.gitignore`: excludes `.env` and backup artifacts

## 2. Core concepts used in this stack

### Volume mapping direction

In Compose, volumes are mapped as:

- `host_path:container_path`

Example:

- `/home/francisco/jellyfin/config:/config`

Meaning:

- left side is your machine path
- right side is inside-container path

### Network model

Most services join the user-defined network `media`, so they can reach each other by service name:

- `protonvpn:8080` (qBittorrent shares ProtonVPN network namespace)
- `flaresolverr:8191`
- `prowlarr:9696`

Tailscale is running as its own service and currently in userspace mode.

### Permissions model

LinuxServer containers use:

- `PUID`
- `PGID`
- `UMASK=002`

`UMASK=002` means new files/folders are writable by owner and group (not world-writable), which helps Radarr/Sonarr/qBittorrent interoperability.

## 3. Current paths and layout

Set in `.env`:

- `CONFIG_ROOT` for app configs
- `MEDIA_ROOT` as the single entry point for media
- `DOWNLOADS_ROOT` for downloads

Future-proofing choice:

- Keep all media-related mounts tied to `MEDIA_ROOT`/`DOWNLOADS_ROOT`
- If you later move to mergerfs (pooled disks), you can mostly change `.env` instead of editing all services

## 4. Services and default ports

- Jellyfin: `8096`
- Prowlarr: `9696`
- Radarr: `7878`
- Sonarr: `8989`
- Bazarr: `6767`
- FlareSolverr: `8191`
- qBittorrent Web UI: `8080`
- Jellyseerr: `5055`
- Duplicati: `8200`
- Dozzle: `8085`
- Beszel: `8090` bound to host loopback by default
- Jellyfin usage/fee tracker: `8092` bound to host loopback by default

## 5. Bring up / inspect / restart

From repo root:

```bash
docker compose --env-file .env up -d
docker compose ps
docker compose logs -f <service>
docker compose restart <service>
docker compose down
```

## 6. Internal service-to-service hostnames

Inside containers, do not use `localhost` for other services.

Use service names from compose, for example:

- FlareSolverr in Prowlarr: `http://flaresolverr:8191`
- qBittorrent behind ProtonVPN: `http://protonvpn:8080`

For ProtonVPN port-forward sync with Gluetun:

- Keep qBittorrent WebUI enabled on `QBITTORRENT_WEBUI_PORT` (default `8080`)
- In qBittorrent WebUI, enable `Bypass authentication for clients on localhost`

Why: `localhost` inside Prowlarr points to Prowlarr itself, not FlareSolverr.

## 7. Tailscale behavior in this setup

### Current mode

Tailscale runs in userspace mode:

- `TS_USERSPACE=true`

This was selected because kernel/host-mode had reliability issues in this environment.

### Accessing services remotely

MagicDNS hostname is stable for this node while identity/hostname remains the same.

Example:

- `http://<hostname>.<tailnet>.ts.net:8096/`

### Optional `tailscale serve`

You can proxy services through Tailscale explicitly if needed:

```bash
docker exec tailscale tailscale serve --bg --http=8096 http://jellyfin:8096
docker exec tailscale tailscale serve --bg --http=8989 http://sonarr:8989
docker exec tailscale tailscale serve --bg --http=7878 http://radarr:7878
docker exec tailscale tailscale serve --bg --http=<tailnet-port> http://<service-name>:<container-port>
```

Check configured serves:

```bash
docker exec tailscale tailscale serve status
```

With persistent state mounted (`${CONFIG_ROOT}/tailscale/state`), serve config usually survives restarts.

## 8. Jellyfin usage and fee tracking

The `jellyfin-usage` service reads Jellyfin's own user database and the Playback Reporting plugin database:

- read-only Jellyfin data: `${CONFIG_ROOT}/jellyfin/config/data/jellyfin.db`
- read-only Playback Reporting data: `${CONFIG_ROOT}/jellyfin/config/data/playback_reporting.db`
- writable fee/payment state: `${CONFIG_ROOT}/jellyfin-usage/jellyfin_usage.db`

It ranks users by watch time over the last 30 days, keeps a configurable history window of at least 90 days, and stores fee status separately so Jellyfin's databases are not modified.

Before first start, set these in `.env`:

```bash
JELLYFIN_USAGE_PORT=8092
JELLYFIN_USAGE_PASSWORD=replace-with-a-private-password
JELLYFIN_USAGE_MONTHLY_FEE=0
JELLYFIN_USAGE_CURRENCY=USD
JELLYFIN_USAGE_HISTORY_DAYS=90
```

Start or rebuild the service:

```bash
docker compose --env-file .env up -d --build jellyfin-usage
```

Open it locally:

```bash
http://127.0.0.1:8092/
```

Expose it through the tailnet:

```bash
./scripts/tailscale-serve-jellyfin-usage.sh
docker exec tailscale tailscale serve status
```

If Tailscale runs on the host instead of a container and the script returns `Access denied`, grant your user operator rights once:

```bash
sudo tailscale set --operator=$USER
./scripts/tailscale-serve-jellyfin-usage.sh
tailscale serve status
```

## 9. Beszel monitoring

Beszel provides host and container resource monitoring with historical data and alerts.

The stack runs:

- `beszel`: web hub, persisted at `${CONFIG_ROOT}/beszel/data`
- `beszel-agent`: local server/container collector, with read-only Docker socket access

Before first start, set these in `.env` if you want values different from the defaults:

```bash
BESZEL_PORT=8090
BESZEL_APP_URL=http://localhost:8090
```

Start the Beszel hub:

```bash
docker compose --env-file .env up -d beszel
```

Open it locally:

```bash
http://127.0.0.1:8090/
```

Expose it through the tailnet:

```bash
tailscale serve --bg --http=8090 http://127.0.0.1:8090
tailscale serve status
```

If Tailscale runs as a container on the `media` Docker network, use the container route instead:

```bash
docker exec tailscale tailscale serve --bg --http=8090 http://beszel:8090
docker exec tailscale tailscale serve status
```

On first login, create the admin account. Then add the local system using Beszel's UI. Copy the generated token and public key into `.env`:

```bash
BESZEL_AGENT_TOKEN=...
BESZEL_AGENT_KEY=...
```

Restart the agent:

```bash
docker compose --env-file .env up -d beszel-agent
```

When adding the local system in Beszel, use this Host / IP value:

```bash
/beszel_socket/beszel.sock
```

## 10. Duplicati instead of custom backup scripts

Duplicati is the backup solution for this stack.

Mounted sources in container:

- `/source/config` (app configs)
- `/source/media` (media)
- `/source/downloads` (downloads)

Recommended backup scope for disaster recovery:

- prioritize app configs (`/source/config`), especially Jellyfin/Radarr/Sonarr/Prowlarr/Bazarr/qBittorrent/Jellyseerr
- back up media according to your storage/bandwidth strategy

Important: local-only backups are not enough for disaster recovery. Prefer offsite destinations (cloud/NAS/remote).

## 11. Known gotchas and fixes

### A. Jellyseerr warning about `/app/config`

For `ghcr.io/seerr-team/seerr`, the config path must be:

- `/app/config`

Using `/config` causes non-persistent state warnings.

### B. Jellyfin playback errors after folder rename

If logs show missing files under a path like `/media/Movies/...` but disk paths are `/media/movies/...`, this is a Linux case-sensitivity mismatch.

Fix:

- correct Jellyfin library paths
- rescan libraries
- optionally remove missing media entries

### C. Radarr showing wrong free space

If containers show much less space than host, verify Docker context:

```bash
docker context ls
```

Running on Docker Desktop context (`desktop-linux`) can reflect VM/filesharing limits instead of raw host disk capacity.

### D. Permission denied across arr stack

Verify ownership/permissions on host paths (`CONFIG_ROOT`, `MEDIA_ROOT`, `DOWNLOADS_ROOT`) match `PUID:PGID` and that directories are writable by group when needed.

## 11. Operational checklist (new machine or rebuild)

1. Clone repo
2. Copy env template:

```bash
cp .env.example .env
```

3. Set correct values in `.env` (`PUID`, `PGID`, paths, timezone, tailscale auth key)
4. Ensure host directories exist and have correct ownership
5. Start stack:

```bash
docker compose --env-file .env up -d
```

6. Verify service health with `docker compose ps` and logs
7. Configure apps (indexers/download client/library roots)

## 12. Security notes

- Do not commit `.env`
- Rotate auth keys if exposed
- Keep containers and host updated regularly
- Restrict remote exposure to Tailscale rather than open public ports when possible
- Set `JELLYFIN_USAGE_PASSWORD` before exposing the usage/fee tracker through Tailscale

## 13. Quick diagnostics

```bash
# stack status
docker compose ps

# per-service logs
docker logs --tail 200 jellyfin
docker logs --tail 200 tailscale
docker logs --tail 200 prowlarr

# tailscale status from container
docker exec tailscale tailscale status
docker exec tailscale tailscale ip -4
docker logs --tail 200 jellyfin-usage

# check in-container disk visibility
docker exec radarr df -h /movies /downloads

# check ProtonVPN forwarded port == qBittorrent listen port
./check-protonvpn-port-sync.sh
```

## 14. Public HTTPS with Dynamic IP (Caddy + Dynu/DuckDNS/No-IP)

This stack now includes:

- `caddy` for reverse proxy + automatic TLS certs
- `ddclient` for Dynamic DNS updates when your public IP changes

### Why this works without a static IP

- your DDNS provider hostname points to your current public IP
- `ddclient` updates that DNS record whenever the IP changes
- Caddy serves your apps by domain and renews certs automatically

### Required router/network setup

1. Forward WAN TCP `80` -> server TCP `80`
2. Forward WAN TCP `443` -> server TCP `443`
3. Ensure your ISP is not behind CGNAT

If you are behind CGNAT, inbound ports usually cannot reach your server from the internet. In that case use Tailscale or a tunnel solution instead of direct public exposure.

### Configure Dynu (recommended)

1. Create a Dynu hostname (or use your own domain hosted in Dynu).
2. Copy template to live config:

```bash
source .env
mkdir -p "${CONFIG_ROOT}/ddclient"
cp ddclient/ddclient.dynu.conf.example "${CONFIG_ROOT}/ddclient/ddclient.conf"
```

3. Edit `${CONFIG_ROOT}/ddclient/ddclient.conf`:
- set `login=` to your Dynu username
- set `password=` to your Dynu password or IP update password
- set the last line to your full Dynu hostname (for example `myhost.dynu.net`)

### Configure DuckDNS (alternative)

1. Create one or more DuckDNS domains.
2. Copy template to live config:

```bash
source .env
mkdir -p "${CONFIG_ROOT}/ddclient"
cp ddclient/ddclient.duckdns.conf.example "${CONFIG_ROOT}/ddclient/ddclient.conf"
```

3. Edit `${CONFIG_ROOT}/ddclient/ddclient.conf`:
- set `password=` to your DuckDNS token
- set the last line to your DuckDNS subdomain(s), comma-separated, no `.duckdns.org` suffix

### Configure No-IP (alternative)

1. Create a No-IP hostname and DDNS key (recommended).
2. Copy template to live config:

```bash
source .env
mkdir -p "${CONFIG_ROOT}/ddclient"
cp ddclient/ddclient.noip.conf.example "${CONFIG_ROOT}/ddclient/ddclient.conf"
```

3. Edit `${CONFIG_ROOT}/ddclient/ddclient.conf`:
- set `login=` and `password=` (No-IP or DDNS key credentials)
- set last line to your full hostname (for example `myhost.ddns.net`)

### Configure Caddy domains

Set these in `.env`:

- `ACME_EMAIL`
- `JELLYFIN_DOMAIN`
- `JELLYSEERR_DOMAIN`
- `SONARR_DOMAIN`
- `RADARR_DOMAIN`
- `PROWLARR_DOMAIN`
- `QBITTORRENT_DOMAIN`
- `NTFY_DOMAIN`

Then start/restart:

```bash
docker compose --env-file .env up -d caddy ddclient
docker compose --env-file .env up -d
```

Check logs:

```bash
docker logs --tail 200 -f ddclient
docker logs --tail 200 -f caddy
```

## 14. CrowdSec (IP reputation + GeoIP blocking)

This repo now includes a `crowdsec` service that:

- reads `caddy` logs directly from Docker
- applies HTTP attack scenarios (`crowdsecurity/caddy`, `crowdsecurity/http-cve`)
- enriches events with GeoIP (`crowdsecurity/geoip-enrich`)
- applies a local country block scenario (`local/caddy-geo-block`)

### Files added

- `crowdsec/acquis.d/caddy.yaml`
- `crowdsec/scenarios/local-caddy-geo-block.yaml`
- `crowdsec/parsers/s02-enrich/local-whitelist.yaml`
- `crowdsec/parsers/s02-enrich/jellyseerr-whitelist.yaml`
- `crowdsec/parsers/s02-enrich/shelfmark-whitelist.yaml`
- `crowdsec/parsers/s02-enrich/ntfy-whitelist.yaml`

### 1) Set environment value

In `.env` set:

- `CROWDSEC_BOUNCER_KEY` to a long random alphanumeric key

### 2) Start CrowdSec

```bash
docker compose --env-file .env up -d crowdsec
docker logs --tail 200 -f crowdsec
```

### 3) Verify parser/scenario loading

```bash
docker exec crowdsec cscli collections list
docker exec crowdsec cscli parsers list
docker exec crowdsec cscli scenarios list
```

### 4) Enable actual blocking (required)

CrowdSec detects and decides; it does not block by itself.  
Install the firewall bouncer on the host:

```bash
# nftables hosts
sudo apt install crowdsec-firewall-bouncer-nftables

# or iptables hosts
sudo apt install crowdsec-firewall-bouncer-iptables
```

Then set the bouncer key from `.env` in:

- `/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml`

Minimal values:

```yaml
api_url: http://127.0.0.1:8087
api_key: <CROWDSEC_BOUNCER_KEY>
mode: nftables
```

Restart bouncer:

```bash
sudo systemctl restart crowdsec-firewall-bouncer
sudo systemctl status crowdsec-firewall-bouncer
```

Verify the bouncer is pulling decisions:

```bash
docker exec crowdsec cscli bouncers list
```

The `firewall` bouncer should show a recent `Last API pull`.

### 5) Test the setup

Safe CrowdSec decision test:

```bash
docker exec crowdsec cscli decisions add --ip 203.0.113.123 --duration 2m --reason setup-test
docker exec crowdsec cscli decisions list
docker exec crowdsec cscli decisions delete --ip 203.0.113.123
```

After the firewall bouncer is installed, confirm it saw the test:

```bash
docker exec crowdsec cscli bouncers list
sudo journalctl -u crowdsec-firewall-bouncer -n 50 --no-pager
```

Live HTTP test from an external network:

```bash
curl -k https://<your-public-domain>/.env
curl -k https://<your-public-domain>/.git/config
docker exec crowdsec cscli alerts list --limit 20
docker exec crowdsec cscli decisions list
```

If you accidentally ban yourself:

```bash
docker exec crowdsec cscli decisions delete --ip <your-public-ip>
```

### 6) Tune country allowlist

Edit:

- `crowdsec/scenarios/local-caddy-geo-block.yaml`

Default allowed countries:

- `UY`, `ES`

Change the list in this expression:

```yaml
evt.Enriched.IsoCode not in ['UY', 'ES']
```

Then restart crowdsec:

```bash
docker compose --env-file .env restart crowdsec
```

### 7) Useful CrowdSec commands

Overall health and parser status:

```bash
docker exec crowdsec cscli metrics
```

Active bans currently being enforced:

```bash
docker exec crowdsec cscli decisions list
```

Recent alerts:

```bash
docker exec crowdsec cscli alerts list --limit 50
```

Geo-blocked IPs:

```bash
docker exec crowdsec cscli alerts list --scenario local/caddy-geo-block --limit 50
```

Inspect one alert:

```bash
docker exec crowdsec cscli alerts inspect <alert-id>
```

Check that the host firewall bouncer is connected:

```bash
docker exec crowdsec cscli bouncers list
systemctl status crowdsec-firewall-bouncer --no-pager
```

Follow raw Caddy access logs:

```bash
docker logs -f caddy
```

Show recent Caddy logs:

```bash
docker logs --tail 100 caddy
```

Unban an IP:

```bash
docker exec crowdsec cscli decisions delete --ip <ip-address>
```

Add a short manual test ban:

```bash
docker exec crowdsec cscli decisions add --ip 203.0.113.123 --duration 2m --reason setup-test
docker exec crowdsec cscli decisions list
docker exec crowdsec cscli decisions delete --ip 203.0.113.123
```

### Useful hardening extras

- Keep admin apps private (VPN-only).
- Leave `local-whitelist.yaml` for LAN/Tailscale ranges to avoid self-bans.
- Leave app-specific whitelists scoped to their specific hostnames so normal app browsing does not trip HTTP crawl detection globally.
- Review decisions periodically:

```bash
docker exec crowdsec cscli decisions list
docker exec crowdsec cscli alerts list --limit 50
```
