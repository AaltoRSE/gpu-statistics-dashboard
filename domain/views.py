"""Window-view derivations shared by the Jobs/Users/Partitions routes.

Every per-window view (per-job aggregates, the partition summary, trend,
occupancy) is one pass over the SAME per-GPU raw series
``sources.gpu_util`` returns (plan §2). Computing them in process —
memoized per (window, scontrol fingerprint) — replaces the four separate
PromQL aggregations the tabs each used to run, so a tab change costs no
upstream query at all.

The two views memoize separately because their callers fetch different
sources: the job view needs the VRAM % series (``vram_avg``) while the
partition view needs nothing beyond the raw utilization series — a
Partitions-tab request must not pay for, or trigger, the VRAM query.
"""

import hashlib
import json

import cache
import deps
from domain.common import aggregate_by
from domain.jobs import job_aggregates
from domain.partitions import partition_view

# Q1's grouping: per-job/per-instance max over the per-GPU raw series.
Q1_LABELS = ["slurmjobid", "instance", "job", "user", "gpu_type"]


def job_view(raw, step, vram_series=()):
    """The Jobs-tab job dicts from the raw per-GPU window series."""
    q1 = aggregate_by(raw, Q1_LABELS, "max")
    return job_aggregates(q1, step, vram_series)


def _node_fingerprint(node_gpu_types):
    """A stable fingerprint of the node -> GPU-type index.

    The memo must not reuse group resolutions across scontrol snapshots:
    every TTL builds a fresh node list, and a node's GRES (its GPU
    types) can change between snapshots. The fingerprint — not the
    dict's identity — keys the cache, so a changed index re-derives.
    """
    payload = json.dumps(
        sorted((n, sorted(types)) for n, types in node_gpu_types.items()),
        sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def partition_views(win, raw, node_gpu_types):
    """Memoized partition-side view of one window (plan §2).

    ``win`` is the pinned ``(start, end, step)`` triple; ``raw`` the
    per-GPU utilization series. Returns ``partition_view``'s
    ``(rows, trend, instances, occupancy, job_groups)`` tuple
    (configured types included — these are full-window views). The memo
    is keyed by window + node fingerprint, so concurrent tabs share one
    computation and a re-pinned window or changed scontrol index
    recomputes.
    """
    start, end, step = win
    fingerprint = _node_fingerprint(node_gpu_types)
    return deps.route_cache.get_or_set(
        cache.partition_views_key(start, end, step, fingerprint), 60,
        lambda: partition_view(raw, node_gpu_types))


def job_views(win, raw, node_gpu_types, vram_series=()):
    """Memoized job-side view of one window (plan §2).

    Same memo discipline as ``partition_views``; returns the job dicts
    ``job_view`` builds. ``vram_series`` (the VRAM % series) fills
    ``vram_avg`` — callers that don't fetch it omit it, and the memo key
    keeps the with-VRAM and without-VRAM computations distinct so one
    caller's rows never leak into the other's.
    """
    start, end, step = win
    fingerprint = _node_fingerprint(node_gpu_types)
    return deps.route_cache.get_or_set(
        cache.job_views_key(start, end, step, fingerprint,
                            bool(vram_series)), 60,
        lambda: job_view(raw, step, vram_series))
