# mediastack

Home media server on a Dell OptiPlex 7040 (i5-6500, 16 GB) running Arch Linux + Docker.
Request in Seerr → Radarr/Sonarr find it (Prowlarr) → Real-Debrid via Decypharr,
falling back to qBittorrent behind ProtonVPN → Tdarr converts for the TV → Jellyfin plays it.

**Secrets are not in this repo.** `.env` (VPN key, Real-Debrid token, qBittorrent login) and the
app config folders (`config/`, API keys + databases) stay on the server only.

## Services (`docker-compose.yml`)

| Service | Port | Role |
|---|---|---|
| gluetun | — | ProtonVPN WireGuard with port forwarding; qBittorrent, Prowlarr and FlareSolverr run inside it |
| qbittorrent | 8080 | Torrent client (VPN only) |
| prowlarr | 9696 | Indexers |
| flaresolverr | 8191 (inside gluetun) | Cloudflare solver for Prowlarr |
| radarr / sonarr | 7878 / 8989 | Movies / TV + anime |
| lidarr | 8686 | Music → `/data/media/music` (Jellyfin "Music" library) |
| lidify | 5000 | Music discovery: similar artists via Last.fm → adds them to Lidarr |
| slskd | 5030 (via gluetun) | Soulseek client inside the VPN; shares the music library read-only |
| droppedneedle | 8688 | Request single tracks or albums → slskd → `/data/media/music` |
| metube | 8081 | yt-dlp web UI: YouTube audio → `/data/media/music/YouTube` |
| decypharr | 8282 | Real-Debrid client (qBittorrent-compatible API) |
| dlrouter | — | [decypharr-fallback](https://github.com/zetawars/decypharr-fallback): Decypharr first, qBittorrent when Real-Debrid refuses |
| tdarr | 8265 | Converts downloads to what the TV plays (flow: `tdarr/build-flow.js`) |
| jellyfin | 8096 | Playback; also reads the old library on the Passport read-only |
| seerr | 5055 | Requests (successor of Jellyseerr/Overseerr) |
| recyclarr | — | Syncs quality profiles / custom formats into the arrs |

Static IPs on the `media` network (172.30.0.0/24) so Prowlarr, which uses gluetun's DNS, can reach the arrs.

## Storage

| Drive | Mount | Role |
|---|---|---|
| 1 TB NVMe (btrfs, LVM) | `/`, `/home` …, `/data-nvme` (subvolume `@data`) | OS + first pool branch |
| Toshiba 500 GB (ext4) | `/mnt/media2` → branch `/mnt/media2/data` | second pool branch |
| WD My Passport 6 TB (exFAT, USB) | `/mnt/usb-library` → branch `MediaServer/pool` | last pool branch; old library in `MediaServer/*` (read-only in Jellyfin) |

`/data` is a **mergerfs** pool of the three branches (`setup-mediapool.sh`): `category.create=ff`, so new files go to the
NVMe until it has 150 GB left, then the Toshiba (20 GB reserve), and the Passport only when both are full.
Containers only ever see `/data` (`/data/torrents`, `/data/media/{movies,tv,anime}`), so hardlinks work and drives
can be added without touching the arrs. `/data-nvme` is its own subvolume so media stays out of snapper's root snapshots.

## Host setup scripts (run in this order on a fresh Arch install)

| Script | What it does |
|---|---|
| `setup-headless.sh` | SSH, `archlinux.local` via avahi, never sleep |
| `setup-usb-library.sh` | Mounts the Passport at `/mnt/usb-library`, SMB share for Windows |
| `setup-mediapool.sh` | mergerfs pool at `/data`, `@data` subvolume, Docker waits for the pool |
| `setup-vnc.sh` + `systemd/turbovnc.service` | TurboVNC virtual XFCE desktop on `10.0.0.42:1` |
| `make-snapshot15-permanent.sh` | One-off recovery script kept for reference (restored the system from snapper snapshot 15) |

## Restore from scratch

1. Install Arch (btrfs + snapper, Docker, `yay`), clone this repo to `~/mediastack`.
2. Run the setup scripts above with `sudo bash`.
3. `cp .env.example .env`, fill in every `CHANGE_ME`.
4. Restore `config/` from the server backup (not in git), or set the apps up again.
5. `docker compose up -d`

## Known issues / to do

- The AMD R5 340X card disables the Intel iGPU, so `INTEL_RENDER_NODE=/dev/dri/renderD128` is actually the AMD card:
  no Quick Sync for Tdarr/Jellyfin. Remove the card (or enable BIOS Multi-Display) and re-check the render node.
- `config/` (app settings + databases) needs its own backup, outside git.
