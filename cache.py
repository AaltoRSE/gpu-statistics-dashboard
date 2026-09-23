"""A small TTL cache plus the named key builders that use it.

Two hand-rolled dict-plus-lock caches used to implement the same
TTL-and-flush logic independently (app.py's route cache and
PromClient's response cache). One implementation here, used by both.

Cache keys used to be tuples and bare strings re-typed at each call
site — a reader building ``("jobs", since_hours, True, user)`` and a
separate invalidator building the same tuple by hand a few hundred
lines away, with nothing keeping them in sync. The key-builder
functions below are the only way a key for a given cached value gets
built, so a reader and its invalidator can't drift apart.
"""

import threading
import time
from concurrent.futures import Future


class TtlCache:
    """A ``{key: (expiry, value)}`` cache with size-bounded eviction.

    Not a general LRU: once the store exceeds ``max_size`` it is
    cleared entirely on the next write, rather than evicting the
    oldest entries individually. That matches what both prior
    implementations already did, and is simple enough to reason about
    for a cache holding at most a few hundred short-lived entries.

    ``get_or_set`` single-flights concurrent misses on the same key: a
    cold seven-day-window request from two browser tabs at once used
    to issue the same Prometheus range query twice, each occupying a
    threadpool worker for its duration. The first caller to miss
    becomes the leader and runs ``fn``; any other caller that misses
    on the same key while the leader is still running joins its
    ``Future`` and gets the same result (or the same exception)
    instead of starting its own fetch.
    """

    def __init__(self, max_size=256):
        self._store = {}
        self._inflight = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def get_or_set(self, key, ttl, fn):
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            future = self._inflight.get(key)
            if future is not None:
                is_leader = False
            else:
                future = Future()
                self._inflight[key] = future
                is_leader = True
        if not is_leader:
            return future.result()
        try:
            value = fn()
        except BaseException as exc:
            # Drop the future so the next caller retries instead of
            # inheriting this exception forever; still deliver it to
            # any follower already waiting on this one.
            with self._lock:
                self._inflight.pop(key, None)
            future.set_exception(exc)
            raise
        with self._lock:
            if len(self._store) > self._max_size:
                self._store.clear()
            self._store[key] = (time.monotonic() + ttl, value)
            self._inflight.pop(key, None)
        future.set_result(value)
        return value

    def peek(self, key):
        """``(True, value)`` when an unexpired entry exists, else
        ``(False, None)``. Non-blocking: it never joins an in-flight
        fetch, so a caller deciding between reusing a cached read and
        issuing its own can probe without single-flight side effects."""
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > time.monotonic():
                return True, hit[1]
        return False, None

    def invalidate(self, *keys):
        """Drop specific entries; used by the forced-refresh path."""
        with self._lock:
            for key in keys:
                self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()


class KeyedBatchCache:
    """Per-key TTL store plus per-key in-flight futures, for fan-out
    caches whose identity is a small key (a job ID) rather than a whole
    request.

    ``get_or_set`` single-flights one whole request key; a 2000-ID sacct
    enrichment instead asks about 2000 small keys at once, of which a
    later caller typically needs only a handful. ``get_batch`` returns
    cached-and-unexpired keys immediately, lets concurrent callers join
    keys already being fetched, and hands exactly the remainder to one
    leader's ``fetch_missing(missing_keys) -> {key: value}``. Keys the
    fetch OMITS from its result (a failed batch's IDs) resolve to None
    for joiners and store no entry — never negative-cache a failure, so
    the next request retries them.

    The locking follows ``TtlCache.get_or_set``: one lock, one ``Future``
    per in-flight key, a leader that raises drops its in-flight entries
    and delivers the exception to joiners. Expired entries are pruned
    opportunistically when a lookup walks past them.
    """

    def __init__(self, max_size=4096):
        self._store = {}
        self._inflight = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def get_batch(self, keys, fetch_missing, ttl_for):
        """Return ``{key: value}`` for every key in ``keys``.

        ``ttl_for(key, value)`` gives the per-key TTL in seconds, since a
        row's freshness depends on its content (terminal job states cache
        far longer than running ones).
        """
        keys = list(dict.fromkeys(keys))
        cached = {}
        missing = []
        join = {}  # key -> Future another caller is fetching
        now = time.monotonic()
        with self._lock:
            for key in keys:
                hit = self._store.get(key)
                if hit and hit[0] > now:
                    cached[key] = hit[1]
                elif key in self._inflight:
                    join[key] = self._inflight[key]
                else:
                    missing.append(key)
            for key in tuple(self._store):
                hit = self._store[key]
                if hit[0] <= now:
                    self._store.pop(key, None)

        if missing:
            leader_future = Future()
            with self._lock:
                # Re-check under the lock: a caller between the scan above
                # and here may have started the same keys.
                still_missing = []
                for key in missing:
                    existing = self._inflight.get(key)
                    if existing is not None:
                        join[key] = existing
                    else:
                        self._inflight[key] = leader_future
                        still_missing.append(key)
                missing = still_missing
            if missing:
                try:
                    fetched = fetch_missing(missing)
                except BaseException as exc:
                    with self._lock:
                        for key in missing:
                            self._inflight.pop(key, None)
                    leader_future.set_exception(exc)
                    raise
                with self._lock:
                    if len(self._store) + len(fetched) > self._max_size:
                        self._store.clear()
                    for key in missing:
                        value = fetched.get(key)
                        if value is not None:
                            self._store[key] = (
                                time.monotonic() + ttl_for(key, value), value)
                        self._inflight.pop(key, None)
                # Omitted keys (a failed batch) resolve to None below and
                # are never stored — the next request retries them.
                leader_future.set_result(fetched)
                for key in missing:
                    if fetched.get(key) is not None:
                        cached[key] = fetched[key]
        for key, future in join.items():
            value = future.result().get(key)
            if value is not None:
                cached[key] = value
        return {key: cached.get(key) for key in keys}

    def clear(self):
        """Drop every entry; test isolation for module-level instances."""
        with self._lock:
            self._store.clear()
            self._inflight.clear()


# ---- key builders --------------------------------------------------
# One function per cached value, called by both whoever reads it and
# whoever invalidates it.

def scontrol_jobs_key():
    return "scontrol_jobs"


def scontrol_nodes_key():
    return "scontrol_nodes"


def job_detail_key(jobid, since_hours):
    return ("jobdetail", jobid, since_hours)


def vram_progress_key(since_hours):
    """The shared window fetch's stable progress-store key, read by both
    the /api/partitions/queue/progress and /api/partitions/vram/progress
    poll routes.

    Both the queue's wait history and the VRAM enrichment now read ONE
    sacct window dump (plan §3), so one key serves both polls: the dump's
    batch work varies only with the window, and the partition and
    running-only parameters have become in-process response views that
    change no fetch. weight is likewise response-shape only, and the
    epoch is excluded so a poll always resolves the in-flight fetch's
    entry.
    """
    return ("vram_progress", since_hours)


def node_detail_key(name, view, start):
    return ("nodedetail", name, view, start)


def pinned_window_key(since_hours):
    """The pinned (start, end, step) triple for a since_hours window.

    Pinning for the route cache's TTL (60 s) means every tab opened
    within one TTL reads window sources fetched for identical bounds,
    and each response reports the window its data actually covers
    (plan §1) instead of recomputing bounds from a clock that has moved.
    """
    return ("pinned_window", since_hours)


def window_source_key(name, start, end, step):
    """Identity of one named window source (a Prometheus range fetch).

    Every window source is keyed by the (start, end, step) it was
    fetched for — the pinned window's triple — so all tabs within one
    pinned-window TTL share one fetch per source, and a re-pinned
    window simply addresses a different key.
    """
    return ("window_source", name, start, end, step)


def snapshot_key():
    """The live-snapshot source's identity: one 30 s TTL for both of its
    instant queries, so running-only views and the Nodes view read the
    same instant (plan §1)."""
    return ("snapshot",)


def window_views_key(start, end, step, fingerprint):
    """Identity of one memoized window-views computation (plan §2).

    The per-window aggregation passes over the per-GPU raw series (job
    aggregates, partition rows, trend, occupancy) run once per window and
    per scontrol fingerprint — the fingerprint, not the node dict's
    identity, because every scontrol TTL builds a fresh node list whose
    group resolutions must not be reused across snapshots.
    """
    return ("win_views", start, end, step, fingerprint)


def sacct_window_key(since_hours):
    """The window-wide sacct dump's cache identity (plan §3).

    Keyed by ``since_hours``, not the captured epoch bounds: the dump's
    bounds are cached alongside its records (``(records, coverage,
    start, end)``), so a later hit filters against the bounds it was
    actually fetched for — the same "cache the fetched bounds" behavior
    the queue route uses today.
    """
    return ("sacct_window", since_hours)


def day_chunk_key(day_start_iso):
    """One full past day of the sacct window dump, cached 1 h (plan §3).

    Full past days are immutable once their midnight passes, so they
    survive the 300 s window-entry TTL; the two edge chunks (running
    jobs' Elapsed/State must be current) are never cached here.
    """
    return ("sacct_day_chunk", day_start_iso)


progress_store = {}
"""Latest batched accounting progress, keyed per fetch scope.

Two publishers write here and their poll routes read it: the queue's
long-window wait-history fetch (the shared sacct window dump, keyed by
``cache.sacct_window_key``) and the VRAM distribution's sacct
enrichment (keyed by ``cache.vram_progress_key``). Each publisher
seeds its entry at fetch start ({"done", "total", "failed_batches"}),
replaces it after every finished batch, and pops it in a ``finally`` —
so a poll never observes a finished or failed fetch's stale state, and
a fetch that crashed leaves no in-flight-looking entry behind. Only the
cache-miss leader that actually runs the fetch touches its key;
followers that join via the route-cache Future never do.
"""


