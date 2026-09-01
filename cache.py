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


class TtlCache:
    """A ``{key: (expiry, value)}`` cache with size-bounded eviction.

    Not a general LRU: once the store exceeds ``max_size`` it is
    cleared entirely on the next write, rather than evicting the
    oldest entries individually. That matches what both prior
    implementations already did, and is simple enough to reason about
    for a cache holding at most a few hundred short-lived entries.
    """

    def __init__(self, max_size=256):
        self._store = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def get_or_set(self, key, ttl, fn):
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = fn()
        with self._lock:
            if len(self._store) > self._max_size:
                self._store.clear()
            self._store[key] = (time.monotonic() + ttl, value)
        return value

    def invalidate(self, *keys):
        """Drop specific entries; used by the forced-refresh path."""
        with self._lock:
            for key in keys:
                self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()


# ---- key builders --------------------------------------------------
# One function per cached value, called by both whoever reads it and
# whoever invalidates it.

def job_window_key(since_hours, include_vram, user):
    return ("jobs", since_hours, include_vram, user)


def sacct_key(job_ids):
    return ("sacct", tuple(sorted(job_ids)))


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


def node_current_key():
    return "node_current"


def node_detail_key(name, view, start):
    return ("nodedetail", name, view, start)
