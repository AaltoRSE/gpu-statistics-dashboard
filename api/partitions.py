"""Routes: GET /api/partitions, GET /api/partitions/vram."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
from api.schemas import PartitionQueueResponse, PartitionsResponse, VramResponse
from domain.common import job_window, running_gpu_job_ids, step_for_range, window
from domain.partitions import (
    WAIT_TOTAL_KEY,
    aggregate_node_current,
    aggregate_partition_window,
    completed_wait_summary,
    gpu_capacity,
    node_current_series,
    partition_window,
    partition_window_series,
    pending_queue_status,
    progress_store,
    wait_empty,
)
from domain.vram import _finalize_vram_records, vram_raw_records
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
                   running_only: bool = Query(False),
                   refresh: bool = Query(False)):
    # Three independent sources, fetched concurrently: the scontrol node
    # snapshot, the partition-window Prometheus series, and the live
    # node-current Prometheus snapshot. Each is cached on its own key
    # (TtlCache single-flights concurrent misses); aggregation needs
    # node types, so it runs after the futures resolve.
    if refresh:
        # Forced refresh (the header's global button) bypasses EVERY
        # cache the response consumes: the core charts' window cache,
        # the scontrol node snapshot (GPU types/capacity), and the live
        # node-current snapshot (allocations). The pending queue keeps
        # its own accounting cadence — a forced charts refresh must not
        # wipe a wait-history fetch another viewer is watching progress
        # on. invalidate() also supersedes any in-flight pre-refresh
        # fetch for these keys, so this request cannot join and
        # re-publish a stale generation.
        deps.get_prom().clear_cache()
        deps.route_cache.invalidate(
            cache.partition_window_key(since_hours, running_only),
            cache.scontrol_nodes_key(),
            cache.node_current_key())
    with ThreadPoolExecutor(max_workers=3) as executor:
        nodes_future = executor.submit(
            deps.route_cache.get_or_set,
            cache.scontrol_nodes_key(), 30, deps.show_nodes)
        series_future = executor.submit(
            partition_window_series, since_hours, running_only)
        current_future = executor.submit(node_current_series)
        nodes = nodes_future.result()
        stats, util_sums, gpu_counts, start, now, step = series_future.result()
        inst_util, inst_vram, active, alloc = current_future.result()
    node_types = gpu_groups.build_node_index(nodes)
    groups, trend, instances, occupancy, job_groups = \
        aggregate_partition_window(stats, util_sums, gpu_counts,
                                   start, now, step,
                                   running_only=running_only,
                                   node_gpu_types=node_types)
    _, _, allocs_by_node, allocs_by_group = aggregate_node_current(
        inst_util, inst_vram, active, alloc, node_types)
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
        cache_key = cache.completed_jobs_key(since_hours)
        progress_key = cache.completed_progress_key(since_hours)

        def fetch():
            # Publish under both identities: the cache key (what this
            # request can inspect locally) and the stable parameter key
            # the browser polls. running_only is deliberately excluded —
            # the accounting cache joins same-window requests regardless
            # of the flag, so a follower's poll must find this fetch's
            # state.
            for key in (cache_key, progress_key):
                progress_store[key] = {"done": 0, "total": 0,
                                       "failed_batches": 0}

            def report(state):
                for key in (cache_key, progress_key):
                    progress_store[key] = state

            try:
                records, coverage = deps.completed_jobs(
                    start_iso, end_iso, report)
                # Cache the bounds the records were actually fetched for:
                # the TTL can outlive the request's epoch window, so a
                # later hit must filter against THESE bounds, not bounds
                # recomputed from a newer clock.
                return (records, coverage, start, now)
            finally:
                # Failed fetches clear too: a stale in-flight entry would
                # otherwise read as live progress on every later poll.
                for key in (cache_key, progress_key):
                    progress_store.pop(key, None)

        records, accounting_coverage, cached_start, cached_end = \
            deps.route_cache.get_or_set(cache_key, 300, fetch)
        wait_history, wait_history_coverage = completed_wait_summary(
            records, node_types, cached_start, cached_end,
            accounting_coverage)
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
    key = cache.completed_progress_key(since_hours)
    return progress_store.get(key, None)


@router.get("/api/partitions/vram", response_model=VramResponse)
def api_part_vram(since_hours: float = Query(24, gt=0, le=168),
                  running_only: bool = Query(False),
                  partition: str = "",
                  weight: str = Query("alloc", pattern="^(alloc|eff)$"),
                  refresh: bool = Query(False)):
    if refresh:
        # Forced refresh (the header's global button) bypasses the VRAM
        # peaks cache and the shared utilization window it builds on;
        # the node snapshot (grouping) is invalidated up front, and the
        # sacct enrichment entry below once the candidate IDs are
        # known — both are consumed by this response, so a forced
        # refresh that left them cached could return fresh Prometheus
        # records under stale grouping/allocation metadata. job_vram_key
        # is NOT invalidated here: the VRAM records route never consumes
        # that source (its peaks live under vram_key).
        deps.get_prom().clear_cache()
        deps.route_cache.invalidate(
            cache.vram_key(since_hours, running_only),
            cache.job_utilization_key(since_hours, None),
            cache.scontrol_nodes_key())
        forced_sacct = True
    else:
        forced_sacct = False
    live = None
    if running_only:
        live = running_gpu_job_ids()
        if not live:
            # No live series: empty is genuinely empty, not unknown.
            start, now = job_window(since_hours)
            return {
                "window": window(start, now),
                "step": step_for_range(now - start),
                "total": 0,
                "enriched_frac": 0.0,
                "failed_batches": 0,
                "jobs": [],
            }
    with ThreadPoolExecutor(max_workers=2) as executor:
        nodes_future = executor.submit(
            deps.route_cache.get_or_set,
            cache.scontrol_nodes_key(), 30, deps.show_nodes)
        raw_future = executor.submit(vram_raw_records, since_hours, live)
        nodes = nodes_future.result()
        records, start, now, step = raw_future.result()
    node_types = gpu_groups.build_node_index(nodes)
    # forced_sacct reaches the finalize so the enrichment entry for the
    # FINAL filtered/capped ID set (known only there) is invalidated
    # right before the get_or_set that consumes it.
    records, total, start, now, step, enriched_frac, failed_batches = \
        _finalize_vram_records(records, start, now, step, live,
                               partition, node_types, weight,
                               force_enrichment=forced_sacct)
    return {
        "window": window(start, now),
        "step": step,
        "total": total,
        "enriched_frac": enriched_frac,
        "failed_batches": failed_batches,
        "jobs": records,
    }
