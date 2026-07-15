import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from . import config
from .formatting import (
    format_progress,
    format_size,
    format_table,
    strip_html,
    truncate,
)
from .jellyfin import JellyfinClient
from .torrent_client import (
    TorrentClient,
    STATE_MAP,
    get_client as get_torrent_client,
    shutdown as shutdown_torrent_client,
)
from .torrents import CATEGORY_MAP, SEARCH_DIR, search as torrent_search_fn, resolve_link as torrent_resolve_link
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


def _torrent_client() -> TorrentClient:
    save_path = config.get_env("TEMP_DOWNLOAD_LOCATION", "TEMPORARY_DOWNLOAD_LOCATION")
    return get_torrent_client(default_save_path=save_path)


# Start the torrent client eagerly so libtorrent begins listening immediately.
_torrent_client()


# ---------------------------------------------------------------------------
# Server-push events
#
# Clients opt in by calling `subscribe_events`; completion events are then
# broadcast to their sessions as MCP log notifications (logger="events") over
# the standalone GET stream. Requires stateful sessions (MCP_STATELESS=false)
# — in stateless mode a session doesn't outlive its request, so there is
# nothing to deliver on.
# ---------------------------------------------------------------------------

_event_sessions: set = set()
_announced_hashes: set = set()  # dedupe: recheck/restart can re-fire the alert


async def _broadcast_event(data: dict) -> None:
    for session in list(_event_sessions):
        try:
            await session.send_log_message(level="notice", data=data, logger="events")
        except Exception:
            _event_sessions.discard(session)  # dead client — drop its session


def _on_torrent_finished(data: dict) -> None:
    # called from the alert pump (already on the event loop); never block it
    if not _event_sessions or data.get("hash") in _announced_hashes:
        return
    _announced_hashes.add(data.get("hash"))
    try:
        asyncio.get_running_loop().create_task(_broadcast_event(data))
    except RuntimeError:
        pass  # no loop (e.g. during shutdown) — nothing to deliver to anyway


_torrent_client().on_torrent_finished = _on_torrent_finished


@mcp.tool()
async def subscribe_events(ctx: Context) -> str:
    """Subscribe this client session to server events (e.g. torrent completion).

    Events arrive as MCP log notifications with logger="events" and a JSON
    payload like {"event": "torrent_finished", "name", "hash", "save_path"}.
    Subscriptions are per-session: re-call this after reconnecting; calling it
    twice on one session is harmless.
    """
    if mcp.settings.stateless_http:
        return ("Cannot subscribe: the server is running stateless "
                "(MCP_STATELESS=true), so sessions don't outlive a request and "
                "events can't be delivered. Set MCP_STATELESS=false and restart.")
    _event_sessions.add(ctx.session)
    return "Subscribed to events on this session."


# ---------------------------------------------------------------------------
# Jellyfin tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def jellyfin_search(query: str, type: str = "") -> str:
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
async def jellyfin_episodes(series_id: str, season: int = 0) -> str:
    """List episodes for a Jellyfin series.

    Args:
        series_id: The Jellyfin series ID
        season: Filter to a specific season number. 0 means all seasons.
    """
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        episodes = await client.get_episodes(series_id, season=season or None)

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
async def jellyfin_refresh() -> str:
    """Trigger a Jellyfin library scan to detect newly added or removed files."""
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        await client.scan_library()
    return "Library scan triggered"


@mcp.tool()
async def jellyfin_devices() -> str:
    """List Jellyfin devices that support remote playback control.

    Only shows devices with an active Jellyfin app session that can receive
    play commands. Use the session ID from this list with jellyfin_play and
    jellyfin_command.
    """
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        sessions = await client.get_sessions()

    # Only include sessions that actually accept commands
    controllable = [
        s for s in sessions
        if s.get("Capabilities", {}).get("SupportsMediaControl")
        and s.get("Capabilities", {}).get("SupportedCommands")
    ]

    if not controllable:
        return "No controllable devices found. Ensure a Jellyfin client app is open on the target device."

    rows = []
    for s in controllable:
        now_playing = s.get("NowPlayingItem")
        playing_str = now_playing.get("Name", "?") if now_playing else ""
        rows.append({
            "device": s.get("DeviceName", "?"),
            "client": s.get("Client", "?"),
            "session_id": s.get("Id", ""),
            "playing": playing_str,
        })
    return format_table(rows, [
        ("Device", "device", 25),
        ("Client", "client", 25),
        ("Session ID", "session_id", 36),
        ("Now Playing", "playing", 30),
    ])


@mcp.tool()
async def jellyfin_play(session_id: str, item_id: str) -> str:
    """Start playing a Jellyfin item on a remote device.

    Use jellyfin_devices to find the session_id and jellyfin_search to find
    the item_id.

    Args:
        session_id: Target device session ID from jellyfin_devices
        item_id: Jellyfin item ID to play (movie, episode, etc.)
    """
    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        await client.play_on_session(session_id, [item_id])

        # Poll for playback confirmation (transcoding can delay startup)
        device_name = "?"
        for attempt in range(5):
            await asyncio.sleep(2)
            sessions = await client.get_sessions()
            for s in sessions:
                if s["Id"] == session_id:
                    device_name = s.get("DeviceName", "?")
                    now_playing = s.get("NowPlayingItem")
                    if now_playing:
                        ps = s.get("PlayState", {})
                        state = "paused" if ps.get("IsPaused") else "playing"
                        method = ps.get("PlayMethod", "?")
                        return f"Now {state}: {now_playing.get('Name', '?')} on {device_name} ({method})"

    return f"Play command sent to {device_name} but playback not confirmed after 10s. The device may not have responded."


PLAYBACK_COMMANDS = {
    "playpause", "pause", "unpause", "stop",
    "nexttrack", "previoustrack",
    "rewind", "fastforward",
    "setvolume", "mute", "unmute", "togglemute",
}


@mcp.tool()
async def jellyfin_command(session_id: str, command: str) -> str:
    """Send a playback command to a Jellyfin device.

    Args:
        session_id: Target device session ID from jellyfin_devices
        command: Playback command — one of: PlayPause, Pause, Unpause, Stop,
                 NextTrack, PreviousTrack, Rewind, FastForward,
                 SetVolume, Mute, Unmute, ToggleMute
    """
    if command.lower() not in PLAYBACK_COMMANDS:
        return f"Unknown command '{command}'. Valid commands: {', '.join(sorted(PLAYBACK_COMMANDS))}"

    url, key = _jellyfin_config()
    async with JellyfinClient(url, key) as client:
        await client.send_playback_command(session_id, command)
    return f"Sent {command} to session {session_id[:12]}..."


# ---------------------------------------------------------------------------
# torrent client tools
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


def _hash_from_magnet(magnet: str) -> Optional[str]:
    """Extract the info hash from a magnet URI."""
    m = re.search(r"urn:btih:([0-9a-fA-F]{40})", magnet)
    if m:
        return m.group(1).lower()
    # Some magnets use base32-encoded hashes
    m = re.search(r"urn:btih:([A-Za-z2-7]{32})", magnet)
    if m:
        import base64

        raw = base64.b32decode(m.group(1).upper())
        return raw.hex()
    return None


def _find_torrent(torrents: list[dict], query: str) -> Optional[dict]:
    q = query.lower()
    for t in torrents:
        if t.get("hash", "").lower().startswith(q):
            return t
    for t in torrents:
        if q in t.get("name", "").lower():
            return t
    return None


_DONE_STATES = {"uploading", "stalledUP", "pausedUP"}
_ERROR_STATES = {"error", "missingFiles"}
_NO_SEEDERS_TIMEOUT = int(config.get_env("TORRENT_STALL_TIMEOUT") or 120)
_last_search_id: Optional[str] = None


def _cleanup_stale_searches(max_age_secs: int = 1800) -> None:
    if not SEARCH_DIR.exists():
        return
    import time
    cutoff = time.time() - max_age_secs
    for f in SEARCH_DIR.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def _save_search(search_id: str, query: str, results: list[dict]) -> None:
    global _last_search_id
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_searches()
    (SEARCH_DIR / f"{search_id}.json").write_text(
        json.dumps({"query": query, "results": results})
    )
    _last_search_id = search_id


def _load_search(search_id: str) -> dict:
    path = SEARCH_DIR / f"{search_id}.json"
    if not path.exists():
        raise ValueError(f"Search '{search_id}' not found. Run torrent_search first.")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Torrent tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def torrent_search(
    query: str,
    category: str = "",
    limit: int = 0,
    sort: str = "seeders",
) -> str:
    """Search for torrents across configured indexers. Returns a numbered list.
    Use torrent_download with a result number to start downloading.

    Args:
        query: Search term (e.g. "The Matrix 1999", "Breaking Bad S03")
        category: Filter by category — "movies" or "tv"
        limit: Maximum number of results to return. 0 means default (50).
        sort: Sort results by "seeders" (default) or "size"
    """
    cat_id = CATEGORY_MAP.get(category) if category else None
    search_limit = limit or 50

    results = await torrent_search_fn(query, category=cat_id, limit=search_limit)

    results.sort(
        key=lambda r: r.get("Seeders" if sort == "seeders" else "Size", 0) or 0,
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
            "indexer": r.get("Tracker", ""),
        })
    table = format_table(rows, [
        ("#", "num", 4),
        ("Title", "title", 55),
        ("Size", "size", 10),
        ("S", "seeders", 5),
        ("Indexer", "indexer", 15),
    ])

    return (
        f"{table}\n\n{len(results)} results (search id: {search_id}). "
        f"Use torrent_download(number) to download."
    )


@mcp.tool()
async def torrent_download(
    number: int,
    timeout: int = 120,
    category: str = "",
    tag: str = "",
    search_id: str = "",
) -> str:
    """Download a torrent from the most recent search results.
    Resolves the download link, adds to the torrent client, and monitors it
    until its health is known — typically 10-20 seconds, up to ~2 minutes
    for slow starters.

    Returns as soon as the torrent is either:
    - Healthy and downloading (seeders connected, progress advancing) —
      tell the user it's downloading and they can check back later with
      torrent_list / torrent_info
    - Complete (small/fast torrents may finish within the window)
    - Dead (no seeders found) — auto-removed, suggest trying the next result
    - Errored — report the error

    IMPORTANT: Before downloading, ALWAYS check Jellyfin first (jellyfin_search)
    to confirm the movie/episode is not already in the library. Do not download duplicates.

    Args:
        number: Result number from torrent_search (e.g. 1, 2, 3)
        timeout: Max seconds to wait for health check (default 120, i.e. 2 minutes)
        category: Category tag for organizing (e.g. "tv", "movies")
        tag: Custom tag for tracking this download
        search_id: Search id from torrent_search output. Omit to use the most
            recent search — only needed to pick from an older search.
    """
    sid = search_id or _last_search_id
    if not sid:
        return "Error: no search results. Run torrent_search first."

    idx = number - 1
    if idx < 0:
        return "Error: number must be >= 1"

    try:
        data = _load_search(sid)
    except ValueError as e:
        return str(e)

    results = data["results"]
    if idx >= len(results):
        return f"Error: #{number} out of range (search has {len(results)} results)"

    result = results[idx]
    title = result.get("Title", "unknown")
    magnet = result.get("MagnetUri")
    link = result.get("Link")
    tracker_id = result.get("TrackerId")

    # Resolve download source
    if magnet:
        source = magnet
    elif link:
        try:
            resolved = await torrent_resolve_link(link, tracker_id=tracker_id)
        except Exception as e:
            return f"Error resolving download for #{number}: {e}"
        if isinstance(resolved, str):
            source = resolved
        else:
            tmp_path = SEARCH_DIR / f"{sid}_{idx}.torrent"
            tmp_path.write_bytes(resolved)
            source = str(tmp_path)
    else:
        return f"Error: #{number} has no download link"

    # Add to torrent client
    save_path = config.get_env("TEMP_DOWNLOAD_LOCATION", "TEMPORARY_DOWNLOAD_LOCATION")
    if not save_path:
        save_path = str(Path(tempfile.gettempdir()) / "media-box" / "downloads")
        Path(save_path).mkdir(parents=True, exist_ok=True)

    client = _torrent_client()
    t_hash = await client.add_torrent(
        source, save_path=save_path, category=category, tag=tag,
    )

    # Monitor torrent health — wait up to timeout to see if it's viable
    timeout = min(max(timeout, 30), 300)
    interval = 5
    elapsed = 0
    name = title
    ever_had_seeders = False
    last_status = ""
    prev_progress = -1.0

    while elapsed < timeout:
        torrents = await client.get_torrents()
        torrent = _find_torrent(torrents, t_hash)
        if not torrent:
            if elapsed < 15:
                await asyncio.sleep(5)
                elapsed += 5
                continue
            return f"ERROR: Torrent not found after adding: {name}"

        state = torrent.get("state", "")
        progress = torrent.get("progress", 0)
        dlspeed = torrent.get("dlspeed", 0)
        eta = torrent.get("eta", 0)
        num_seeds = torrent.get("num_seeds", 0)
        name = torrent.get("name", name)

        if num_seeds > 0:
            ever_had_seeders = True

        last_status = (
            f"{format_progress(progress)}  "
            f"{format_size(dlspeed)}/s  "
            f"ETA {_format_eta(eta)}  "
            f"Seeds {num_seeds}"
        )

        # Already finished (small/fast torrent)
        if state in _DONE_STATES and (ever_had_seeders or progress >= 1.0):
            save = torrent.get("save_path", save_path)
            return (
                f"Complete: {name} ({t_hash[:12]})\n{last_status}\n"
                f"Save path: {save}"
            )

        if state in _ERROR_STATES:
            error_detail = torrent.get("error", "")
            error_msg = f"ERROR: {name} ({t_hash[:12]})"
            if error_detail:
                error_msg += f"\nReason: {error_detail}"
            else:
                error_msg += f"\nState: {STATE_MAP.get(state, state)}"
            error_msg += "\nCheck save_path is writable and has enough disk space. Use torrent_logs() for more detail."
            return error_msg

        # Seeders connected and progress advanced between two polls — it's
        # demonstrably healthy, so return now instead of making the caller
        # sit out the rest of the window.
        if num_seeds > 0 and 0 <= prev_progress < progress:
            return (
                f"DOWNLOADING: {name} ({t_hash[:12]})\n{last_status}\n"
                f"Healthy — {num_seeds} seeder(s) connected and progress advancing. "
                f"The download continues in the background. Tell the user it's in "
                f"progress; check later with torrent_info or torrent_list. If they'd "
                f"prefer a different result, use torrent_delete to remove this one first."
            )
        prev_progress = progress

        # No seeders after the stall timeout — dead torrent, remove it
        if elapsed >= _NO_SEEDERS_TIMEOUT and not ever_had_seeders and progress < 1.0:
            await client.delete_torrent(t_hash, delete_files=True)
            return (
                f"DEAD TORRENT (removed): {name} — no seeders after {_NO_SEEDERS_TIMEOUT}s ({t_hash[:12]}). "
                f"Try the next search result, or re-search with a different indexer."
            )

        await asyncio.sleep(interval)
        elapsed += interval

    # Health check window expired — torrent is alive but never proved itself
    if ever_had_seeders:
        return (
            f"DOWNLOADING: {name} ({t_hash[:12]})\n{last_status}\n"
            f"The torrent is healthy and downloading. Tell the user it's in progress "
            f"and they can check status with torrent_info or torrent_list. "
            f"If they'd prefer a different result, use torrent_delete to remove this one first."
        )
    else:
        return (
            f"SLOW START: {name} ({t_hash[:12]})\n{last_status}\n"
            f"No seeders found yet but still searching. Ask the user: wait it out, "
            f"or try the next search result? Use torrent_delete to remove if switching."
        )


@mcp.tool()
async def torrent_list(
    filter: str = "",
    category: str = "",
    state: str = "",
) -> str:
    """List active and completed torrents.

    Args:
        filter: Filter by name substring
        category: Filter by category
        state: Filter by state (Downloading, Completed, Stalled, Paused, Error)
    """
    client = _torrent_client()
    torrents = await client.get_torrents(category=category or None)

    if filter:
        filt = filter.lower()
        torrents = [t for t in torrents if filt in t.get("name", "").lower()]

    if state:
        state_filter = state.lower()
        if state_filter == "completed":
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
        seeds = f"{t.get('num_seeds', 0)}/{t.get('num_seeds_swarm', 0)}"
        peers = f"{t.get('num_peers', 0)}/{t.get('num_peers_swarm', 0)}"
        rows.append({
            "name": t.get("name", ""),
            "size": format_size(t.get("size")),
            "progress": format_progress(t.get("progress", 0)),
            "state": STATE_MAP.get(t.get("state", ""), t.get("state", "")),
            "seeds": seeds,
            "peers": peers,
            "hash": t.get("hash", "")[:12],
        })
    return format_table(rows, [
        ("Name", "name", 0),
        ("Size", "size", 10),
        ("Progress", "progress", 28),
        ("State", "state", 12),
        ("Seeds", "seeds", 7),
        ("Peers", "peers", 7),
        ("Hash", "hash", 12),
    ])


@mcp.tool()
async def torrent_info(query: str) -> str:
    """Get detailed info about a torrent — progress, speed, ETA, save path, files.

    Args:
        query: Torrent hash prefix or name substring
    """
    client = _torrent_client()
    torrents = await client.get_torrents()
    torrent = _find_torrent(torrents, query)
    if not torrent:
        return f"No torrent matching '{query}'"

    h = torrent["hash"]
    files = await client.get_torrent_files(h)

    state = STATE_MAP.get(torrent.get("state", ""), torrent.get("state", ""))
    progress = torrent.get("progress", 0)
    save_path = torrent.get("save_path", "")

    lines = [
        f"Name:       {torrent.get('name')}",
        f"Hash:       {h[:12]}",
        f"State:      {state}",
        f"Progress:   {format_progress(progress)}",
        f"Size:       {format_size(torrent.get('size'))}",
        f"Speed:      {format_size(torrent.get('dlspeed', 0))}/s ↓  {format_size(torrent.get('upspeed', 0))}/s ↑",
        f"ETA:        {_format_eta(torrent.get('eta', 0))}",
        f"Seeds:      {torrent.get('num_seeds', 0)} connected, {torrent.get('num_seeds_swarm', 0)} in swarm",
        f"Peers:      {torrent.get('num_peers', 0)} connected, {torrent.get('num_peers_swarm', 0)} in swarm",
        f"Ratio:      {torrent.get('ratio', 0):.2f}",
        f"Save path:  {save_path}",
    ]

    # Tracker info
    trackers = await client.get_torrent_trackers(h)
    if trackers:
        lines.append(f"\nTrackers ({len(trackers)}):")
        for tr in trackers:
            msg = f"  {tr['url']}  (seeds: {tr['seeds']}, peers: {tr['peers']})"
            if tr.get("message"):
                msg += f"  [{tr['message']}]"
            lines.append(msg)

    if files:
        lines.append(f"\nFiles ({len(files)}):")
        for f in files:
            pct = f.get("progress", 0) * 100
            lines.append(f"  {pct:5.1f}%  {format_size(f.get('size')):>10s}  {f.get('name', '')}")

    return "\n".join(lines)


@mcp.tool()
async def torrent_peers(query: str) -> str:
    """List connected peers for a torrent — shows IP, client, speed, and flags.

    Args:
        query: Torrent hash prefix or name substring
    """
    client = _torrent_client()
    torrents = await client.get_torrents()
    torrent = _find_torrent(torrents, query)
    if not torrent:
        return f"No torrent matching '{query}'"

    h = torrent["hash"]
    peers = await client.get_torrent_peers(h)
    if not peers:
        return f"No connected peers for {torrent.get('name', h[:12])}"

    rows = []
    for p in peers:
        rows.append({
            "ip": p["ip"],
            "client": p.get("client", "")[:20],
            "progress": format_progress(p.get("progress", 0)),
            "down": f"{format_size(p.get('down_speed', 0))}/s",
            "up": f"{format_size(p.get('up_speed', 0))}/s",
            "flags": p.get("flags", ""),
        })
    return format_table(rows, [
        ("IP", "ip", 15),
        ("Client", "client", 20),
        ("Progress", "progress", 28),
        ("Down", "down", 12),
        ("Up", "up", 12),
        ("Flags", "flags", 0),
    ])


@mcp.tool()
async def torrent_delete(query: str, delete_files: bool = False) -> str:
    """Delete a torrent by hash prefix or name.

    Args:
        query: Torrent hash prefix or name substring
        delete_files: Also delete downloaded files from disk
    """
    client = _torrent_client()
    torrents = await client.get_torrents()
    torrent = _find_torrent(torrents, query)
    if not torrent:
        return f"No torrent matching '{query}'"

    h = torrent["hash"]
    name = torrent.get("name", h[:12])
    await client.delete_torrent(h, delete_files=delete_files)
    action = "Deleted with files" if delete_files else "Deleted"
    return f"{action}: {name} ({h[:12]})"


@mcp.tool()
async def torrent_wait(query: str, timeout: int = 1800) -> str:
    """Wait for a torrent to finish downloading.

    IMPORTANT: This tool blocks until the download completes (can take minutes
    to hours). ALWAYS call this from a subagent/background task, never in the
    main conversation thread — it will freeze the chat.

    Args:
        query: Torrent hash prefix or name substring
        timeout: Max seconds to wait (default 1800)
    """
    client = _torrent_client()
    timeout = min(max(timeout, 60), 1800)
    interval = 10

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
            return f"ERROR: Torrent disappeared: {name}"

        state = torrent.get("state", "")
        progress = torrent.get("progress", 0)
        dlspeed = torrent.get("dlspeed", 0)
        eta = torrent.get("eta", 0)
        num_seeds = torrent.get("num_seeds", 0)

        if num_seeds > 0:
            ever_had_seeders = True

        last_status = (
            f"{format_progress(progress)}  "
            f"{format_size(dlspeed)}/s  "
            f"ETA {_format_eta(eta)}  "
            f"Seeds {num_seeds}"
        )

        if state in _DONE_STATES and (ever_had_seeders or progress >= 1.0):
            save = torrent.get("save_path", "")
            return f"Complete: {name} ({t_hash[:12]})\n{last_status}\nSave path: {save}"

        if state in _ERROR_STATES:
            error_detail = torrent.get("error", "")
            error_msg = f"ERROR: {name} ({t_hash[:12]})"
            if error_detail:
                error_msg += f"\nReason: {error_detail}"
            else:
                error_msg += f"\nState: {STATE_MAP.get(state, state)}"
            error_msg += "\nCheck save_path is writable and has enough disk space. Use torrent_logs() for more detail."
            return error_msg

        if elapsed >= _NO_SEEDERS_TIMEOUT and not ever_had_seeders and progress < 1.0:
            await client.delete_torrent(t_hash, delete_files=True)
            return (
                f"DEAD TORRENT (removed): {name} — no seeders after {_NO_SEEDERS_TIMEOUT}s ({t_hash[:12]}). "
                f"Try the next search result, or re-search with a different indexer."
            )

        await asyncio.sleep(interval)
        elapsed += interval

    return f"TIMEOUT after {timeout}s: {name} ({t_hash[:12]})\n{last_status}"


@mcp.tool()
async def torrent_logs(limit: int = 100) -> str:
    """Show recent libtorrent engine logs for diagnosing torrent issues
    (connectivity, port mapping, peer errors, etc.). Only use when the user
    asks to troubleshoot torrent problems.

    Args:
        limit: Number of recent log entries to return (default 100)
    """
    client = _torrent_client()
    client._process_alerts(timeout=0.5)
    entries = client.get_logs(limit=limit)
    if not entries:
        return "No log entries yet."
    return "\n".join(entries)


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
async def tvmaze_episodes(show_id: int, season: int = 0) -> str:
    """List episodes for a TV show from TVMaze.

    Args:
        show_id: TVMaze show ID
        season: Filter to a specific season number. 0 means all seasons.
    """
    async with TVMazeClient() as client:
        if season:
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
async def tvmaze_lookup(imdb: str = "", tvdb: str = "") -> str:
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


def _normalize_unicode(s: str) -> str:
    """Normalize Unicode for filename matching.

    MCP JSON transport can mangle curly quotes (U+2018/2019) to straight
    quotes (U+0027), causing FileNotFoundError. This normalizes both sides
    so matching works regardless.
    """
    import unicodedata
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\u2018", "'").replace("\u2019", "'")  # curly single quotes
    s = s.replace("\u201C", '"').replace("\u201D", '"')  # curly double quotes
    s = s.replace("\u2013", "-").replace("\u2014", "-")  # en/em dash
    return s


def _resolve_source(source: Path) -> Path:
    if source.is_absolute():
        resolved = source
    else:
        (temp_dir,) = config.require_env("TEMP_DOWNLOAD_LOCATION")
        resolved = Path(temp_dir) / source

    # If the exact path exists, use it
    if resolved.is_file():
        return resolved

    # Fuzzy match: the MCP JSON transport may have mangled Unicode characters
    # in the filename. List the parent directory and match after normalization.
    parent = resolved.parent
    if parent.is_dir():
        target = _normalize_unicode(resolved.name)
        for entry in parent.iterdir():
            if _normalize_unicode(entry.name) == target:
                return entry

    return resolved  # return as-is, _validate_source will report the error


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
async def mover_list(path: str = "") -> str:
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


async def _copy_file(source: Path, dest: Path, *, force: bool = False) -> str:
    if dest.exists() and not force:
        return f"Error: destination already exists: {dest}\nUse force=true to overwrite."
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _do_copy():
        try:
            shutil.copy2(str(source), str(dest))
        except BaseException:
            if dest.exists():
                dest.unlink()
            raise

    await asyncio.get_event_loop().run_in_executor(None, _do_copy)
    return f"Copied: {source}\n    -> {dest}"


async def _cleanup_torrent(torrent_hash: str) -> str:
    client = _torrent_client()
    torrents = await client.get_torrents()
    match = _find_torrent(torrents, torrent_hash)
    if not match:
        return f"No torrent matching: {torrent_hash}"
    full_hash = match["hash"]
    await client.delete_torrent(full_hash, delete_files=True)
    return f"Deleted torrent {full_hash[:12]} and remaining files"


@mcp.tool()
async def mover_movie(
    source: str,
    dest_name: str,
    force: bool = False,
    torrent_hash: str = "",
) -> str:
    """Move a movie file to the Jellyfin movies library with proper naming.

    WARNING: This copies large files and can take minutes. Run in a subagent, not the main thread.

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

    result = await _copy_file(source_path, dest, force=force)

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
    torrent_hash: str = "",
) -> str:
    """Move a TV episode file to the Jellyfin TV library with proper naming.

    WARNING: This copies large files and can take minutes. Run in a subagent, not the main thread.

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

    result = await _copy_file(source_path, dest, force=force)

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
    torrent_hash: str = "",
) -> str:
    """Move multiple TV episode files to the Jellyfin TV library in one operation.

    WARNING: This copies large files and can take minutes. Run in a subagent, not the main thread.
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
            result = await _copy_file(source_path, dest, force=force)
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

_TRANSPORT_ALIASES = {
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "sse": "sse",
    "stdio": "stdio",
}


def main():
    parser = argparse.ArgumentParser(
        description="media-box MCP server",
    )
    parser.add_argument(
        "--transport",
        choices=sorted(_TRANSPORT_ALIASES),
        default=config.get_env("MCP_TRANSPORT") or "http",
        help="MCP transport: http (streamable HTTP, default), sse, or stdio",
    )
    parser.add_argument(
        "--host",
        default=config.get_env("MCP_HOST") or "127.0.0.1",
        help="bind address for http/sse transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(config.get_env("MCP_PORT") or 8765),
        help="port for http/sse transports (default: 8765)",
    )
    args = parser.parse_args()

    # The default can come from the config file, which argparse doesn't validate
    transport = _TRANSPORT_ALIASES.get(args.transport)
    if transport is None:
        parser.error(
            f"invalid transport '{args.transport}' "
            f"(choose from {', '.join(sorted(_TRANSPORT_ALIASES))})"
        )

    if transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        if transport == "streamable-http":
            # Stateless by default: with server-side sessions, a server restart
            # invalidates every connected client's mcp-session-id and their next
            # request 404s until they re-initialize. Stateless requests are
            # self-contained, so agents keep working across restarts. BUT
            # server-push events (subscribe_events) need sessions that outlive
            # a request — set MCP_STATELESS=false to enable them; clients then
            # must re-initialize (and re-subscribe) after a server restart.
            mcp.settings.stateless_http = (config.MCP_STATELESS or "true").lower() in ("true", "1", "yes", "on")
        path = mcp.settings.streamable_http_path if transport == "streamable-http" else mcp.settings.sse_path
        print(f"media-box MCP server listening on http://{args.host}:{args.port}{path} ({transport})")

    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        # uvicorn has already shut down gracefully by the time asyncio
        # re-raises the Ctrl+C — don't dump a traceback over it
        pass
    finally:
        shutdown_torrent_client()
        print("media-box MCP server stopped, torrent state saved")


if __name__ == "__main__":
    main()
