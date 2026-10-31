"""Per-indexer health tracking and automatic benching (a circuit breaker).

Every search outcome is recorded. An indexer that keeps failing is *benched*:
skipped by searches for an escalating cooldown (15m -> 1h -> 6h -> 24h). When
the cooldown ends it becomes a *probe*: the next search includes it once. If
that succeeds it rejoins fully; if not, it's benched again for longer. A
rate-limit (HTTP 429) benches straight away, honouring Retry-After.

"Empty" answers (site responded, nothing matched) are healthy — a niche query
legitimately finds nothing. A site that answers but never returns anything
(a broken definition) is caught by canary probes instead: a known-broad query
that should always match, run periodically in the background.

State persists to JSON so a restart doesn't forget that a site is dead.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("media_box.indexer_health")

BENCH_STEPS = [15 * 60, 60 * 60, 6 * 3600, 24 * 3600]
FAILS_BEFORE_BENCH = 3          # consecutive hard failures
CANARY_EMPTIES_BEFORE_BENCH = 2  # consecutive canary runs with zero results
EWMA = 0.3                       # weight of the newest latency sample

OK_STATES = {"ok", "empty"}


class IndexerHealth:
    def __init__(self, path: Path):
        self._path = path
        self._state: dict[str, dict[str, Any]] = {}
        try:
            self._state = json.loads(path.read_text())
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Ignoring unreadable indexer health file {path}: {e}")

    # -- queries ---------------------------------------------------------

    def _get(self, iid: str) -> dict[str, Any]:
        return self._state.setdefault(iid, {
            "fails": 0, "level": 0, "benched_until": 0.0, "bench_reason": "",
            "last_status": "", "last_detail": "", "last_ok": 0.0, "last_fail": 0.0,
            "ok": 0, "failed": 0, "latency": None, "canary_empty": 0,
        })

    def partition(self, ids: list[str], now: Optional[float] = None) -> tuple[list[str], list[str], list[str]]:
        """Split indexer ids into (active, probes, benched).

        active = healthy; probes = bench expired, gets one trial in this
        search; benched = skip. Searches should use active + probes.
        """
        now = time.time() if now is None else now
        active, probes, benched = [], [], []
        for iid in ids:
            st = self._state.get(iid)
            if not st or not st.get("benched_until"):
                active.append(iid)
            elif st["benched_until"] > now:
                benched.append(iid)
            else:
                probes.append(iid)
        return active, probes, benched

    def bench_info(self, iid: str) -> tuple[float, str]:
        st = self._state.get(iid) or {}
        return st.get("benched_until", 0.0), st.get("bench_reason", "")

    # -- updates ---------------------------------------------------------

    def record(self, iid: str, status: str, *, detail: str = "", elapsed: float = 0.0,
               retry_after: Optional[float] = None, now: Optional[float] = None) -> Optional[str]:
        """Record one search outcome. Returns a short note if the bench state changed."""
        now = time.time() if now is None else now
        st = self._get(iid)
        st["last_status"], st["last_detail"] = status, detail
        if elapsed:
            st["latency"] = elapsed if st["latency"] is None else (
                EWMA * elapsed + (1 - EWMA) * st["latency"])

        if status in OK_STATES:
            st["ok"] += 1
            st["last_ok"] = now
            was_benched = bool(st["benched_until"])
            st["fails"], st["level"], st["benched_until"], st["bench_reason"] = 0, 0, 0.0, ""
            if was_benched:
                logger.info(f"Indexer {iid} recovered")
                return f"{iid} recovered"
            return None

        st["failed"] += 1
        st["last_fail"] = now
        st["fails"] += 1
        if status == "rate_limited":
            return self._bench(iid, st, now, f"rate limited ({detail})", minimum=retry_after)
        if st["fails"] >= FAILS_BEFORE_BENCH or st["benched_until"]:
            # a failed probe re-benches immediately, for longer
            return self._bench(iid, st, now, f"{st['fails']} failures in a row, last: {status} ({detail})")
        return None

    def record_canary(self, iid: str, status: str, result_count: int, *, detail: str = "",
                      elapsed: float = 0.0, now: Optional[float] = None) -> Optional[str]:
        """Record a background canary probe (a broad query that should always match)."""
        now = time.time() if now is None else now
        st = self._get(iid)
        if status in OK_STATES and result_count == 0:
            st["canary_empty"] += 1
            st["last_status"], st["last_detail"] = "empty", "canary query returned nothing"
            if st["canary_empty"] >= CANARY_EMPTIES_BEFORE_BENCH:
                return self._bench(iid, st, now, "canary query returned nothing — definition likely broken")
            return None
        st["canary_empty"] = 0
        return self.record(iid, status, detail=detail, elapsed=elapsed, now=now)

    def _bench(self, iid: str, st: dict, now: float, reason: str,
               minimum: Optional[float] = None) -> str:
        step = BENCH_STEPS[min(st["level"], len(BENCH_STEPS) - 1)]
        duration = max(step, minimum or 0)
        st["level"] += 1
        st["benched_until"] = now + duration
        st["bench_reason"] = reason
        logger.warning(f"Benching indexer {iid} for {_fmt(duration)}: {reason}")
        return f"{iid} benched for {_fmt(duration)}: {reason}"

    def reset(self, iid: Optional[str] = None) -> list[str]:
        """Clear bench state for one indexer (or all). Returns the ids reset."""
        ids = [iid] if iid else list(self._state)
        done = []
        for i in ids:
            st = self._state.get(i)
            if st:
                st.update(fails=0, level=0, benched_until=0.0, bench_reason="", canary_empty=0)
                done.append(i)
        return done

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=1))
            tmp.replace(self._path)
        except Exception as e:
            logger.warning(f"Could not save indexer health: {e}")

    # -- reporting -------------------------------------------------------

    def rows(self, ids: list[str], now: Optional[float] = None) -> list[dict[str, str]]:
        now = time.time() if now is None else now
        out = []
        for iid in ids:
            st = self._state.get(iid) or {}
            total = st.get("ok", 0) + st.get("failed", 0)
            until = st.get("benched_until") or 0
            if until > now:
                state = f"benched {_fmt(until - now)}"
            elif until:
                state = "probe next"
            else:
                state = "active"
            out.append({
                "id": iid,
                "state": state,
                "success": f"{100 * st.get('ok', 0) // total}% of {total}" if total else "—",
                "latency": f"{st['latency']:.1f}s" if st.get("latency") is not None else "—",
                "last": (st.get("last_status", "") + (f" ({st['last_detail']})" if st.get("last_detail") else "")) or "—",
                "reason": st.get("bench_reason", "") if until else "",
            })
        return out


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"
