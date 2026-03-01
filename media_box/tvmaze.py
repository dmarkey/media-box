import asyncio
from typing import Any, Optional

import aiohttp


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TVMazeClient:
    BASE_URL = "https://api.tvmaze.com"

    def __init__(self, rate_limit_delay: float = 0.5):
        self.session: Optional[aiohttp.ClientSession] = None
        self._delay = rate_limit_delay
        self._last_req: float = 0

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        # Simple rate limiting
        now = asyncio.get_event_loop().time()
        wait = self._delay - (now - self._last_req)
        if wait > 0:
            await asyncio.sleep(wait)

        url = f"{self.BASE_URL}{endpoint}"
        async with self.session.get(url, params=params) as r:
            self._last_req = asyncio.get_event_loop().time()
            if r.status == 404:
                return None
            if r.status == 429:
                await asyncio.sleep(2)
                return await self._get(endpoint, params)
            r.raise_for_status()
            return await r.json()

    async def search_shows(self, query: str) -> list[dict[str, Any]]:
        result = await self._get("/search/shows", {"q": query})
        return result or []

    async def get_show(self, show_id: int) -> Optional[dict[str, Any]]:
        return await self._get(f"/shows/{show_id}")

    async def get_seasons(self, show_id: int) -> list[dict[str, Any]]:
        result = await self._get(f"/shows/{show_id}/seasons")
        return result or []

    async def get_episodes(self, show_id: int, specials: bool = False) -> list[dict[str, Any]]:
        params = {}
        if specials:
            params["specials"] = "1"
        result = await self._get(f"/shows/{show_id}/episodes", params)
        return result or []

    async def get_season_episodes(self, season_id: int) -> list[dict[str, Any]]:
        result = await self._get(f"/seasons/{season_id}/episodes")
        return result or []

    async def lookup_show(self, **kwargs) -> Optional[dict[str, Any]]:
        """Lookup by external id. Pass imdb='tt...' or thetvdb='12345'."""
        return await self._get("/lookup/shows", kwargs)


