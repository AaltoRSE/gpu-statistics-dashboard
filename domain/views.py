"""Window-view derivations shared by the Jobs/Users/Partitions routes.

Every per-window view (per-job aggregates, the partition summary, trend,
occupancy) is one pass over the SAME per-GPU raw series
``sources.gpu_util`` returns (plan §2). Computing them in process —
memoized per (window, scontrol fingerprint) — replaces the four separate
PromQL aggregations the tabs each used to run, so a tab change costs no
upstream query at all.
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


def window_views(win, raw, vram_series, node_gpu_types):
    """Memoized per-window views: jobs + partition + bounds, one pass.

    ``win`` is the pinned ``(start, end, step)`` triple; ``raw`` the
    per-GPU utilization series and ``vram_series`` the VRAM % series
    (the window's second source, feeding the job view's ``vram_avg``).
    Returns ``{"jobs", "partition", "start", "end", "step", "raw"}``
    where ``partition`` is the ``(rows, trend, instances, occupancy,
    job_groups)`` tuple from domain.partitions.partition_view
    (configured types included — these are full-window views). The memo
    is keyed by window + node fingerprint, so concurrent tabs share one
    computation and a re-pinned window or changed scontrol index
    recomputes.
    """
    start, end, step = win
    fingerprint = _node_fingerprint(node_gpu_types)

    def compute():
        return {
            "jobs": job_view(raw, step, vram_series),
            "partition": partition_view(raw, node_gpu_types),
        }

    views = deps.route_cache.get_or_set(
        cache.window_views_key(start, end, step, fingerprint), 60, compute)
    return {"jobs": views["jobs"], "partition": views["partition"],
            "start": start, "end": end, "step": step, "raw": raw}
