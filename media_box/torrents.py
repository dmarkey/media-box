"""Torrent search via pyackett (native Python, no server needed).

The pipeline, built to keep working when individual sites don't:

1. Indexers = TORRENT_INDEXERS + API sources (Knaben, EZTV API; TORRENT_API_SOURCES),
   minus any the health tracker has benched for failing (see indexer_health).
2. TV queries ("Show S03E07") are parsed into show/season/episode, and the
   show's IMDb ID is looked up on TVMaze, so ID-based sources (EZTV API) work.
   Only the ID-based sources get the IMDb ID — a wrong TVMaze match must not
   narrow the keyword indexers' results.
3. All indexers run concurrently under one deadline; slow ones are reported
   as timed out instead of holding the search up.
4. Every indexer's outcome feeds the health tracker.
5. If results are thin, query variants are tried (punctuation stripped,
   year dropped, season pack instead of episode).
6. Results are merged by infohash: one row per torrent, best seeder count,
   every source kept so a dead download link can fall back to another.

Results are annotated, never filtered for quality — the caller (an LLM) picks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .indexer_health import IndexerHealth

logger = logging.getLogger("media_box.torrents")

CATEGORY_MAP = {"movies": 2000, "tv": 5000}
SEARCH_DIR = Path(tempfile.gettempdir()) / "media-box" / "searches"
STATE_DIR = Path.home() / ".config" / "media-box"

DEFAULT_API_SOURCES = "knaben,eztv-api"
ID_ONLY_SOURCES = {"eztv-api"}   # only useful with an IMDb ID
THIN_RESULTS = 3                 # fewer merged results than this -> try variants

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_pyackett_instance = None
_pyackett_lock: Optional[asyncio.Lock] = None
_indexer_ids: list[str] = []
_health: Optional[IndexerHealth] = None
_imdb_cache: dict[str, tuple[float, Optional[str]]] = {}


def health() -> IndexerHealth:
    global _health
    if _health is None:
        _health = IndexerHealth(STATE_DIR / "indexer_health.json")
    return _health


def configured_indexer_ids() -> list[str]:
    """TORRENT_INDEXERS plus the API sources, de-duplicated, in order."""
    ids = [s.strip() for s in (config.TORRENT_INDEXERS or "").split(",") if s.strip()]
    api = config.TORRENT_API_SOURCES or DEFAULT_API_SOURCES
    if api.strip().lower() in ("none", "off", "false", "0"):
        api = ""
    for s in api.split(","):
        s = s.strip()
        if s and s not in ids:
            ids.append(s)
    return ids


async def _get_pyackett():
    """Get or create the singleton Pyackett instance (safe under concurrency)."""
    global _pyackett_instance, _indexer_ids, _pyackett_lock
    if _pyackett_instance is not None:
        return _pyackett_instance
    if _pyackett_lock is None:
        _pyackett_lock = asyncio.Lock()
    async with _pyackett_lock:
        if _pyackett_instance is not None:
            return _pyackett_instance

        indexers = configured_indexer_ids()
        if not indexers:
            raise RuntimeError(
                "No torrent indexers configured. Set TORRENT_INDEXERS in config "
                "(e.g. TORRENT_INDEXERS=1337x,therarbg,thepiratebay)"
            )

        from pyackett import Pyackett

        pk = Pyackett(proxy=config.TORRENT_SEARCH_PROXY,
                      config_dir=str(STATE_DIR / "pyackett"))
        # GitHub fetch + YAML parsing: keep it off the event loop
        await asyncio.to_thread(pk.load_definitions_from_github, "jackett")

        ok_ids = []
        for idx_id in indexers:
            try:
                if await pk.configure_indexer(idx_id, {}):
                    ok_ids.append(idx_id)
                else:
                    logger.warning(f"Indexer {idx_id} could not be configured (unknown id?)")
            except Exception as e:
                logger.warning(f"Indexer {idx_id} failed to configure: {e}")
        _indexer_ids = ok_ids
        _pyackett_instance = pk
        return pk


# ---------------------------------------------------------------------------
# Query parsing / IMDb resolution
# ---------------------------------------------------------------------------

_EP_RE = re.compile(r"\bS(\d{1,2})\s*E(\d{1,3})\b", re.I)
_SEASON_RE = re.compile(r"\b(?:S(\d{1,2})|Season\s+(\d{1,2}))\b", re.I)
_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")


@dataclass
class ParsedQuery:
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None


def parse_query(query: str) -> ParsedQuery:
    """'Breaking Bad S03E07' -> title 'Breaking Bad', season 3, episode 7."""
    m = _EP_RE.search(query)
    if m:
        return ParsedQuery(_clean(query[:m.start()] + query[m.end():]), int(m.group(1)), int(m.group(2)))
    m = _SEASON_RE.search(query)
    if m:
        return ParsedQuery(_clean(query[:m.start()] + query[m.end():]), int(m.group(1) or m.group(2)))
    return ParsedQuery(_clean(query))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -")


async def resolve_imdb(title: str, timeout: float = 4.0) -> Optional[str]:
    """Best-effort show title -> IMDb ID via TVMaze. Cached; None on any failure."""
    key = title.lower().strip()
    if not key:
        return None
    cached = _imdb_cache.get(key)
    if cached and time.time() - cached[0] < 24 * 3600:
        return cached[1]
    imdb = None
    try:
        from .tvmaze import TVMazeClient
        async with TVMazeClient(rate_limit_delay=0) as client:
            results = await asyncio.wait_for(client.search_shows(title), timeout)
        if results:
            imdb = ((results[0].get("show") or {}).get("externals") or {}).get("imdb")
    except Exception as e:
        logger.info(f"IMDb lookup for {title!r} failed: {e}")
        return None  # don't cache transient failures
    _imdb_cache[key] = (time.time(), imdb)
    return imdb


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass
class IndexerOutcome:
    id: str
    status: str            # ok / empty / rate_limited / blocked / ... / benched
    summary: str
    probe: bool = False


@dataclass
class SearchOutcome:
    results: list[dict[str, Any]]
    outcomes: list[IndexerOutcome] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)   # extra queries tried
    imdb: Optional[str] = None
    notes: list[str] = field(default_factory=list)      # bench/recover changes

    @property
    def failed(self) -> list[IndexerOutcome]:
        return [o for o in self.outcomes if o.status not in ("ok", "empty")]


def _deadline() -> float:
    try:
        return float(config.TORRENT_SEARCH_DEADLINE or 12)
    except ValueError:
        return 12.0


async def search(
    query: str,
    category: Optional[int] = None,
    limit: Optional[int] = None,
    imdb: Optional[str] = None,
) -> SearchOutcome:
    """Search all healthy indexers; see module docstring for the pipeline."""
    from pyackett import TorznabQuery

    pk = await _get_pyackett()
    h = health()
    active, probes, benched = h.partition(_indexer_ids)
    targets = active + probes

    parsed = parse_query(query)
    is_tv = category == CATEGORY_MAP["tv"] or parsed.season is not None
    if imdb and not imdb.startswith("tt"):
        imdb = "tt" + imdb
    if not imdb and is_tv and any(t in ID_ONLY_SOURCES for t in targets):
        imdb = await resolve_imdb(parsed.title)

    outcome = SearchOutcome(results=[], imdb=imdb)
    for iid in benched:
        until, reason = h.bench_info(iid)
        outcome.outcomes.append(IndexerOutcome(
            iid, "benched", f"benched {int((until - time.time()) // 60)}m: {reason}"))

    def make_query(title: str, season: Optional[int], episode: Optional[int], with_imdb: bool):
        return TorznabQuery(
            search_term=title,
            season=season,
            episode=str(episode) if episode is not None else None,
            imdb_id=imdb if with_imdb else None,
            categories=[category] if category else [],
            limit=limit or 100,
        )

    async def run(title, season, episode, ids, record=True):
        """One round: keyword indexers and ID sources concurrently."""
        kw_ids = [i for i in ids if i not in ID_ONLY_SOURCES]
        id_ids = [i for i in ids if i in ID_ONLY_SOURCES and imdb]
        calls = []
        if kw_ids:
            calls.append(pk.search_detailed(make_query(title, season, episode, False),
                                            indexer_ids=kw_ids, deadline=_deadline()))
        if id_ids:
            calls.append(pk.search_detailed(make_query(title, season, episode, True),
                                            indexer_ids=id_ids, deadline=_deadline()))
        reports = await asyncio.gather(*calls)
        releases, statuses = [], {}
        for rep in reports:
            releases.extend(rep.results)
            statuses.update(rep.statuses)
        if record:
            for iid, st in statuses.items():
                note = h.record(iid, st.status, detail=_detail(st), elapsed=st.elapsed,
                                retry_after=st.retry_after, soft=_deadline_miss(st))
                if note:
                    outcome.notes.append(note)
        return releases, statuses

    releases, statuses = await run(parsed.title, parsed.season, parsed.episode, targets)
    for iid in targets:
        st = statuses.get(iid)
        if st is None:  # ID-only source skipped for lack of an IMDb ID
            outcome.outcomes.append(IndexerOutcome(iid, "skipped", "needs an IMDb ID"))
            continue
        outcome.outcomes.append(IndexerOutcome(iid, st.status, st.summary(), probe=iid in probes))

    merged = merge(releases)

    # Thin results: try variants against the indexers that answered
    if len(merged) < THIN_RESULTS:
        answered = [i for i, s in statuses.items() if s.ok]
        for title, season, episode in _variants(parsed):
            if not answered:
                break
            q_str = " ".join(x for x in [title, _ep_str(season, episode)] if x)
            outcome.variants.append(q_str)
            more, _ = await run(title, season, episode, answered, record=False)
            releases.extend(more)
            merged = merge(releases)
            if len(merged) >= THIN_RESULTS:
                break

    h.save()
    merged.sort(key=lambda r: r.get("Seeders") or 0, reverse=True)
    outcome.results = merged
    return outcome


def _deadline_miss(st) -> bool:
    """Our search deadline expired (vs the site's own request timing out)."""
    return st.status == "timeout" and (st.message or "").startswith("no answer within")


def _detail(st) -> str:
    if st.http_status:
        return f"HTTP {st.http_status}"
    return (st.message or "")[:80]


def _ep_str(season, episode) -> str:
    if season is None:
        return ""
    return f"S{season:02d}" + (f"E{episode:02d}" if episode is not None else "")


def _variants(p: ParsedQuery):
    """Alternative (title, season, episode) queries, most promising first."""
    seen = {(p.title.lower(), p.season, p.episode)}
    candidates = []
    simplified = re.sub(r"[^\w\s]", " ", p.title.replace("&", " and "))
    simplified = _clean(simplified)
    candidates.append((simplified, p.season, p.episode))
    no_amp = _clean(re.sub(r"[^\w\s]", " ", p.title.replace("&", " ")))
    candidates.append((no_amp, p.season, p.episode))
    no_year = _clean(_YEAR_RE.sub("", simplified))
    if no_year:
        candidates.append((no_year, p.season, p.episode))
    if p.episode is not None:
        candidates.append((no_year or simplified, p.season, None))  # season pack
    out = []
    for c in candidates:
        key = (c[0].lower(), c[1], c[2])
        if c[0] and key not in seen:
            seen.add(key)
            out.append(c)
    return out[:3]


# ---------------------------------------------------------------------------
# Merge / annotate
# ---------------------------------------------------------------------------

_BTIH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def _infohash(r) -> Optional[str]:
    if r.info_hash:
        h = r.info_hash.strip().lower()
        if len(h) == 40:
            return h
    for uri in (r.magnet_uri, r.link):
        if uri and uri.startswith("magnet:"):
            m = _BTIH_RE.search(uri)
            if m:
                h = m.group(1)
                if len(h) == 32:
                    import base64
                    h = base64.b32decode(h.upper()).hex()
                return h.lower()
    return None


def merge(releases) -> list[dict[str, Any]]:
    """One row per torrent. Same infohash from several indexers -> one row
    with the best seeder count and every source kept as a fallback."""
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    by_title: dict[str, str] = {}   # title+size key -> row key (incl. hashed rows)
    # hashed releases first, so hashless copies can fold into them regardless
    # of which indexer answered first (stable sort keeps order otherwise)
    hashed = [(r, _infohash(r)) for r in releases]
    hashed.sort(key=lambda rh: rh[1] is None)
    for r, h in hashed:
        if not (r.seeders or 0) > 0:
            continue
        # no infohash (details-page indexers): same normalised title + size
        # from two sites is the same release. Sizes are compared to 2
        # significant figures — sites round differently ("3.2 GB" vs bytes).
        tkey = f"t:{_norm_title(r.title)}:{(r.size or 0):.2g}"
        # a hashless copy joins a hashed row with the same title+size
        key = h or by_title.get(tkey, tkey)
        by_title.setdefault(tkey, key)
        source = {"tracker": r.origin_name, "tracker_id": r.origin_id,
                  "link": r.link, "magnet": r.magnet_uri}
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "Title": r.title,
                "Guid": r.guid,
                "Link": r.link,
                "Details": r.details,
                "PublishDate": r.publish_date.isoformat() if r.publish_date else None,
                "Category": r.category,
                "Size": r.size,
                "Seeders": r.seeders or 0,
                "Peers": r.peers,
                "MagnetUri": r.magnet_uri,
                "InfoHash": h,
                "Tracker": r.origin_name,
                "TrackerId": r.origin_id,
                "Sources": [source],
            }
            order.append(key)
            continue
        row["Sources"].append(source)
        if (r.seeders or 0) > row["Seeders"]:
            row["Seeders"], row["Peers"] = r.seeders, r.peers
        if not row["MagnetUri"] and r.magnet_uri:
            row["MagnetUri"] = r.magnet_uri
        if not row["Size"] and r.size:
            row["Size"] = r.size
    out = [rows[k] for k in order]
    for row in out:
        trackers = list(dict.fromkeys(s["tracker"] for s in row["Sources"] if s["tracker"]))
        row["Tracker"] = trackers[0] if len(trackers) == 1 else f"{trackers[0]} +{len(trackers) - 1}"
        row["Note"] = annotate(row)
    return out


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


_ARCHIVE_RE = re.compile(r"\.(rar|zip|7z|iso)\b", re.I)
_EXEC_RE = re.compile(r"\.(exe|lnk|scr|bat|cmd|msi)\b", re.I)
_RES_RE = re.compile(r"\b(2160p|4k|1080p|720p)\b", re.I)


def annotate(row: dict[str, Any]) -> str:
    """Short warnings for the model; never used to filter."""
    title, size = row.get("Title") or "", row.get("Size") or 0
    notes = []
    if _EXEC_RE.search(title):
        notes.append("executable?")
    elif _ARCHIVE_RE.search(title):
        notes.append("archive")
    res = (_RES_RE.search(title) or [None])[0]
    is_episode = bool(_EP_RE.search(title))
    if size and res:
        mb = size / 1e6
        floor = {"2160p": 1500, "4k": 1500, "1080p": 400, "720p": 150}[res.lower()]
        if is_episode:
            floor //= 4
        if mb < floor:
            notes.append("small for " + res.lower())
    return ", ".join(notes)


# ---------------------------------------------------------------------------
# Download-link resolution
# ---------------------------------------------------------------------------


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
    magnet_match = re.search(r'magnet:\?xt=urn:btih:[^"\'&\s]+', resp.text)
    if magnet_match:
        return magnet_match.group(0)

    raise RuntimeError(f"Could not resolve download link: {link[:80]}")


async def resolve_any(row: dict[str, Any]) -> tuple[str | bytes, str]:
    """Resolve a merged result to a magnet or .torrent bytes, trying every source.

    Returns (magnet_or_bytes, tracker used). Raises the last error if all fail.
    """
    if row.get("MagnetUri"):
        return row["MagnetUri"], row.get("Tracker") or ""
    sources = row.get("Sources") or [{"link": row.get("Link"), "tracker_id": row.get("TrackerId"),
                                      "tracker": row.get("Tracker"), "magnet": None}]
    last: Exception = RuntimeError("no download link")
    for s in sources:
        if s.get("magnet"):
            return s["magnet"], s.get("tracker") or ""
        if not s.get("link"):
            continue
        try:
            return await resolve_link(s["link"], tracker_id=s.get("tracker_id")), s.get("tracker") or ""
        except Exception as e:
            logger.info(f"Resolving via {s.get('tracker')} failed: {e}")
            last = e
    raise last


# ---------------------------------------------------------------------------
# Background maintenance: canary probes + definition refresh
# ---------------------------------------------------------------------------

# A broad query that should always return something; per-source overrides
CANARY_QUERIES = {
    "eztv-api": {"search_term": "Succession", "imdb_id": "tt7660850"},
}
CANARY_FALLBACK_TERMS = ["1080p", "the"]
CANARY_DEADLINE = 90.0


async def run_canaries() -> list[str]:
    """Probe every configured indexer with a broad query; feed health.

    Catches what user searches can't: a site that answers but never matches
    anything (broken definition), and lets benched indexers recover without
    waiting for a user search to probe them.
    """
    from pyackett import TorznabQuery

    pk = await _get_pyackett()
    h = health()
    notes = []

    async def probe(iid):
        spec = CANARY_QUERIES.get(iid)
        terms = [spec] if spec else [{"search_term": t} for t in CANARY_FALLBACK_TERMS]
        st = None
        for t in terms:
            # long enough for a request timeout (30s) plus a Cloudflare solve
            # queued behind others, so a probe sees the site's real outcome
            rep = await pk.search_detailed(TorznabQuery(limit=20, **t), indexer_ids=[iid],
                                           deadline=CANARY_DEADLINE)
            st = rep.statuses[iid]
            if not st.ok or st.result_count:
                break
        return iid, st

    for iid, st in await asyncio.gather(*(probe(i) for i in _indexer_ids)):
        note = h.record_canary(iid, st.status, st.result_count, detail=_detail(st),
                               elapsed=st.elapsed, soft=_deadline_miss(st))
        if note:
            notes.append(note)
    h.save()
    return notes


async def maintenance_loop() -> None:
    """Warm pyackett at startup, then canaries every TORRENT_CANARY_INTERVAL
    hours and a definitions refresh every 12h."""
    try:
        canary_hours = float(config.TORRENT_CANARY_INTERVAL or 6)
    except ValueError:
        canary_hours = 6.0
    await asyncio.sleep(5)
    try:
        await _get_pyackett()
    except Exception as e:
        logger.warning(f"Search init failed (will retry on first search): {e}")
    last_canary = 0.0 if canary_hours > 0 else float("inf")
    last_refresh = time.time()
    first_canary_at = time.time() + 60
    while True:
        now = time.time()
        try:
            if canary_hours > 0 and now >= first_canary_at and now - last_canary >= canary_hours * 3600:
                last_canary = now
                for note in await run_canaries():
                    logger.info(f"Canary: {note}")
            if now - last_refresh >= 12 * 3600:
                last_refresh = now
                pk = await _get_pyackett()
                if await pk.refresh_definitions():
                    logger.info("Indexer definitions updated")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Search maintenance failed: {e}")
        await asyncio.sleep(60)
