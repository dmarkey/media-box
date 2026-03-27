"""Embedded BitTorrent client using libtorrent.

Replaces the need for a running qBittorrent instance. Provides the same
interface (get_torrents, add_torrent, delete_torrent, etc.) but runs
in-process. Session state is persisted to disk so downloads resume
across MCP server restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import libtorrent as lt

logger = logging.getLogger("media_box.torrent_client")


def _bool(value: str | None, default: bool) -> bool:
    """Parse a config string as boolean."""
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")

# ---------------------------------------------------------------------------
# State mapping (compatible with qBittorrent state names)
# ---------------------------------------------------------------------------

STATE_MAP = {
    "uploading": "Seeding",
    "seeding": "Seeding",
    "downloading": "Downloading",
    "stalled": "Stalled",
    "paused": "Paused",
    "completed": "Completed",
    "queued": "Queued",
    "checking": "Checking",
    "error": "Error",
    "missing": "Missing",
    "moving": "Moving",
    "allocating": "Allocating",
    "metadata": "Fetching metadata",
}


def _lt_state_to_str(state: int) -> str:
    """Convert libtorrent torrent_status.state enum to a string."""
    mapping = {
        lt.torrent_status.states.checking_files: "checking",
        lt.torrent_status.states.downloading_metadata: "metadata",
        lt.torrent_status.states.downloading: "downloading",
        lt.torrent_status.states.finished: "completed",
        lt.torrent_status.states.seeding: "seeding",
        lt.torrent_status.states.checking_resume_data: "checking",
    }
    # allocating was removed in newer libtorrent
    if hasattr(lt.torrent_status.states, "allocating"):
        mapping[lt.torrent_status.states.allocating] = "allocating"
    return mapping.get(state, "unknown")


class TorrentClient:
    """Embedded BitTorrent client with persistent state.

    State is saved to `state_dir/` so torrents resume after restart.
    Provides the same interface as the old QBittorrentClient.
    """

    def __init__(
        self,
        state_dir: str | Path | None = None,
        default_save_path: str | Path | None = None,
    ):
        from . import config

        self._state_dir = Path(state_dir or Path.home() / ".config" / "media-box" / "torrents")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._resume_dir = self._state_dir / "resume"
        self._resume_dir.mkdir(exist_ok=True)
        self._meta_path = self._state_dir / "metadata.json"
        self._default_save_path = str(default_save_path or self._state_dir / "downloads")
        Path(self._default_save_path).mkdir(parents=True, exist_ok=True)

        # Torrent metadata (tags, categories) — libtorrent doesn't track these
        self._meta: dict[str, dict[str, Any]] = self._load_meta()

        # Seeding policy — seed to 1.0 ratio or 60 min, whichever comes first
        self._seed_ratio = float(config.TORRENT_SEED_RATIO or 1.0)
        self._seed_time = int(config.TORRENT_SEED_TIME or 60) * 60  # config is minutes, store as seconds

        # Create libtorrent session with sane defaults for a media box:
        # - Moderate connection limits (not a seedbox)
        # - Upload capped at 1 MB/s to not saturate upstream
        # - Encryption forced for privacy
        # - Seed to 1.0 ratio then stop (be a good citizen, but don't seed forever)
        listen_port = int(config.TORRENT_PORT or 6881)
        settings = {
            "listen_interfaces": f"0.0.0.0:{listen_port}",
            "enable_dht": _bool(config.TORRENT_ENABLE_DHT, True),
            "enable_lsd": _bool(config.TORRENT_ENABLE_LSD, True),
            "enable_natpmp": _bool(config.TORRENT_ENABLE_NATPMP, True),
            "enable_upnp": _bool(config.TORRENT_ENABLE_UPNP, True),
            "enable_incoming_utp": _bool(config.TORRENT_ENABLE_UTP, True),
            "enable_outgoing_utp": _bool(config.TORRENT_ENABLE_UTP, True),
            "connections_limit": int(config.TORRENT_MAX_CONNECTIONS or 200),
            "unchoke_slots_limit": int(config.TORRENT_MAX_UPLOADS or 4),
            "download_rate_limit": int(config.TORRENT_DOWNLOAD_RATE_LIMIT or 0),
            "upload_rate_limit": int(config.TORRENT_UPLOAD_RATE_LIMIT or 1024 * 1024),  # 1 MB/s
            "anonymous_mode": _bool(config.TORRENT_ANONYMOUS_MODE, False),
            "alert_mask": lt.alert.category_t.status_notification | lt.alert.category_t.error_notification,
        }

        # Encryption forced by default for privacy
        enc = (config.TORRENT_ENCRYPTION or "forced").lower()
        enc_val = {"forced": 0, "enabled": 1, "disabled": 2}.get(enc, 1)
        settings["in_enc_policy"] = enc_val
        settings["out_enc_policy"] = enc_val

        # Peer proxy (separate from search proxy)
        proxy_url = config.TORRENT_PROXY_URL
        if proxy_url:
            from urllib.parse import urlparse
            p = urlparse(proxy_url)
            settings["proxy_hostname"] = p.hostname or ""
            settings["proxy_port"] = p.port or 1080
            settings["proxy_type"] = lt.proxy_type_t.socks5_pw if p.username else lt.proxy_type_t.socks5
            if p.username:
                settings["proxy_username"] = p.username
            if p.password:
                settings["proxy_password"] = p.password
            settings["proxy_peer_connections"] = True

        self._session = lt.session(settings)

        # Per-torrent defaults
        self._max_connections_per_torrent = int(config.TORRENT_MAX_CONNECTIONS_PER_TORRENT or 50)
        self._max_uploads_per_torrent = int(config.TORRENT_MAX_UPLOADS_PER_TORRENT or -1)

        self._handles: dict[str, lt.torrent_handle] = {}

        # Restore previous torrents
        self._restore_torrents()
        logger.info(
            f"Torrent client started (port {listen_port}, "
            f"{len(self._handles)} resumed torrents)"
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text())
            except Exception:
                pass
        return {}

    def _save_meta(self):
        self._meta_path.write_text(json.dumps(self._meta, indent=2))

    def _save_resume_data(self, handle: lt.torrent_handle):
        """Save resume data for a single torrent."""
        try:
            if not handle.is_valid():
                return
            status = handle.status()
            if not status.has_metadata:
                return
            handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
        except Exception:
            pass

    def _save_all_resume_data(self):
        """Save resume data for all torrents."""
        outstanding = 0
        for h in self._handles.values():
            try:
                if h.is_valid() and h.status().has_metadata:
                    h.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
                    outstanding += 1
            except Exception:
                pass
        # Wait for all save_resume_data alerts to come back
        while outstanding > 0:
            self._session.wait_for_alert(5000)
            alerts = self._session.pop_alerts()
            for alert in alerts:
                if isinstance(alert, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)):
                    outstanding -= 1
                    if isinstance(alert, lt.save_resume_data_alert):
                        h = alert.handle
                        info_hash = str(h.info_hash())
                        resume_path = self._resume_dir / f"{info_hash}.fastresume"
                        resume_data = lt.write_resume_data_buf(alert.params)
                        resume_path.write_bytes(resume_data)

    def _process_alerts(self, timeout: float = 0.1):
        """Process libtorrent alerts (saves resume data to disk)."""
        self._session.wait_for_alert(int(timeout * 1000))
        alerts = self._session.pop_alerts()
        for alert in alerts:
            if isinstance(alert, lt.save_resume_data_alert):
                h = alert.handle
                info_hash = str(h.info_hash())
                resume_path = self._resume_dir / f"{info_hash}.fastresume"
                resume_data = lt.write_resume_data_buf(alert.params)
                resume_path.write_bytes(resume_data)
            elif isinstance(alert, lt.save_resume_data_failed_alert):
                pass  # torrent was removed or has no metadata yet

    def _restore_torrents(self):
        """Restore torrents from saved resume data or magnet URIs."""
        restored_hashes = set()

        # First, restore from fastresume files (has full state)
        for resume_file in self._resume_dir.glob("*.fastresume"):
            try:
                resume_data = resume_file.read_bytes()
                info_hash = resume_file.stem
                meta = self._meta.get(info_hash, {})
                save_path = meta.get("save_path", self._default_save_path)

                params = lt.read_resume_data(resume_data)
                params.save_path = save_path

                handle = self._session.add_torrent(params)
                self._handles[str(handle.info_hash())] = handle
                restored_hashes.add(info_hash)
            except Exception as e:
                logger.warning(f"Failed to restore torrent {resume_file.name}: {e}")

        # Then, restore any magnets that don't have resume data yet
        for info_hash, meta in self._meta.items():
            if info_hash in restored_hashes:
                continue
            source = meta.get("source", "")
            if source.startswith("magnet:"):
                try:
                    save_path = meta.get("save_path", self._default_save_path)
                    params = lt.parse_magnet_uri(source)
                    params.save_path = save_path
                    handle = self._session.add_torrent(params)
                    self._handles[str(handle.info_hash())] = handle
                    restored_hashes.add(info_hash)
                except Exception as e:
                    logger.warning(f"Failed to restore magnet {info_hash[:12]}: {e}")

    def save_state(self):
        """Persist all state to disk. Call before shutdown."""
        self._save_all_resume_data()
        self._save_meta()

    # ------------------------------------------------------------------
    # Public API (compatible with QBittorrentClient interface)
    # ------------------------------------------------------------------

    async def add_torrent(
        self,
        magnet_or_path: str,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> str:
        """Add a torrent from a magnet link or .torrent file path.

        Returns the info_hash of the added torrent.
        """
        save = save_path or self._default_save_path
        Path(save).mkdir(parents=True, exist_ok=True)

        if magnet_or_path.startswith("magnet:"):
            params = lt.parse_magnet_uri(magnet_or_path)
            params.save_path = save
            handle = self._session.add_torrent(params)
        elif magnet_or_path.startswith("http://") or magnet_or_path.startswith("https://"):
            # Download .torrent file from URL
            torrent_bytes = await self._fetch_torrent(magnet_or_path)
            if isinstance(torrent_bytes, str) and torrent_bytes.startswith("magnet:"):
                # URL redirected to a magnet link
                params = lt.parse_magnet_uri(torrent_bytes)
                params.save_path = save
                handle = self._session.add_torrent(params)
            else:
                ti = lt.torrent_info(lt.bdecode(torrent_bytes))
                params = lt.add_torrent_params()
                params.ti = ti
                params.save_path = save
                handle = self._session.add_torrent(params)
        else:
            ti = lt.torrent_info(magnet_or_path)
            params = lt.add_torrent_params()
            params.ti = ti
            params.save_path = save
            handle = self._session.add_torrent(params)

        # Apply per-torrent limits
        handle.set_max_connections(self._max_connections_per_torrent)
        handle.set_max_uploads(self._max_uploads_per_torrent)

        # Wait briefly for info_hash to be available
        await asyncio.sleep(0.5)
        info_hash = str(handle.info_hash())
        self._handles[info_hash] = handle

        # Store metadata (including source for resume without fastresume data)
        self._meta[info_hash] = {
            "category": category or "",
            "tags": tag or "",
            "save_path": save,
            "added_on": int(time.time()),
            "source": magnet_or_path if magnet_or_path.startswith("magnet:") else "",
        }
        self._save_meta()
        self._save_resume_data(handle)
        self._process_alerts(timeout=1)

        logger.info(f"Added torrent: {handle.name()} ({info_hash[:12]})")
        return info_hash

    async def get_torrents(
        self,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List torrents, optionally filtered by category or tag."""
        self._process_alerts()
        results = []
        for info_hash, handle in list(self._handles.items()):
            if not handle.is_valid():
                continue
            meta = self._meta.get(info_hash, {})

            if category and meta.get("category", "") != category:
                continue
            if tag and tag not in meta.get("tags", ""):
                continue

            status = handle.status()
            state_str = _lt_state_to_str(status.state)

            # Enforce seeding policy — auto-pause when limits are met
            if status.is_seeding and not status.paused:
                ratio = status.total_upload / max(status.total_wanted_done, 1)
                seed_time = status.seeding_duration if hasattr(status, "seeding_duration") else 0
                should_stop = False
                if self._seed_ratio > 0 and ratio >= self._seed_ratio:
                    should_stop = True
                if self._seed_time > 0 and seed_time >= self._seed_time:
                    should_stop = True
                if should_stop:
                    handle.pause()
                    meta["completed_on"] = meta.get("completed_on") or int(time.time())
                    self._save_meta()

            # Match qBittorrent's paused states
            if status.paused and not status.auto_managed:
                if status.is_finished:
                    state_str = "completed"
                else:
                    state_str = "paused"
            elif status.is_seeding:
                state_str = "seeding"

            # Friendly state for qbt compat
            if state_str == "downloading" and status.num_seeds == 0:
                friendly_state = "stalledDL"
            elif state_str == "seeding":
                friendly_state = "uploading"
            elif state_str == "completed":
                friendly_state = "pausedUP"
            elif state_str == "paused":
                friendly_state = "pausedDL"
            elif state_str == "downloading":
                friendly_state = "downloading"
            elif state_str == "checking":
                friendly_state = "checkingDL"
            elif state_str == "metadata":
                friendly_state = "stalledDL"
            else:
                friendly_state = state_str

            eta = 0
            if status.download_rate > 0 and status.total_wanted > 0:
                remaining = status.total_wanted - status.total_wanted_done
                eta = int(remaining / status.download_rate)

            results.append({
                "hash": info_hash,
                "name": handle.name() or info_hash[:12],
                "state": friendly_state,
                "progress": status.progress,
                "dlspeed": status.download_rate,
                "upspeed": status.upload_rate,
                "eta": eta,
                "num_seeds": status.num_seeds,
                "num_peers": status.num_peers,
                "size": status.total_wanted,
                "downloaded": status.total_wanted_done,
                "uploaded": status.total_upload,
                "ratio": status.ratio if hasattr(status, "ratio") else (
                    status.total_upload / max(status.total_wanted_done, 1)
                ),
                "save_path": status.save_path,
                "category": meta.get("category", ""),
                "tags": meta.get("tags", ""),
                "added_on": meta.get("added_on", 0),
                "completion_on": meta.get("completed_on", 0),
            })

        return results

    async def get_torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        """List files in a torrent."""
        handle = self._handles.get(torrent_hash)
        if not handle or not handle.is_valid():
            return []

        status = handle.status()
        if not status.has_metadata:
            return []

        ti = handle.torrent_file()
        files = ti.files()
        file_progress = handle.file_progress()

        result = []
        for i in range(files.num_files()):
            size = files.file_size(i)
            progress = file_progress[i] / max(size, 1) if size > 0 else 1.0
            result.append({
                "name": files.file_path(i),
                "size": size,
                "progress": progress,
                "priority": handle.file_priority(i),
                "index": i,
            })
        return result

    async def delete_torrent(
        self, torrent_hash: str, delete_files: bool = False,
    ) -> None:
        """Remove a torrent."""
        handle = self._handles.pop(torrent_hash, None)
        if handle and handle.is_valid():
            if delete_files:
                self._session.remove_torrent(handle, lt.options_t.delete_files)
            else:
                self._session.remove_torrent(handle)

        # Clean up state
        resume_path = self._resume_dir / f"{torrent_hash}.fastresume"
        resume_path.unlink(missing_ok=True)
        self._meta.pop(torrent_hash, None)
        self._save_meta()
        logger.info(f"Deleted torrent {torrent_hash[:12]} (files={'yes' if delete_files else 'no'})")

    async def delete_torrents(
        self, torrent_hashes: list[str], delete_files: bool = False,
    ) -> None:
        """Remove multiple torrents."""
        for h in torrent_hashes:
            await self.delete_torrent(h, delete_files=delete_files)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _fetch_torrent(self, url: str) -> bytes | str:
        """Fetch a .torrent file from a URL. Returns bytes or a magnet URI string.

        Uses pyackett's HTTP client if available (has proxy + CF bypass),
        falls back to aiohttp for direct connections.
        """
        # Try pyackett's client first (has proxy, CF cookies, TLS fingerprinting)
        try:
            from media_box.torrents import _get_pyackett
            pk = await _get_pyackett()
            if pk and pk._client:
                resp = await pk._client.get(url)
                # Check for magnet redirect
                text = resp.text
                if text.strip().startswith("magnet:"):
                    return text.strip()
                return resp.content
        except Exception:
            pass

        # Fallback to direct aiohttp
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                if str(resp.url).startswith("magnet:"):
                    return str(resp.url)
                data = await resp.read()
                if len(data) < 1000:
                    text = data.decode("utf-8", errors="replace")
                    if text.strip().startswith("magnet:"):
                        return text.strip()
                return data

    # ------------------------------------------------------------------
    # Context manager (for compat with existing server.py patterns)
    # ------------------------------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.save_state()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: TorrentClient | None = None


def get_client(
    state_dir: str | Path | None = None,
    default_save_path: str | Path | None = None,
) -> TorrentClient:
    """Get or create the singleton TorrentClient."""
    global _instance
    if _instance is None:
        _instance = TorrentClient(
            state_dir=state_dir,
            default_save_path=default_save_path,
        )
    return _instance
