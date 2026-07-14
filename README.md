# media-box

An MCP (Model Context Protocol) server for managing a home media stack — Jellyfin, a built-in BitTorrent client, torrent search, and TVMaze. Gives LLM agents the ability to search for media, download torrents, organize files with Jellyfin naming conventions, and refresh libraries — all through a single set of tools.

## What it does

An LLM connected to this server can handle requests like *"download Breaking Bad season 3"* end-to-end:

1. Check if it already exists in Jellyfin
2. Look up episode metadata on TVMaze
3. Search for torrents across multiple indexers (with Cloudflare bypass)
4. Add the best result to the built-in torrent client and verify it's healthy
5. Wait for the download to complete
6. Move and rename files to match Jellyfin conventions
7. Trigger a library refresh

The server exposes 24 tools across four services. It ships with an `instructions` prompt (`SKILL.md`) that teaches the LLM the full workflow, so it knows how to chain the tools together without extra prompting.

The server runs as a persistent HTTP service (MCP streamable HTTP transport). The torrent client is embedded — downloads keep running and torrents keep seeding even while no MCP client is connected, and session state persists across restarts.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A running [Jellyfin](https://jellyfin.org/) media server
- Internet access for [TVMaze](https://www.tvmaze.com/api) (public API, no key needed)

Torrent search is built in via [pyackett](https://github.com/dmarkey/pyackett) — no separate Jackett server needed. The torrent client is built in via [libtorrent](https://libtorrent.org/) — no qBittorrent needed.

## Configuration

Create a config file at `~/.config/media-box/config`:

```ini
# Jellyfin
JELLYFIN_URL=http://localhost:8096
JELLYFIN_API_KEY=your-jellyfin-api-key

# Torrent search — comma-separated list of indexer IDs
TORRENT_INDEXERS=1337x,therarbg,thepiratebay,limetorrents,torrentproject2

# Proxy for indexer websites (optional, recommended for Cloudflare-protected sites)
TORRENT_SEARCH_PROXY=socks5://user:pass@host:1080

# Storage paths
TEMP_DOWNLOAD_LOCATION=/path/to/downloads
TV_SHOWS_SAVE_LOCATION=/path/to/jellyfin/tv
MOVIES_SAVE_LOCATION=/path/to/jellyfin/movies

# MCP server transport (all optional)
#MCP_TRANSPORT=http        # http (streamable HTTP, default), sse, or stdio
#MCP_HOST=127.0.0.1
#MCP_PORT=8765
#MCP_STATELESS=true        # stateless HTTP (default) — clients survive server restarts
```

Alternatively, set these as environment variables. The server checks the config file first, then falls back to env vars.

### Torrent client tuning (all optional)

The built-in libtorrent client ships with defaults tuned for fast downloads on a home connection: generous connection limits, fast peer ramp-up, announce to every tracker in the torrent, upload capped at 1 MB/s, encryption forced, seed to 1.0 ratio or 60 minutes then stop. Every knob is overridable in the config file:

```ini
TORRENT_PORT=6881                  # listen port
TORRENT_LISTEN_INTERFACE=          # bind IP (default: auto-detect default route)
TORRENT_MAX_CONNECTIONS=500        # global connection limit
TORRENT_MAX_CONNECTIONS_PER_TORRENT=200
TORRENT_CONNECTION_SPEED=100       # peer connection attempts/sec
TORRENT_MAX_UPLOADS=8              # global unchoke slots (peers you reciprocate to)
TORRENT_DOWNLOAD_RATE_LIMIT=0      # bytes/s, 0 = unlimited
TORRENT_UPLOAD_RATE_LIMIT=1048576  # bytes/s
TORRENT_ENCRYPTION=forced          # forced/enabled/disabled — "enabled" sees more peers (faster), "forced" is more private
TORRENT_ANNOUNCE_ALL_TRACKERS=true # announce to every tracker, not just the first working one
TORRENT_ACTIVE_DOWNLOADS=8         # simultaneous active downloads before queueing
TORRENT_SEED_RATIO=1.0             # stop seeding at this ratio
TORRENT_SEED_TIME=60               # or after this many minutes
TORRENT_STALL_TIMEOUT=120          # remove torrents with no seeders after N seconds
TORRENT_PROXY_URL=                 # socks5://host:port for peer traffic (separate from search proxy)
TORRENT_ANONYMOUS_MODE=false
```

DHT, LSD, uTP, UPnP, and NAT-PMP are on by default (`TORRENT_ENABLE_DHT` etc. to disable). Torrent session state lives in `~/.config/media-box/torrents/` — downloads resume automatically after a restart.

> **Download speed tip:** inbound connectability makes a big difference on smaller swarms. The client tries UPnP/NAT-PMP automatically; check `torrent_logs()` for `portmap` entries to confirm your router cooperated. If mapping fails, forward `TORRENT_PORT` (6881) manually.

### Torrent indexers

Public indexers that work well (no account needed):

| Indexer ID | Site | Notes |
|---|---|---|
| `1337x` | 1337x.to | Large, general. Cloudflare protected. |
| `thepiratebay` | thepiratebay.org | The classic. JSON API. |
| `therarbg` | therarbg.to | RarBG successor. JSON API. |
| `limetorrents` | limetorrents.fun | General. |
| `torrentproject2` | torrentproject2.net | Meta-search, high result count. |
| `kickasstorrents-ws` | kickass.ws | KAT clone. Cloudflare protected. |
| `eztv` | eztvx.to | TV-focused. Cloudflare protected. |
| `torrentdownload` | torrentdownload.info | General. |

Sites behind Cloudflare are handled automatically via [Camoufox](https://github.com/daijro/camoufox) (anti-detect Firefox). The first search to a CF-protected site takes ~5 seconds to solve the challenge; subsequent searches reuse the cached cookie.

### Private trackers

Private trackers need credentials. Add them to the config file using the indexer ID as a prefix:

```ini
# IPTorrents (cookie auth)
TORRENT_INDEXERS=1337x,therarbg,iptorrents
IPTORRENTS_COOKIE=uid=123456; pass=abcdef123456
IPTORRENTS_USERAGENT=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...
```

Then in `torrents.py`, pass the credentials when configuring:

```python
await pk.configure_indexer("iptorrents", {
    "cookie": config.get_env("IPTORRENTS_COOKIE"),
    "useragent": config.get_env("IPTORRENTS_USERAGENT"),
})
```

> **Note:** Private tracker support currently requires a code change to pass credentials. A future update will add automatic credential loading from config.

To find the cookie: open the tracker in your browser → DevTools → Application → Cookies → copy the full cookie string.

### Optional: path mapping

If Jellyfin sees the media at different paths than this server (NAS mounts, Docker volumes, NFS):

```ini
LOCAL_PATH_PREFIX=/mnt/nas/media
JELLYFIN_PATH_PREFIX=/media
```

## Running the MCP server

No installation required — `uvx` fetches and runs the server directly from GitHub:

```bash
uvx --from git+https://github.com/dmarkey/media-box.git media-box-mcp
```

The server starts on `http://127.0.0.1:8765/mcp` using the MCP streamable HTTP transport. Because the torrent client is embedded, run it as a long-lived service — downloads and seeding continue between agent sessions.

Options: `--transport http|sse|stdio`, `--host`, `--port` (or the `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` config keys). The legacy SSE transport is served at `/sse`.

The HTTP transport runs stateless by default, so you can restart or redeploy the server without breaking connected agents — no stale-session 404s. Set `MCP_STATELESS=false` to restore server-side sessions.

<details>
<summary>Example systemd unit</summary>

```ini
[Unit]
Description=media-box MCP server
After=network-online.target

[Service]
ExecStart=/usr/local/bin/uvx --from git+https://github.com/dmarkey/media-box.git media-box-mcp
Restart=on-failure

[Install]
WantedBy=default.target
```

</details>

### Claude Code

```bash
claude mcp add --transport http media-box http://127.0.0.1:8765/mcp
```

Or in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "media-box": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "timeout": 1800
    }
  }
}
```

### Any MCP client

Point any client that supports streamable HTTP at `http://<host>:8765/mcp`. Clients that only speak stdio can still launch the server directly with `media-box-mcp --transport stdio` — but note the torrent client then lives and dies with that single client connection.

## Important: MCP timeout configuration

The `torrent_wait` tool blocks until a torrent finishes downloading, which can take up to 30 minutes (`torrent_download` itself returns as soon as the torrent proves healthy — usually seconds, ~2 minutes worst case). Most MCP clients have a default tool call timeout that is much shorter and will kill the request before the download completes.

**If you use `torrent_wait`, increase your MCP client's tool timeout** to at least 1800 seconds (30 minutes) — e.g. `"timeout": 1800` in the Claude Code server config shown above. For other clients, consult their documentation for per-server or global tool call timeouts.

## Tools

### Jellyfin

| Tool | Description |
|------|-------------|
| `jellyfin_search` | Search the media library by name, with optional type filter (movie/series/episode) |
| `jellyfin_libraries` | List all media libraries |
| `jellyfin_episodes` | List episodes for a series, with optional season filter |
| `jellyfin_refresh` | Trigger a library scan to detect new/removed files |
| `jellyfin_devices` | List devices that accept remote playback control |
| `jellyfin_play` | Start playing an item on a remote device |
| `jellyfin_command` | Send a playback command (Pause, Stop, SetVolume, ...) to a device |

### Torrents

| Tool | Description |
|------|-------------|
| `torrent_search` | Search for torrents across configured indexers. Returns a numbered list. |
| `torrent_download` | Download result #N from a search. Resolves the link, adds it to the client, and returns as soon as the torrent proves healthy (usually 10-20s) — dead torrents (no seeders) are auto-removed. |
| `torrent_list` | List active/completed torrents with filters for name, category, state |
| `torrent_info` | Detailed torrent info — progress, speed, ETA, trackers, save path, file list |
| `torrent_peers` | List connected peers for a torrent |
| `torrent_wait` | Block until a torrent completes, errors, or times out |
| `torrent_delete` | Delete a torrent by name or hash prefix, optionally removing files |
| `torrent_logs` | Recent libtorrent engine logs for troubleshooting |

### TVMaze

| Tool | Description |
|------|-------------|
| `tvmaze_search` | Search for TV shows |
| `tvmaze_show` | Get show details (status, rating, genres, summary) |
| `tvmaze_episodes` | List episodes with airdates and runtimes |
| `tvmaze_seasons` | List seasons with episode counts |
| `tvmaze_lookup` | Look up a show by IMDB or TVDB ID |

### File Mover

| Tool | Description |
|------|-------------|
| `mover_list` | List files in the temp download directory |
| `mover_movie` | Move a movie file into the library with Jellyfin naming: `Title (Year)/Title (Year).ext` |
| `mover_tv` | Move a single TV episode with naming: `Show/Season XX/Show - SXXEXX - Title.ext` |
| `mover_tv_batch` | Move an entire season of episodes in a single call |

## License

[MIT](LICENSE)
