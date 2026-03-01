import os
from typing import Any, Optional

import aiohttp


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class QBittorrentClient:
    def __init__(self, url: str, username: str, password: str):
        self.base_url = url.rstrip("/")
        self.username = username
        self.password = password
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        await self._login()
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    async def _login(self):
        url = f"{self.base_url}/api/v2/auth/login"
        data = {"username": self.username, "password": self.password}
        headers = {"Referer": self.base_url}
        async with self.session.post(url, data=data, headers=headers) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"qBittorrent login failed ({r.status}): {text}")

    async def get_torrents(
        self,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v2/torrents/info"
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def get_torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v2/torrents/files"
        async with self.session.get(url, params={"hash": torrent_hash}) as r:
            r.raise_for_status()
            return await r.json()

    async def add_torrent(
        self,
        magnet_or_path: str,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> None:
        url = f"{self.base_url}/api/v2/torrents/add"
        headers = {"Referer": self.base_url}

        if magnet_or_path.startswith("magnet:"):
            data = aiohttp.FormData()
            data.add_field("urls", magnet_or_path)
            if save_path:
                data.add_field("savepath", save_path)
            if category:
                data.add_field("category", category)
            if tag:
                data.add_field("tags", tag)
            async with self.session.post(url, data=data, headers=headers) as r:
                r.raise_for_status()
                text = await r.text()
                if "fail" in text.lower():
                    raise RuntimeError(f"Failed to add torrent: {text}")
        else:
            with open(magnet_or_path, "rb") as f:
                torrent_bytes = f.read()
            data = aiohttp.FormData()
            data.add_field(
                "torrents", torrent_bytes, filename=os.path.basename(magnet_or_path)
            )
            if save_path:
                data.add_field("savepath", save_path)
            if category:
                data.add_field("category", category)
            if tag:
                data.add_field("tags", tag)
            async with self.session.post(url, data=data, headers=headers) as r:
                r.raise_for_status()
                text = await r.text()
                if "fail" in text.lower():
                    raise RuntimeError(f"Failed to add torrent: {text}")

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> None:
        url = f"{self.base_url}/api/v2/torrents/delete"
        data = {
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false",
        }
        headers = {"Referer": self.base_url}
        async with self.session.post(url, data=data, headers=headers) as r:
            r.raise_for_status()

    async def delete_torrents(self, torrent_hashes: list[str], delete_files: bool = False) -> None:
        url = f"{self.base_url}/api/v2/torrents/delete"
        data = {
            "hashes": "|".join(torrent_hashes),
            "deleteFiles": "true" if delete_files else "false",
        }
        headers = {"Referer": self.base_url}
        async with self.session.post(url, data=data, headers=headers) as r:
            r.raise_for_status()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATE_MAP = {
    "uploading": "Seeding",
    "stalledUP": "Seeding",
    "downloading": "Downloading",
    "stalledDL": "Stalled",
    "pausedDL": "Paused",
    "pausedUP": "Completed",
    "queuedDL": "Queued",
    "queuedUP": "Queued",
    "checkingDL": "Checking",
    "checkingUP": "Checking",
    "error": "Error",
    "missingFiles": "Missing",
    "moving": "Moving",
}


