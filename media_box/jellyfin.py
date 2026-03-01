from typing import Any, Optional

import aiohttp


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class JellyfinClient:
    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"X-MediaBrowser-Token": self.api_key}
        )
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    async def search_items(
        self, query: str, item_types: str = "Movie,Series,Episode"
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/Items"
        params = {
            "searchTerm": query,
            "recursive": "true",
            "includeItemTypes": item_types,
            "fields": "Name,ProductionYear,Type,Overview,PremiereDate,ProviderIds",
        }
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("Items", [])

    async def get_libraries(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/Library/MediaFolders"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("Items", [])

    async def get_episodes(
        self, series_id: str, season: Optional[int] = None
    ) -> list[dict[str, Any]]:
        # Need a user id for the episodes endpoint
        url = f"{self.base_url}/Users"
        async with self.session.get(url) as r:
            r.raise_for_status()
            users = await r.json()
        if not users:
            return []
        user_id = users[0]["Id"]

        url = f"{self.base_url}/Users/{user_id}/Items"
        params = {
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Fields": "Name,IndexNumber,ParentIndexNumber,SeasonNumber,Type",
        }
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()
            items = data.get("Items", [])

        if season is not None:
            items = [
                ep
                for ep in items
                if (ep.get("ParentIndexNumber") or ep.get("SeasonNumber")) == season
            ]
        return items

    async def refresh_library(self, library_id: str) -> None:
        url = f"{self.base_url}/Items/{library_id}/Refresh"
        params = {
            "metadataRefreshMode": "FullRefresh",
            "imageRefreshMode": "FullRefresh",
            "replaceAllImages": "false",
            "refreshItem": "true",
        }
        async with self.session.post(url, params=params) as r:
            r.raise_for_status()

    async def refresh_all_libraries(self) -> None:
        libraries = await self.get_libraries()
        for lib in libraries:
            lid = lib.get("Id")
            if lid:
                await self.refresh_library(lid)


