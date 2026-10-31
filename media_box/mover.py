import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Allowed extensions
# ---------------------------------------------------------------------------

MEDIA_EXTENSIONS = {
    # Video
    ".mkv", ".mp4", ".avi", ".wmv", ".flv", ".mov", ".webm", ".ts", ".m4v",
    # Subtitle
    ".srt", ".sub", ".ass", ".ssa", ".vtt",
}


_EXEC_EXTENSIONS = {".exe", ".lnk", ".scr", ".bat", ".cmd", ".msi", ".pif", ".vbs"}
_ARCHIVE_RE = re.compile(r"\.(rar|zip|7z|tar|gz|r\d\d|\d{3})$", re.I)
_VIDEO_EXTENSIONS = {e for e in MEDIA_EXTENSIONS if e not in {".srt", ".sub", ".ass", ".ssa", ".vtt"}}


def inspect_payload(files: list[dict]) -> tuple[str, str]:
    """Judge a torrent by its file list: ("ok"|"warn"|"reject", detail)."""
    names = [f.get("name", "") for f in files]
    exts = {Path(n).suffix.lower() for n in names}
    bad = sorted(exts & _EXEC_EXTENSIONS)
    if bad:
        return "reject", f"contains executable files ({', '.join(bad)})"
    if exts & _VIDEO_EXTENSIONS:
        return "ok", ""
    if any(_ARCHIVE_RE.search(n) for n in names):
        return "warn", ("payload is an archive (no video files) — it needs extracting "
                        "before the mover can import it")
    shown = ", ".join(sorted(e or "(none)" for e in exts)[:6])
    return "reject", f"no video files in the torrent (found: {shown})"
