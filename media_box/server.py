import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import config
from .formatting import (
    format_progress,
    format_size,
    format_table,
    strip_html,
    truncate,
)
from .jellyfin import JellyfinClient
from .qbittorrent import QBittorrentClient, STATE_MAP
from .jackett import CATEGORY_MAP, JackettClient, SEARCH_DIR
from .tvmaze import TVMazeClient
from .mover import MEDIA_EXTENSIONS

mcp = FastMCP(
    "media-box",
    instructions=Path(__file__).with_name("SKILL.md").read_text(),
)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _jellyfin_config() -> tuple[str, str]:
    return tuple(config.require_env("JELLYFIN_URL", "JELLYFIN_API_KEY"))  # type: ignore[return-value]


def _qbt_config() -> tuple[str, str, str]:
    return tuple(  # type: ignore[return-value]
        config.require_env("QBITTORRENT_URL", "QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD")
    )


def _jackett_config() -> tuple[str, str]:
    return tuple(config.require_env("JACKETT_URL", "JACKETT_API_KEY"))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Jellyfin tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def jellyfin_search(query: str, type: Optional[str] = None) -> str:
    """Search the Jellyfin media library.

    Args:
        query: Search term
        type: Filter by type — "movie", "series", or "episode". Omit to search all.
    """
    url, key = _jellyfin_config()
    type_map = {"movie": "Movie", "series": "Series", "episode": "Episode"}
    item_types = type_map.get(type, "Movie,Series,Episode") if type else "Movie,Series,Episode"

    async with JellyfinClient(url, key) as client:
        items = await client.search_items(query, item_types=item_types)

    rows = []
    for it in items:
        rows.append({
            "name": it.get("Name", ""),
            "type": it.get("Type", ""),
            "year": str(it.get("ProductionYear", "")),
            "id": it.get("Id", ""),
        })
    return format_table(rows, [
        ("Name", "name", 40),
        ("Type", "type", 10),
        ("Year", "year", 6),
        ("ID", "id", 36),
    ])


@mcp.tool()
async def jellyfin_libraries() -> str:
    """List all Jellyfin media libraries."""
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        libs = await client.get_libraries()

    rows = []
    for lib in libs:
        rows.append({
            "name": lib.get("Name", ""),
            "type": lib.get("CollectionType", ""),
            "id": lib.get("Id", ""),
        })
    return format_table(rows, [
        ("Name", "name", 30),
        ("Type", "type", 15),
        ("ID", "id", 36),
    ])


@mcp.tool()
async def jellyfin_episodes(series_id: str, season: Optional[int] = None) -> str:
    """List episodes for a Jellyfin series.

    Args:
        series_id: The Jellyfin series ID
        season: Filter to a specific season number
    """
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        episodes = await client.get_episodes(series_id, season=season)

    rows = []
    for ep in episodes:
        s = ep.get("ParentIndexNumber") or ep.get("SeasonNumber") or ""
        e = ep.get("IndexNumber", "")
        rows.append({
            "se": f"S{s:02d}E{e:02d}" if isinstance(s, int) and isinstance(e, int) else f"S{s}E{e}",
            "name": ep.get("Name", ""),
            "id": ep.get("Id", ""),
        })
    return format_table(rows, [
        ("Ep", "se", 8),
        ("Name", "name", 45),
        ("ID", "id", 36),
    ])


@mcp.tool()
async def jellyfin_refresh(library_id: Optional[str] = None) -> str:
    """Trigger a Jellyfin library metadata refresh.

    Args:
        library_id: Refresh a specific library. Omit to refresh all libraries.
    """
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        if library_id:
            await client.refresh_library(library_id)
            return f"Refreshed library {library_id}"
        else:
            await client.refresh_all_libraries()
            return "Refreshed all libraries"


# ---------------------------------------------------------------------------
# qBittorrent tools
# ---------------------------------------------------------------------------


def _format_eta(seconds: int) -> str:
    if seconds <= 0 or seconds >= 8640000:
        return "∞" if seconds >= 8640000 else "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _find_torrent(torrents: list[dict], query: str) -> Optional[dict]:
    q = query.lower()
    for t in torrents:
        if t.get("hash", "").lower().startswith(q):
            return t
    for t in torrents:
        if q in t.get("name", "").lower():
            return t
    return None


@mcp.tool()
async def qbt_list(
    filter: Optional[str] = None,
    tag: Optional[str] = None,
    category: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """List torrents in qBittorrent.

    Args:
        filter: Filter torrents by name substring
        tag: Filter by qBittorrent tag
        category: Filter by qBittorrent category
        state: Filter by state (Downloading, Completed, Error, Missing, Stalled, Paused, Queued, Checking, Moving). "Completed" includes both finished and seeding torrents.
    """
    url, user, pw = _qbt_config()
    async with QBittorrentClient(url, user, pw) as client:
        torrents = await client.get_torrents(category=category, tag=tag)

    if filter:
        filt = filter.lower()
        torrents = [t for t in torrents if filt in t.get("name", "").lower()]

    if state:
        state_filter = state.lower()
        if state_filter == "completed":
            # "Completed" means done downloading — includes seeding/completed/paused-after-finish
            completed_states = {"seeding", "completed"}
            torrents = [
                t for t in torrents
                if STATE_MAP.get(t.get("state", ""), t.get("state", "")).lower() in completed_states
            ]
        else:
            torrents = [
                t for t in torrents
                if STATE_MAP.get(t.get("state", ""), t.get("state", "")).lower() == state_filter
            ]

    rows = []
    for t in torrents:
        rows.append({
            "name": t.get("name", ""),
            "size": format_size(t.get("size")),
            "progress": format_progress(t.get("progress", 0)),
            "state": STATE_MAP.get(t.get("state", ""), t.get("state", "")),
            "hash": t.get("hash", "")[:12],
        })
    return format_table(rows, [
        ("Name", "name", 0),
        ("Size", "size", 10),
        ("Progress", "progress", 28),
        ("State", "state", 12),
        ("Hash", "hash", 12),
    ])


@mcp.tool()
async def qbt_info(query: str) -> str:
    """Get detailed info about a torrent — progress, speed, ETA, save path, files.

    Args:
        query: Torrent hash (or prefix) or name substring
    """
    url, user, pw = _qbt_config()
    async with QBittorrentClient(url, user, pw) as client:
        torrents = await client.get_torrents()
        torrent = _find_torrent(torrents, query)
        if not torrent:
            return f"No torrent matching '{query}'"

        h = torrent["hash"]
        files = await client.get_torrent_files(h)

    state = STATE_MAP.get(torrent.get("state", ""), torrent.get("state", ""))
    progress = torrent.get("progress", 0)
    save_path = config.to_host_path(
        torrent.get("save_path") or torrent.get("content_path", "")
    )

    lines = [
        f"Name:       {torrent.get('name')}",
        f"Hash:       {h}",
        f"State:      {state}",
        f"Progress:   {format_progress(progress)}",
        f"Size:       {format_size(torrent.get('size'))}",
        f"Downloaded: {format_size(torrent.get('downloaded'))}",
        f"Speed:      {format_size(torrent.get('dlspeed', 0))}/s",
        f"ETA:        {_format_eta(torrent.get('eta', 0))}",
        f"Save path:  {save_path}",
        f"Category:   {torrent.get('category', '')}",
        f"Tags:       {torrent.get('tags', '')}",
    ]

    if files:
        lines.append(f"\nFiles ({len(files)}):")
        for f in files:
            pct = f.get("progress", 0) * 100
            name = f.get("name", "")
            size = format_size(f.get("size"))
            lines.append(f"  {pct:5.1f}%  {size:>10s}  {name}")

    return "\n".join(lines)


@mcp.tool()
async def qbt_delete(queries: list[str], delete_files: bool = False) -> str:
    """Delete one or more torrents from qBittorrent.

    Args:
        queries: List of torrent hashes (or prefixes) or name substrings
        delete_files: Also delete downloaded files from disk
    """
    url, user, pw = _qbt_config()
    lines: list[str] = []
    async with QBittorrentClient(url, user, pw) as client:
        torrents = await client.get_torrents()

        resolved = []
        for query in queries:
            torrent = _find_torrent(torrents, query)
            if torrent:
                resolved.append(torrent)
            else:
                lines.append(f"No torrent matching '{query}'")

        if not resolved:
            lines.append("No torrents to delete")
            return "\n".join(lines)

        full_hashes = [t["hash"] for t in resolved]
        await client.delete_torrents(full_hashes, delete_files=delete_files)

        remaining = await client.get_torrents()
        remaining_hashes = {t.get("hash") for t in remaining}

        action = "Deleted torrent and files" if delete_files else "Deleted torrent"
        for t in resolved:
            h = t["hash"]
            if h in remaining_hashes:
                lines.append(f"Failed to delete: {t.get('name')} ({h[:12]})")
            else:
                lines.append(f"{action}: {t.get('name')} ({h[:12]})")

    return "\n".join(lines)


_DONE_STATES = {"uploading", "stalledUP", "pausedUP"}
_ERROR_STATES = {"error", "missingFiles"}
_NO_SEEDERS_TIMEOUT = 300


@mcp.tool()
async def qbt_wait(query: str, timeout: int = 1800) -> str:
    """Wait for a torrent to complete downloading. Blocks until done, error, or timeout.

    Args:
        query: Torrent hash (or prefix) or name substring
        timeout: Seconds to wait (default 1800, min 60, max 1800)
    """
    url, user, pw = _qbt_config()
    timeout = min(max(timeout, 60), 1800)
    interval = 10

    async with QBittorrentClient(url, user, pw) as client:
        torrents = await client.get_torrents()
        torrent = _find_torrent(torrents, query)
        if not torrent:
            return f"No torrent matching '{query}'"

        t_hash = torrent["hash"]
        name = torrent.get("name", t_hash[:12])

        elapsed = 0
        ever_had_seeders = False
        last_status = ""
        while elapsed < timeout:
            torrents = await client.get_torrents()
            torrent = _find_torrent(torrents, t_hash)
            if not torrent:
                return f"ERROR: Torrent disappeared: {name} ({t_hash[:12]})"

            state = torrent.get("state", "")
            progress = torrent.get("progress", 0)
            dlspeed = torrent.get("dlspeed", 0)
            eta = torrent.get("eta", 0)
            num_seeds = torrent.get("num_seeds", 0)
            friendly_state = STATE_MAP.get(state, state)

            if num_seeds > 0:
                ever_had_seeders = True

            last_status = (
                f"{format_progress(progress)}  "
                f"{format_size(dlspeed)}/s  "
                f"ETA {_format_eta(eta)}  "
                f"Seeds {num_seeds}  "
                f"[{friendly_state}]"
            )

            if state in _DONE_STATES:
                return f"Complete: {name} ({t_hash[:12]})\n{last_status}"

            if state in _ERROR_STATES:
                return f"ERROR: {name} state={friendly_state} ({t_hash[:12]})\n{last_status}"

            if elapsed >= _NO_SEEDERS_TIMEOUT and not ever_had_seeders:
                return (
                    f"DEAD TORRENT: {name} — no seeders connected after "
                    f"{_NO_SEEDERS_TIMEOUT // 60} minutes ({t_hash[:12]})\n{last_status}"
                )

            await asyncio.sleep(interval)
            elapsed += interval

        return (
            f"TIMEOUT after {timeout}s: {name} ({t_hash[:12]})\n{last_status}"
        )


# ---------------------------------------------------------------------------
# Jackett tools
# ---------------------------------------------------------------------------


def _cleanup_stale_searches(max_age_secs: int = 1800) -> None:
    if not SEARCH_DIR.exists():
        return
    import time
    cutoff = time.time() - max_age_secs
    for f in SEARCH_DIR.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def _save_search(search_id: str, query: str, results: list[dict]) -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_searches()
    (SEARCH_DIR / f"{search_id}.json").write_text(
        json.dumps({"query": query, "results": results})
    )


def _load_search(search_id: str) -> dict:
    path = SEARCH_DIR / f"{search_id}.json"
    if not path.exists():
        raise ValueError(f"Search '{search_id}' not found. Run a search first.")
    return json.loads(path.read_text())


@mcp.tool()
async def jackett_search(
    query: str,
    category: Optional[str] = None,
    limit: Optional[int] = None,
    sort: str = "seeders",
) -> str:
    """Search for torrents using Jackett.

    Args:
        query: Search term (e.g. "The Matrix 1999", "Breaking Bad S03")
        category: Filter by category — "movies" or "tv"
        limit: Maximum number of results to return
        sort: Sort results by "seeders" (default) or "size"
    """
    url, key = _jackett_config()
    cat_id = CATEGORY_MAP.get(category) if category else None
    search_limit = limit or 50

    async with JackettClient(url, key) as client:
        results = await client.search(query, category=cat_id, limit=search_limit)

    results.sort(
        key=lambda r: r.get("Seeders" if sort == "seeders" else "Size", 0),
        reverse=True,
    )

    if limit:
        results = results[:limit]

    search_id = secrets.token_hex(3)
    _save_search(search_id, query, results)

    rows = []
    for i, r in enumerate(results):
        rows.append({
            "num": str(i + 1),
            "title": r.get("Title", ""),
            "size": format_size(r.get("Size")),
            "seeders": str(r.get("Seeders", 0)),
            "leechers": str(r.get("Peers", 0)),
            "indexer": r.get("Tracker", ""),
        })
    table = format_table(rows, [
        ("#", "num", 4),
        ("Title", "title", 50),
        ("Size", "size", 10),
        ("S", "seeders", 5),
        ("L", "leechers", 5),
        ("Indexer", "indexer", 15),
    ])

    return f"{table}\n\nSearch ID: {search_id}  ({len(results)} results)\nTo add: use jackett_add with ref \"{search_id}:<number>\""


@mcp.tool()
async def jackett_add(
    ref: str,
    category: Optional[str] = None,
    tag: Optional[str] = None,
) -> str:
    """Add a Jackett search result to qBittorrent for downloading.

    Args:
        ref: Search result reference in format "search_id:number" (e.g. "a3f2c1:3")
        category: qBittorrent category (e.g. "mm-tv", "mm-movie")
        tag: qBittorrent tag for tracking this download
    """
    if ":" not in ref:
        return f"Error: expected format <search-id>:<number>, got '{ref}'"
    sid, num_str = ref.rsplit(":", 1)
    try:
        num = int(num_str)
    except ValueError:
        return f"Error: '{num_str}' is not a valid result number"
    if num < 1:
        return "Error: result number must be >= 1"
    idx = num - 1

    data = _load_search(sid)
    results = data["results"]

    if idx >= len(results):
        return f"Error: result #{num} out of range (search has {len(results)} results)"

    result = results[idx]
    title = result.get("Title", "unknown")
    magnet = result.get("MagnetUri") or result.get("magneturi")
    link = result.get("Link")

    jackett_url, jackett_key = _jackett_config()

    if magnet:
        source = magnet
    elif link:
        async with JackettClient(jackett_url, jackett_key) as client:
            resolved = await client.resolve_link(link)
        if isinstance(resolved, str):
            source = resolved
        else:
            tmp_path = SEARCH_DIR / f"{sid}_{idx}.torrent"
            tmp_path.write_bytes(resolved)
            source = str(tmp_path)
    else:
        return f"Error: result #{num} has no magnet or download link"

    save_path = config.get_env("TEMP_DOWNLOAD_LOCATION", "TEMPORARY_DOWNLOAD_LOCATION")
    if not save_path:
        save_path = str(Path(tempfile.gettempdir()) / "media-box" / "downloads")
        Path(save_path).mkdir(parents=True, exist_ok=True)

    qbt_url, qbt_user, qbt_pw = _qbt_config()
    async with QBittorrentClient(qbt_url, qbt_user, qbt_pw) as qbt:
        await qbt.add_torrent(
            source,
            save_path=config.to_container_path(save_path),
            category=category,
            tag=tag,
        )

    return f"Added to qBittorrent: {title}"


# ---------------------------------------------------------------------------
# TVMaze tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def tvmaze_search(query: str) -> str:
    """Search for TV shows on TVMaze.

    Args:
        query: Show name to search for
    """
    async with TVMazeClient() as client:
        results = await client.search_shows(query)

    rows = []
    for item in results:
        show = item.get("show", {})
        network = (show.get("network") or show.get("webChannel") or {}).get("name", "")
        rows.append({
            "id": str(show.get("id", "")),
            "name": show.get("name", ""),
            "year": (show.get("premiered") or "")[:4],
            "status": show.get("status", ""),
            "network": network,
            "score": f"{item.get('score', 0):.1f}",
        })
    return format_table(rows, [
        ("ID", "id", 7),
        ("Name", "name", 35),
        ("Year", "year", 6),
        ("Status", "status", 12),
        ("Network", "network", 18),
        ("Score", "score", 6),
    ])


@mcp.tool()
async def tvmaze_show(show_id: int) -> str:
    """Get detailed information about a TV show from TVMaze.

    Args:
        show_id: TVMaze show ID
    """
    async with TVMazeClient() as client:
        show = await client.get_show(show_id)

    if not show:
        return f"Show {show_id} not found"

    network = (show.get("network") or show.get("webChannel") or {}).get("name", "N/A")
    rating = show.get("rating", {}).get("average")
    genres = ", ".join(show.get("genres", [])) or "N/A"
    summary = strip_html(show.get("summary", ""))

    lines = [
        f"Name:      {show.get('name')}",
        f"ID:        {show.get('id')}",
        f"Status:    {show.get('status')}",
        f"Premiered: {show.get('premiered', 'N/A')}",
        f"Ended:     {show.get('ended', 'N/A')}",
        f"Network:   {network}",
        f"Rating:    {rating}/10" if rating else "Rating:    N/A",
        f"Genres:    {genres}",
        f"URL:       {show.get('url', 'N/A')}",
    ]
    if summary:
        lines.append(f"Summary:   {truncate(summary, 120)}")

    return "\n".join(lines)


@mcp.tool()
async def tvmaze_episodes(show_id: int, season: Optional[int] = None) -> str:
    """List episodes for a TV show from TVMaze.

    Args:
        show_id: TVMaze show ID
        season: Filter to a specific season number
    """
    async with TVMazeClient() as client:
        if season is not None:
            seasons = await client.get_seasons(show_id)
            season_id = None
            for s in seasons:
                if s.get("number") == season:
                    season_id = s["id"]
                    break
            if season_id is None:
                return f"Season {season} not found"
            episodes = await client.get_season_episodes(season_id)
        else:
            episodes = await client.get_episodes(show_id)

    rows = []
    for ep in episodes:
        s = ep.get("season", "")
        e = ep.get("number", "")
        code = f"S{s:02d}E{e:02d}" if isinstance(s, int) and isinstance(e, int) else f"S{s}E{e}"
        rows.append({
            "ep": code,
            "name": ep.get("name", ""),
            "airdate": ep.get("airdate", "TBA"),
            "runtime": str(ep.get("runtime", "")) + "m" if ep.get("runtime") else "",
        })
    return format_table(rows, [
        ("Ep", "ep", 8),
        ("Name", "name", 40),
        ("Airdate", "airdate", 12),
        ("Runtime", "runtime", 8),
    ])


@mcp.tool()
async def tvmaze_seasons(show_id: int) -> str:
    """List seasons for a TV show from TVMaze.

    Args:
        show_id: TVMaze show ID
    """
    async with TVMazeClient() as client:
        seasons = await client.get_seasons(show_id)

    rows = []
    for s in seasons:
        num = s.get("number")
        if num is None:
            continue
        rows.append({
            "num": str(num),
            "episodes": str(s.get("episodeOrder", "?")),
            "premiere": s.get("premiereDate", "TBA"),
            "end": s.get("endDate", "TBA"),
            "id": str(s.get("id", "")),
        })
    return format_table(rows, [
        ("#", "num", 4),
        ("Episodes", "episodes", 10),
        ("Premiere", "premiere", 12),
        ("End", "end", 12),
        ("Season ID", "id", 10),
    ])


@mcp.tool()
async def tvmaze_lookup(imdb: Optional[str] = None, tvdb: Optional[str] = None) -> str:
    """Look up a TV show on TVMaze by external ID.

    Args:
        imdb: IMDB ID (e.g. "tt0903747")
        tvdb: TheTVDB numeric ID
    """
    if not imdb and not tvdb:
        return "Error: provide either imdb or tvdb"

    params = {}
    if imdb:
        params["imdb"] = imdb
    elif tvdb:
        params["thetvdb"] = tvdb

    async with TVMazeClient() as client:
        show = await client.lookup_show(**params)

    if not show:
        return "No show found for given ID"

    network = (show.get("network") or show.get("webChannel") or {}).get("name", "N/A")
    lines = [
        f"Name:      {show.get('name')}",
        f"ID:        {show.get('id')}",
        f"Status:    {show.get('status')}",
        f"Premiered: {show.get('premiered', 'N/A')}",
        f"Network:   {network}",
        f"URL:       {show.get('url', 'N/A')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mover tools
# ---------------------------------------------------------------------------


def _resolve_source(source: Path) -> Path:
    if source.is_absolute():
        return source
    (temp_dir,) = config.require_env("TEMP_DOWNLOAD_LOCATION")
    return Path(temp_dir) / source


def _validate_source(source: Path) -> Optional[str]:
    if not source.is_file():
        return f"Error: source file not found: {source}"
    if source.suffix.lower() not in MEDIA_EXTENSIONS:
        return (
            f"Error: unrecognised extension '{source.suffix}'. "
            f"Allowed: {', '.join(sorted(MEDIA_EXTENSIONS))}"
        )
    return None


def _format_file_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@mcp.tool()
async def mover_list(path: Optional[str] = None) -> str:
    """List files in the temporary download location.

    Args:
        path: Subfolder to list (optional, relative to temp download dir)
    """
    (temp_dir,) = config.require_env("TEMP_DOWNLOAD_LOCATION")
    target = Path(temp_dir)
    if path:
        target = target / path

    if not target.exists():
        return f"Error: path not found: {target}"

    if target.is_file():
        return f"  {_format_file_size(target.stat().st_size):>10s}  {target.name}"

    entries = sorted(target.iterdir())
    if not entries:
        return "(empty)"

    lines = []
    for entry in entries:
        if entry.is_dir():
            lines.append(f"  {'[dir]':>10s}  {entry.name}/")
        else:
            lines.append(f"  {_format_file_size(entry.stat().st_size):>10s}  {entry.name}")
    return "\n".join(lines)


import shutil


def _copy_file(source: Path, dest: Path, *, force: bool = False) -> str:
    if dest.exists() and not force:
        return f"Error: destination already exists: {dest}\nUse force=true to overwrite."
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(source), str(dest))
    except BaseException:
        if dest.exists():
            dest.unlink()
        raise
    return f"Copied: {source}\n    -> {dest}"


async def _cleanup_torrent(torrent_hash: str) -> str:
    url, user, pw = config.require_env(
        "QBITTORRENT_URL", "QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD"
    )
    async with QBittorrentClient(url, user, pw) as client:
        await client.delete_torrent(torrent_hash, delete_files=True)
    return f"Deleted torrent {torrent_hash[:12]} and remaining files"


@mcp.tool()
async def mover_movie(
    source: str,
    dest_name: str,
    force: bool = False,
    torrent_hash: Optional[str] = None,
) -> str:
    """Move a movie file to the Jellyfin movies library with proper naming.

    Creates folder structure: Title (Year)/Title (Year).ext

    Args:
        source: Source file path (absolute, or relative to temp download dir)
        dest_name: Destination filename following Jellyfin convention (e.g. "The Matrix (1999).mkv")
        force: Overwrite if destination already exists
        torrent_hash: If provided, delete this torrent and its files after moving
    """
    (movies_dir,) = config.require_env("MOVIES_SAVE_LOCATION")
    source_path = _resolve_source(Path(source))

    err = _validate_source(source_path)
    if err:
        return err

    folder_name = Path(dest_name).stem
    dest = Path(movies_dir) / folder_name / dest_name

    result = _copy_file(source_path, dest, force=force)

    if torrent_hash:
        cleanup_result = await _cleanup_torrent(torrent_hash)
        result += f"\n{cleanup_result}"

    return result


@mcp.tool()
async def mover_tv(
    source: str,
    dest_name: str,
    show: str,
    season: int,
    force: bool = False,
    torrent_hash: Optional[str] = None,
) -> str:
    """Move a TV episode file to the Jellyfin TV library with proper naming.

    Creates folder structure: Show Name/Season XX/Show Name - SXXEXX - Title.ext

    Args:
        source: Source file path (absolute, or relative to temp download dir)
        dest_name: Destination filename (e.g. "Breaking Bad - S03E07 - One Minute.mkv")
        show: Show name (used for the top-level folder)
        season: Season number (used for the Season XX subfolder)
        force: Overwrite if destination already exists
        torrent_hash: If provided, delete this torrent and its files after moving (use on last episode only)
    """
    (tv_dir,) = config.require_env("TV_SHOWS_SAVE_LOCATION")
    source_path = _resolve_source(Path(source))

    err = _validate_source(source_path)
    if err:
        return err

    season_folder = f"Season {season:02d}"
    dest = Path(tv_dir) / show / season_folder / dest_name

    result = _copy_file(source_path, dest, force=force)

    if torrent_hash:
        cleanup_result = await _cleanup_torrent(torrent_hash)
        result += f"\n{cleanup_result}"

    return result


@mcp.tool()
async def mover_tv_batch(
    moves: list[dict],
    show: str,
    season: int,
    force: bool = False,
    torrent_hash: Optional[str] = None,
) -> str:
    """Move multiple TV episode files to the Jellyfin TV library in one operation.

    Use this instead of calling mover_tv repeatedly for each episode in a season.
    Creates folder structure: Show Name/Season XX/<dest_name>

    Args:
        moves: List of {"source": "<path>", "dest_name": "<filename>"} objects. Source paths can be relative to the temp download dir. Example: [{"source": "torrent-folder/ep01.mkv", "dest_name": "Breaking Bad - S03E01 - No Mas.mkv"}, ...]
        show: Show name (used for the top-level folder)
        season: Season number (used for the Season XX subfolder)
        force: Overwrite if destinations already exist
        torrent_hash: If provided, delete this torrent and its files after all moves complete
    """
    (tv_dir,) = config.require_env("TV_SHOWS_SAVE_LOCATION")
    season_folder = f"Season {season:02d}"
    lines: list[str] = []
    errors = 0

    for i, move in enumerate(moves):
        src = move.get("source", "")
        dest_name = move.get("dest_name", "")
        if not src or not dest_name:
            lines.append(f"#{i + 1}: SKIPPED — missing source or dest_name")
            errors += 1
            continue

        source_path = _resolve_source(Path(src))
        err = _validate_source(source_path)
        if err:
            lines.append(f"#{i + 1}: {err}")
            errors += 1
            continue

        dest = Path(tv_dir) / show / season_folder / dest_name
        try:
            result = _copy_file(source_path, dest, force=force)
            lines.append(f"#{i + 1}: {result}")
        except Exception as e:
            lines.append(f"#{i + 1}: ERROR — {e}")
            errors += 1

    if torrent_hash and errors == 0:
        cleanup_result = await _cleanup_torrent(torrent_hash)
        lines.append(cleanup_result)
    elif torrent_hash and errors > 0:
        lines.append(f"Torrent cleanup skipped — {errors} error(s) occurred")

    lines.append(f"\n{len(moves) - errors}/{len(moves)} files moved")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run()


if __name__ == "__main__":
    main()
