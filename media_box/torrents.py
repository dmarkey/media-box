"""Torrent search via pyackett (native Python, no server needed)."""

import tempfile
from pathlib import Path
from typing import Any, Optional

from . import config

CATEGORY_MAP = {"movies": 2000, "tv": 5000}
SEARCH_DIR = Path(tempfile.gettempdir()) / "media-box" / "searches"

# ---------------------------------------------------------------------------
# Singleton pyackett instance
# ---------------------------------------------------------------------------

_pyackett_instance = None


async def _get_pyackett():
    """Get or create the singleton Pyackett instance."""
    global _pyackett_instance
    if _pyackett_instance is not None:
        return _pyackett_instance

    indexers_str = config.TORRENT_INDEXERS
    if not indexers_str:
        raise RuntimeError(
            "No torrent indexers configured. Set TORRENT_INDEXERS in config "
            "(e.g. TORRENT_INDEXERS=1337x,therarbg,thepiratebay)"
        )

    indexers = [s.strip() for s in indexers_str.split(",") if s.strip()]

    from pyackett import Pyackett

    config_dir = Path.home() / ".config" / "media-box" / "pyackett"
    pk = Pyackett(proxy=config.TORRENT_SEARCH_PROXY, config_dir=str(config_dir))
    pk.load_definitions_from_github("jackett")

    for idx_id in indexers:
        await pk.configure_indexer(idx_id, {})

    _pyackett_instance = pk
    return pk


async def search(
    query: str,
    category: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Search for torrents via pyackett."""
    from pyackett.core.models import TorznabQuery

    pk = await _get_pyackett()

    tq = TorznabQuery(
        search_term=query,
        categories=[category] if category else [],
        limit=limit or 100,
    )

    results = await pk.manager.search(tq)

    return [
        {
            "Title": r.title,
            "Guid": r.guid,
            "Link": r.link,
            "Details": r.details,
            "PublishDate": r.publish_date.isoformat() if r.publish_date else None,
            "Category": r.category,
            "Size": r.size,
            "Seeders": r.seeders,
            "Peers": r.peers,
            "MagnetUri": r.magnet_uri,
            "InfoHash": r.info_hash,
            "Tracker": r.origin_name,
            "TrackerId": r.origin_id,
        }
        for r in results
        if (r.seeders or 0) > 0
    ]


async def resolve_link(link: str, tracker_id: str | None = None) -> str | bytes:
    """Resolve a download link to a magnet URI or .torrent bytes.

    For sites like 1337x where the search result link points to a details
    page, this uses the indexer's download selectors to find the actual
    .torrent or magnet on that page.
    """
    pk = await _get_pyackett()

    # If we have a tracker ID, try the definition's download selectors first
    if tracker_id:
        resolved = await pk.resolve_download(tracker_id, link)
        if resolved:
            if resolved.startswith("magnet:"):
                return resolved
            # It's a .torrent URL — fetch it
            resp = await pk._client.get(resolved)
            if resp.text.strip().startswith("magnet:"):
                return resp.text.strip()
            return resp.content

    # Direct fetch fallback
    resp = await pk._client.get(link)
    content_type = resp.headers.get("content-type", "")
    if "magnet" in content_type or resp.text.strip().startswith("magnet:"):
        return resp.text.strip()
    # Check if it's valid torrent data (starts with 'd')
    if resp.content and resp.content[:1] == b"d":
        return resp.content

    # Last resort: the response might be an HTML details page
    # Try to find a magnet link in it
    import re
    magnet_match = re.search(r'magnet:\?xt=urn:btih:[^"\'&\s]+', resp.text)
    if magnet_match:
        return magnet_match.group(0)

    raise RuntimeError(f"Could not resolve download link: {link[:80]}")
