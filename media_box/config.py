import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config file support
# ---------------------------------------------------------------------------

_file_config: dict[str, str] = {}
_config_file_path: Optional[str] = None

_CONFIG_SEARCH_PATHS = [
    Path.home() / ".config" / "media-box" / "config",
    Path("/etc/media-box/config"),
]


def _parse_config_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE config file. Blank lines and #-comments are ignored."""
    result: dict[str, str] = {}
    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(
                    f"Warning: {path}:{lineno}: skipping malformed line (no '=')",
                    file=sys.stderr,
                )
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip optional surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


def load_config(path: Optional[str] = None) -> None:
    """Load configuration from a file.

    If *path* is given, load that file (error if missing).  Otherwise search
    the well-known locations and load the first one found.
    """
    global _file_config, _config_file_path

    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"Config file not found: {p}")
        _file_config = _parse_config_file(p)
        _config_file_path = str(p)
        return

    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.is_file():
            _file_config = _parse_config_file(candidate)
            _config_file_path = str(candidate)
            return


def config_file_path() -> Optional[str]:
    """Return the path of the loaded config file, or None."""
    return _config_file_path


# Auto-discover on import
load_config()

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def get_env(name: str, *fallbacks: str) -> Optional[str]:
    """Look up a config value. Checks the config file first, then env vars."""
    for key in (name, *fallbacks):
        value = _file_config.get(key) or os.getenv(key)
        if value:
            return value
    return None


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def require_env(*names: str) -> list[str]:
    """Return values for config keys, raise ConfigError if any are missing."""
    values = []
    missing = []
    for name in names:
        val = _file_config.get(name) or os.getenv(name)
        if not val:
            missing.append(name)
        values.append(val or "")
    if missing:
        raise ConfigError(f"Missing required configuration: {', '.join(missing)}")
    return values


# Jellyfin
JELLYFIN_URL = get_env("JELLYFIN_URL")
JELLYFIN_API_KEY = get_env("JELLYFIN_API_KEY")


# Torrent search (pyackett)
TORRENT_INDEXERS = get_env("TORRENT_INDEXERS")  # comma-separated: 1337x,therarbg,thepiratebay
TORRENT_PROXY = get_env("TORRENT_PROXY")  # socks5://user:pass@host:port

# Storage
TEMP_DOWNLOAD_LOCATION = get_env("TEMP_DOWNLOAD_LOCATION", "TEMPORARY_DOWNLOAD_LOCATION")
TV_SHOWS_SAVE_LOCATION = get_env("TV_SHOWS_SAVE_LOCATION")
MOVIES_SAVE_LOCATION = get_env("MOVIES_SAVE_LOCATION")

# Path mapping — the MCP server may see files at different paths than Jellyfin
# (e.g. NAS mounts, Docker volumes, NFS). LOCAL_PATH is where this server
# writes files; JELLYFIN_PATH is how Jellyfin sees the same location.
LOCAL_PATH_PREFIX = get_env("LOCAL_PATH_PREFIX")
JELLYFIN_PATH_PREFIX = get_env("JELLYFIN_PATH_PREFIX")


def to_jellyfin_path(local_path: str) -> str:
    """Translate a local path to the corresponding Jellyfin path."""
    if LOCAL_PATH_PREFIX and JELLYFIN_PATH_PREFIX:
        prefix = LOCAL_PATH_PREFIX.rstrip("/")
        if local_path.startswith(prefix):
            return JELLYFIN_PATH_PREFIX.rstrip("/") + local_path[len(prefix):]
    return local_path


def to_local_path(jellyfin_path: str) -> str:
    """Translate a Jellyfin path to the corresponding local path."""
    if LOCAL_PATH_PREFIX and JELLYFIN_PATH_PREFIX:
        prefix = JELLYFIN_PATH_PREFIX.rstrip("/")
        if jellyfin_path.startswith(prefix):
            return LOCAL_PATH_PREFIX.rstrip("/") + jellyfin_path[len(prefix):]
    return jellyfin_path
