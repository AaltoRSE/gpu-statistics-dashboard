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

    Each in-flight entry is a ``(generation, future)`` pair. Forced
    refresh supersedes a pre-refresh flight by BUMPING the generation
    and installing a fresh future: the old leader finishes but no
    longer owns its generation, so it publishes nothing, pops
    nothing, and only wakes its own already-joined followers with its
    (now superseded) result — no ``InvalidStateError`` against a
    cancelled future, no stomping the forced generation's entry.
    """

    def __init__(self, max_size=256):
        self._store = {}
        self._inflight = {}  # key -> [generation, Future] (leader-owned)
        self._stale = set()  # keys whose next fetch must be a fresh one
        self._lock = threading.Lock()
        self._max_size = max_size
        self._generation = 0

    def get_or_set(self, key, ttl, fn):
        with self._lock:
            hit = self._store.get(key)
            if key in self._stale:
                # Forced-refresh marker: a warm store hit is not good
                # enough and a pre-refresh in-flight fetch must not be
                # joined. The first post-invalidate caller drops the
                # stale future and becomes a fresh leader; its
                # generation already prevents the old leader from
                # publishing. Discarding the marker IMMEDIATELY (same
                # lock) keeps single-flight: later concurrent callers
                # see no marker and simply join the new leader.
                self._stale.discard(key)
                self._inflight.pop(key, None)
                hit = None  # ignore the (possibly warm) store entry
            if hit and hit[0] > time.monotonic():
                return hit[1]
            entry = self._inflight.get(key)
            if entry is not None:
                generation, future = entry
                is_leader = False
            else:
                self._generation += 1
                generation = self._generation
                future = Future()
                self._inflight[key] = [generation, future]
                is_leader = True
        if not is_leader:
            return future.result()
        try:
            value = fn()
        except BaseException as exc:
            # Drop the entry so the next caller retries instead of
            # inheriting this exception forever; still deliver it to
            # any follower already waiting on this one. A superseded
            # leader (invalidate raced it) pops nothing — its entry
            # belongs to a newer generation.
            with self._lock:
                entry = self._inflight.get(key)
                if entry is not None and entry[0] == generation:
                    self._inflight.pop(key, None)
            future.set_exception(exc)
            raise
        with self._lock:
            entry = self._inflight.get(key)
            if entry is not None and entry[0] == generation:
                if len(self._store) > self._max_size:
                    self._store.clear()
                self._store[key] = (time.monotonic() + ttl, value)
                self._inflight.pop(key, None)
                self._stale.discard(key)
        # Wake joined followers; a superseded leader's future is its
        # own object and no longer referenced by _inflight.
        future.set_result(value)
        return value

    def invalidate(self, *keys):
        """Drop specific entries; used by the forced-refresh path.

        A key with an in-flight pre-refresh fetch is SUPERSEDED via the
        stale marker: the next get_or_set ignores any warm store value
        AND drops the pre-refresh future, starting a fresh leader
        flight — it never joins the stale one, and no Future is
        pre-installed that nobody would complete. The old leader still
        finishes and wakes its own already-joined followers, but its
        store publish is suppressed by the generation check.
        """
        with self._lock:
            for key in keys:
                self._store.pop(key, None)
                self._stale.add(key)

    def clear(self):
        """Drop everything, superseding every in-flight fetch (forced
        refresh must not join a pre-refresh generation)."""
        with self._lock:
            self._store.clear()
            self._stale.update(self._inflight)


    def set(self, key, ttl, value):
        """Store a value directly (used by the VRAM envelope-alignment
        refetch in domain/jobs.py, which must overwrite a stale entry
        without joining or creating an in-flight fetch)."""
        with self._lock:
            if len(self._store) > self._max_size:
                self._store.clear()
            self._store[key] = (time.monotonic() + ttl, value)


# ---- key builders --------------------------------------------------
# One function per cached value, called by both whoever reads it and
# whoever invalidates it.

def job_utilization_key(since_hours, user=None):
    """The shared per-window utilization range query every job-list
    consumer reads: the Jobs tab, the Users aggregation, and the VRAM
    chart's job records all build on the SAME Prometheus fetch, so the
    cache identity must not vary by caller (include_vram was folded
    into this key and made the VRAM route re-run the identical query).
    ``user`` keeps the query-scoped identity: a single-user request
    must not share with (nor evict) the whole-window fetch."""
    return ("job_utilization", since_hours, user)


def job_vram_key(since_hours):
    """The per-window VRAM percentage range query, cached separately
    from utilization so utilization-only callers never pay for it.
    Deliberately user-independent: the query aggregates per-job peaks
    and carries no user selector, so a user-scoped Jobs request shares
    the global VRAM fetch instead of duplicating it."""
    return ("job_vram", since_hours)


def sacct_key(job_ids):
    return ("sacct", tuple(sorted(job_ids)))


def sacct_resilient_key(job_ids):
    """Cache key for the resilient enrichment's (dict, failed) tuple.

    Deliberately distinct from :func:`sacct_key`: that key holds the plain
    dict the Jobs list/detail paths consume, and storing the tuple under it
    would hand the other consumer the wrong shape for the cache's TTL.
    """
    return ("sacct_resilient", tuple(sorted(job_ids)))


def scontrol_jobs_key():
    return "scontrol_jobs"


def scontrol_nodes_key():
    return "scontrol_nodes"


def job_detail_key(jobid, since_hours):
    return ("jobdetail", jobid, since_hours)


def partition_window_key(since_hours, running_only):
    return ("parts", since_hours, running_only)


def vram_key(since_hours, running_only):
    return ("vram_gb", since_hours, running_only)


def completed_jobs_key(since_hours):
    """The accounting cache identity, keyed like the progress store.

    ``since_hours`` (not the captured epoch window) is the identity: the
    request's ``now`` changes every second, so an epoch key would never
    hit the 300s TTL cache in production. Same ``since_hours`` requests
    join one fetch and its shared progress state; ``running_only`` is
    deliberately excluded for the same reason progress omits it.
    """
    return ("completed_jobs", since_hours)


def completed_progress_key(since_hours):
    """The stable progress-store key shared by the queue and progress routes.

    It deliberately omits ``running_only``: the accounting cache single-
    flights on ``(start, end)`` alone, so two same-window requests that
    differ only in that flag join ONE fetch. Keying progress by the flag
    would leave the follower polling a key the leader never publishes.
    Progress is per-batch state of the shared fetch, not per-response
    view, so the flag has no place in this identity.
    """
    return ("completed_progress", since_hours)


def node_current_key():
    return "node_current"


def node_detail_key(name, view, start):
    return ("nodedetail", name, view, start)


