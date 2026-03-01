import tempfile
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp.client_exceptions import NonHttpUrlRedirectClientError


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

CATEGORY_MAP = {"movies": 2000, "tv": 5000}
SEARCH_DIR = Path(tempfile.gettempdir()) / "media-box" / "searches"


class JackettClient:
    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    async def search(
        self,
        query: str,
        category: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v2.0/indexers/all/results"
        params: dict[str, str] = {"Query": query, "apikey": self.api_key}
        if category is not None:
            params["Category[]"] = str(category)
        if limit is not None:
            params["limit"] = str(limit)
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()
            results = data.get("Results", [])
            # Filter out results with zero seeders (dead torrents)
            return [r for r in results if r.get("Seeders", 0) > 0]

    async def resolve_link(self, link: str) -> str | bytes:
        """Resolve a Jackett download link. Returns a magnet URI string or .torrent bytes."""
        try:
            async with self.session.get(link) as r:
                r.raise_for_status()
                return await r.read()
        except NonHttpUrlRedirectClientError as e:
            location = getattr(e, "message", str(e))
            if location and location.startswith("magnet:"):
                return location
            raise


