# media-box

An MCP (Model Context Protocol) server for managing a home media stack — Jellyfin, qBittorrent, torrent search, and TVMaze. Gives LLM agents the ability to search for media, download torrents, organize files with Jellyfin naming conventions, and refresh libraries — all through a single set of tools.

## What it does

An LLM connected to this server can handle requests like *"download Breaking Bad season 3"* end-to-end:

1. Check if it already exists in Jellyfin
2. Look up episode metadata on TVMaze
3. Search for torrents across multiple indexers (with Cloudflare bypass)
4. Add the best result to qBittorrent
5. Wait for the download to complete
6. Move and rename files to match Jellyfin conventions
7. Trigger a library refresh

The server exposes 22 tools across four services. It ships with an `instructions` prompt (`SKILL.md`) that teaches the LLM the full workflow, so it knows how to chain the tools together without extra prompting.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Running instances of:
  - [Jellyfin](https://jellyfin.org/) media server
  - [qBittorrent](https://www.qbittorrent.org/) with Web UI enabled
- Internet access for [TVMaze](https://www.tvmaze.com/api) (public API, no key needed)

Torrent search is built in via [pyackett](https://github.com/dmarkey/pyackett) — no separate Jackett server needed.

## Configuration

Create a config file at `~/.config/media-box/config`:

```ini
# Jellyfin
JELLYFIN_URL=http://localhost:8096
JELLYFIN_API_KEY=your-jellyfin-api-key

# qBittorrent
QBITTORRENT_URL=http://localhost:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=your-password

# Torrent search — comma-separated list of indexer IDs
TORRENT_INDEXERS=1337x,therarbg,thepiratebay,limetorrents,torrentproject2

# Proxy for torrent sites (optional, recommended for Cloudflare-protected sites)
TORRENT_PROXY=socks5://user:pass@host:1080

# Storage paths
TEMP_DOWNLOAD_LOCATION=/path/to/downloads
TV_SHOWS_SAVE_LOCATION=/path/to/jellyfin/tv
MOVIES_SAVE_LOCATION=/path/to/jellyfin/movies
```

Alternatively, set these as environment variables. The server checks the config file first, then falls back to env vars.

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

### Optional: qBittorrent path mapping

If qBittorrent runs in a container with different mount paths:

```ini
QBITTORRENT_HOST_PATH=/host/path/to/downloads
QBITTORRENT_CONTAINER_PATH=/container/path/to/downloads
```

The server translates paths automatically when communicating with qBittorrent.

## Running the MCP server

No installation required — `uvx` fetches and runs the server directly from GitHub.

```bash
uvx --from git+https://github.com/dmarkey/media-box.git media-box-mcp
```

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "media-box": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dmarkey/media-box.git", "media-box-mcp"]
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "media-box": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/dmarkey/media-box.git", "media-box-mcp"]
    }
  }
}
```

### Any MCP client

The server speaks MCP over stdio. Point your client at:

```bash
uvx --from git+https://github.com/dmarkey/media-box.git media-box-mcp
```

## Important: MCP timeout configuration

The `qbt_wait` tool blocks until a torrent finishes downloading, which can take up to 30 minutes. Most MCP clients have a default tool call timeout that is much shorter than this and will kill the connection before the download completes.

**You must increase your MCP client's timeout** to at least 1800 seconds (30 minutes) for this server to work properly. How to do this depends on your client:

- **Claude Code:** Set `"timeout"` in your MCP server config in `~/.claude/settings.json`:
  ```json
  {
    "mcpServers": {
      "media-box": {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/dmarkey/media-box.git", "media-box-mcp"],
        "timeout": 1800
      }
    }
  }
  ```
- **Other clients:** Consult your client's documentation for how to set per-server or global tool call timeouts.

## Tools

### Jellyfin

| Tool | Description |
|------|-------------|
| `jellyfin_search` | Search the media library by name, with optional type filter (movie/series/episode) |
| `jellyfin_libraries` | List all media libraries |
| `jellyfin_episodes` | List episodes for a series, with optional season filter |
| `jellyfin_refresh` | Trigger a library scan to detect new/removed files |

### Torrents

| Tool | Description |
|------|-------------|
| `torrent_search` | Search for torrents across configured indexers. Returns a numbered list. |
| `torrent_download` | Download result #N from the last search. Resolves link, adds to client, waits for completion — all in one call. |
| `torrent_list` | List active/completed torrents with filters for name, category, state |
| `torrent_info` | Detailed torrent info — progress, speed, ETA, save path, file list |
| `torrent_wait` | Block until a torrent completes, errors, or times out |
| `torrent_delete` | Delete a torrent by name or hash prefix, optionally removing files |

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
