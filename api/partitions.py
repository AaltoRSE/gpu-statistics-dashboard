"""Routes: GET /api/partitions, GET /api/partitions/vram."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
from api.schemas import PartitionQueueResponse, PartitionsResponse, VramResponse
from domain.common import window
from domain.partitions import (
    WAIT_TOTAL_KEY,
    completed_wait_summary,
    gpu_capacity,
    node_current,
    partition_window,
    pending_queue_status,
    progress_store,
    wait_empty,
)
from domain.vram import vram_job_records
from slurm import SlurmError

router = APIRouter()


def _queue_snapshot(now, partition_types):
    """Pending-job rows, per-GPU-type summary, and reachability.

    A failed or missing squeue is an explicit ``available=False`` state,
    never an empty queue: an operator must be able to tell "nothing
    pending" from "the queue is unreadable". squeue runs on every queue
    request — the queue is a live scheduler snapshot and a time-window
    change must refetch it rather than redisplay a cached view (the
    Prometheus side stays cached through ``partition_window``).
    ``wait_s`` is computed from ``now`` so a displayed waiting age is
    current at response time. ``partition_types`` (the partition ->
    GPU-types map from the same cached scontrol node snapshot every
    other group in this route uses) drives the pending rows' type
    eligibility — no extra scontrol call.
    """
    try:
        jobs = deps.queue_pending()
    except SlurmError:
        return {}, {"unique_pending_jobs": None,
                    "unique_gpus_requested": None}, [], False
    summary, totals, waiting_jobs = pending_queue_status(
        jobs, now, partition_types)
    return summary, totals, waiting_jobs, True


@router.get("/api/partitions", response_model=PartitionsResponse)
def api_partitions(since_hours: float = Query(24, gt=0, le=168),
                   running_only: bool = Query(False)):
    nodes = deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes)
    node_types = gpu_groups.build_node_index(nodes)
    groups, trend, instances, occupancy, job_groups, start, now, step = \
        partition_window(since_hours, running_only, node_gpu_types=node_types)
    _, _, allocs_by_node, allocs_by_group = node_current(node_types)
    gpu_capacity(groups, instances, nodes, allocs_by_group)
    for g in groups:
        avg_alloc = occupancy.get(g["name"])
        total = g.get("gpus_total") or 0
        if avg_alloc is not None and total > 0:
            g["mean_occupancy"] = round(min(100.0, avg_alloc / total * 100.0), 1)
    return {
        "window": window(start, now),
        "step": step,
        "partitions": groups,
        "trend": trend,
    }


@router.get("/api/partitions/queue",
            response_model=PartitionQueueResponse)
def api_partition_queue(since_hours: float = Query(24, gt=0, le=168),
                        running_only: bool = Query(False)):
    """Live pending-job queue and historical waits, independent of the
    utilization endpoint.

    squeue (live snapshot) and sacct (wait history) are typically slower
    than the Prometheus-backed /api/partitions, so the Partitions tab
    fetches both endpoints concurrently and renders whichever arrives
    first under its own panel. Reusing ``partition_window`` costs
    nothing: its ``partition_window_key`` cache and single-flight mean
    the concurrent core request's Prometheus fetch is shared, and a
    queue request landing later is a cache hit.

    Current queue and historical wait degrade independently: a dead
    squeue must not hide a valid sacct history and vice versa, and
    neither failure may turn the whole response into an error or
    masquerade as a zero.
    """
    nodes = deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30,
                                        deps.show_nodes)
    node_types = gpu_groups.build_node_index(nodes)
    partition_types = gpu_groups.partition_gpu_types(nodes)
    groups, _, _, _, _, start, now, _ = partition_window(
        since_hours, running_only, node_gpu_types=node_types)
    queue, totals, waiting_jobs, queue_available = _queue_snapshot(
        now, partition_types)
    try:
        tz = ZoneInfo("Europe/Helsinki")
        start_iso = datetime.fromtimestamp(start, tz).replace(
            tzinfo=None).isoformat(timespec="seconds")
        end_iso = datetime.fromtimestamp(now, tz).replace(
            tzinfo=None).isoformat(timespec="seconds")
        cache_key = cache.completed_jobs_key(start, now)
        progress_key = cache.completed_progress_key(since_hours, running_only)

        def fetch():
            # Publish under every joining request's lookup key: accounting
            # itself single-flights on the epoch cache_key, but a follower
            # with a different running_only polls its own parameter-derived
            # progress key and must still see the shared fetch's state.
            for key in (cache_key, progress_key):
                progress_store[key] = {"done": 0, "total": 0,
                                       "failed_batches": 0}

            def report(state):
                for key in (cache_key, progress_key):
                    progress_store[key] = state

            result = deps.completed_jobs(start_iso, end_iso, report)
            for key in (cache_key, progress_key):
                progress_store.pop(key, None)
            return result

        records, accounting_coverage = deps.route_cache.get_or_set(
            cache_key, 300, fetch)
        wait_history, wait_history_coverage = completed_wait_summary(
            records, node_types, start, now, accounting_coverage)
        wait_history_available = bool(accounting_coverage["successful_batches"])
    except SlurmError:
        # An unexpected accounting failure leaves live queue data usable.
        wait_history = {}
        wait_history_coverage = None
        wait_history_available = False
    # One summary entry per visible name: the union of live pending
    # groups, historical wait groups, and the utilization groups (so a
    # zero-pending GPU type still renders). squeue and sacct degrade
    # independently: pending fields are integers when squeue answered
    # (0 = genuinely empty for this type); wait statistics come from the
    # historical wait join, with zero samples and null percentiles when
    # that type has no valid started-job waits. The cluster-wide unique
    # totals live separately in ``totals`` — per-type rows overlap
    # (flexible jobs), so they must never be summed.
    merged = {}
    for name in (({g["name"] for g in groups}
                  | set(queue) | set(wait_history))
                 - {WAIT_TOTAL_KEY}):
        if queue_available:
            entry = {"exclusive_jobs": 0, "flexible_jobs": 0,
                     "exclusive_gpus": 0, "flexible_gpus": 0,
                     "eligible_jobs": 0, "eligible_gpus": 0}
            if name in queue:
                entry.update(queue[name])
                # eligible_* is a per-row invariant (exclusive +
                # flexible), computed here so every rendering surface
                # sees it filled.
                entry["eligible_jobs"] = (entry["exclusive_jobs"]
                                          + entry["flexible_jobs"])
                entry["eligible_gpus"] = (entry["exclusive_gpus"]
                                          + entry["flexible_gpus"])
        else:
            # squeue is down: pending demand is unknown, never a
            # fabricated 0 — but the wait history stays independently
            # valid below.
            entry = {"exclusive_jobs": None, "flexible_jobs": None,
                     "exclusive_gpus": None, "flexible_gpus": None,
                     "eligible_jobs": None, "eligible_gpus": None}
        if name in wait_history:
            entry.update(wait_history[name])
        elif wait_history_available:
            entry.update(wait_empty())
        else:
            entry.update({
                "wait_p50_s": None, "wait_p90_s": None,
                "wait_avg_s": None, "wait_samples": None,
                "wait_per_gpu_hour_p50": None,
            })
        merged[name] = entry
    return {
        "queue": merged,
        "totals": totals,
        "queue_available": queue_available,
        "waiting_jobs": waiting_jobs,
        "wait_history_available": wait_history_available,
        "wait_history_coverage": wait_history_coverage,
    }


@router.get("/api/partitions/queue/progress")
def api_partition_queue_progress(since_hours: float = Query(24, gt=0, le=168),
                                 running_only: bool = Query(False)):
    """Batched accounting progress for the queue's current-window fetch.

    The browser polls this while the queue loader is in flight; the
    accounting fetch records its daily-batch state into ``progress_store``
    (single-flighted through the same TTL cache as the accounting result).
    """
    key = cache.completed_progress_key(since_hours, running_only)
    return progress_store.get(key, None)


@router.get("/api/partitions/vram", response_model=VramResponse)
def api_part_vram(since_hours: float = Query(24, gt=0, le=168),
                  running_only: bool = Query(False),
                  partition: str = "",
                  weight: str = Query("alloc", pattern="^(alloc|eff)$")):
    node_types = gpu_groups.build_node_index(
        deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes))
    records, total, start, now, step = vram_job_records(
        since_hours, running_only, partition, node_types, weight)
    return {
        "window": window(start, now),
        "step": step,
        "total": total,
        "jobs": records,
    }
