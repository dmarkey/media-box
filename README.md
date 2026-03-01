# media-box

An MCP (Model Context Protocol) server for managing a home media stack — Jellyfin, qBittorrent, Jackett, and TVMaze. Gives LLM agents the ability to search for media, download torrents, organize files with Jellyfin naming conventions, and refresh libraries — all through a single set of tools.

## What it does

An LLM connected to this server can handle requests like *"download Breaking Bad season 3"* end-to-end:

1. Check if it already exists in Jellyfin
2. Look up episode metadata on TVMaze
3. Search for torrents via Jackett
4. Add the best result to qBittorrent
5. Wait for the download to complete
6. Move and rename files to match Jellyfin conventions
7. Trigger a library refresh

The server exposes 19 tools across five services. It ships with an `instructions` prompt (`SKILL.md`) that teaches the LLM the full workflow, so it knows how to chain the tools together without extra prompting.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Running instances of:
  - [Jellyfin](https://jellyfin.org/) media server
  - [qBittorrent](https://www.qbittorrent.org/) with Web UI enabled
  - [Jackett](https://github.com/Jackett/Jackett) torrent indexer proxy
- Internet access for [TVMaze](https://www.tvmaze.com/api) (public API, no key needed)

## Configuration

Create a config file at `~/.config/media-box/config`:

```ini
JELLYFIN_URL=http://localhost:8096
JELLYFIN_API_KEY=your-jellyfin-api-key

QBITTORRENT_URL=http://localhost:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=your-password

JACKETT_URL=http://localhost:9117
JACKETT_API_KEY=your-jackett-api-key

TEMP_DOWNLOAD_LOCATION=/path/to/downloads
TV_SHOWS_SAVE_LOCATION=/path/to/jellyfin/tv
MOVIES_SAVE_LOCATION=/path/to/jellyfin/movies
```

Alternatively, set these as environment variables. The server checks the config file first, then falls back to env vars.

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
| `jellyfin_refresh` | Trigger a metadata refresh for one or all libraries |

### qBittorrent

| Tool | Description |
|------|-------------|
| `qbt_list` | List torrents with filters for name, tag, category, and state. "Completed" state includes seeding. |
| `qbt_info` | Detailed torrent info — progress, speed, ETA, save path, file list |
| `qbt_wait` | Block until a torrent completes, errors, or times out (default 30 min) |
| `qbt_delete` | Delete one or more torrents by hash, optionally removing downloaded files |

### Jackett

| Tool | Description |
|------|-------------|
| `jackett_search` | Search for torrents across all configured indexers, with category and sort options |
| `jackett_add` | Add a search result to qBittorrent by reference (e.g. `"a3f2c1:3"`) |

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
