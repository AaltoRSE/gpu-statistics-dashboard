"""Routes: GET /api/partitions, /queue, /vram and their progress polls."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import PartitionQueueResponse, PartitionsResponse, VramResponse
from domain.common import window
from domain.partitions import (
    WAIT_TOTAL_KEY,
    completed_wait_summary,
    gpu_capacity,
    partition_view,
    pending_queue_status,
    wait_empty,
)
from domain.views import job_views, partition_views
from domain.vram import vram_job_records
from prom import PrometheusError
from slurm import SlurmError

router = APIRouter()


def _pinned(since_hours):
    """The shared pinned window plus the scontrol snapshot/index pair."""
    pinned = sources.pinned_window(since_hours)
    nodes = deps.route_cache.get_or_set(
        cache.scontrol_nodes_key(), 30, deps.show_nodes)
    return pinned, nodes, gpu_groups.build_node_index(nodes)


def _live_ids():
    """The live snapshot's job-ID set, {} when Prometheus is down."""
    try:
        return sources.live_snapshot()["live_ids"]
    except PrometheusError:
        return set()


def _snapshot_or_none():
    """The live snapshot for capacity attribution, None on Prometheus failure.

    Capacity rows must still render when the instant queries fail — a
    dead exporter must not blank the partitions table — so the alloc
    join falls back to zero allocated instead of raising.
    """
    try:
        return sources.live_snapshot()
    except PrometheusError:
        return None


@router.get("/api/partitions", response_model=PartitionsResponse)
def api_partitions(since_hours: float = Query(24, gt=0, le=720),
                   running_only: bool = Query(False)):
    pinned, nodes, node_types = _pinned(since_hours)
    start, now, step = pinned
    if running_only:
        # Live snapshot first: with no running GPU jobs we must not issue
        # the broad window range query at all.
        live = _live_ids()
        if not live:
            return {"window": window(start, now), "step": step,
                    "partitions": [], "trend": {}}
    else:
        live = None
    if live is not None:
        # Running-only keeps only the live jobs' series — the in-process
        # replacement for the ``slurmjobid=~…`` matcher — and must not
        # claim configured-but-unmeasured capacity the filtered window
        # never observed. The snapshot is already in hand (the live-ID
        # check above); only the shared raw fetch is needed.
        raw = [s for s in sources.gpu_util(pinned)
               if s["metric"].get("slurmjobid", "") in live]
        groups, trend, instances, occupancy, _ = partition_view(
            raw, node_types, include_configured=False)
        snap = _snapshot_or_none()
    else:
        # The partition view needs only the raw utilization series (plan
        # §2) — no VRAM query — and the live snapshot degrades to None
        # when Prometheus is down.
        raw, snap = sources.gather(
            lambda: sources.gpu_util(pinned),
            _snapshot_or_none,
        )
        groups, trend, instances, occupancy, _ = partition_views(
            pinned, raw, node_types)
    allocs = snap["allocs_by_group"] if snap else {}
    gpu_capacity(groups, instances, nodes, allocs)
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
def api_partition_queue(since_hours: float = Query(24, gt=0, le=720),
                        running_only: bool = Query(False)):
    """Live pending-job queue and historical waits, independent of the
    utilization endpoint.

    squeue (live snapshot) and sacct (wait history) are typically slower
    than the Prometheus-backed /api/partitions, so the Partitions tab
    fetches both endpoints concurrently and renders whichever arrives
    first under its own panel. The utilization groups come from the
    shared window view — one fetch, shared with the concurrent core
    request through the pinned-window cache.

    Current queue and historical wait degrade independently: a dead
    squeue must not hide a valid sacct history and vice versa, and
    neither failure may turn the whole response into an error or
    masquerade as a zero.
    """
    pinned, nodes, node_types = _pinned(since_hours)
    start, now, _ = pinned
    if running_only:
        live = _live_ids()
        if not live:
            groups = []
        else:
            raw = [s for s in sources.gpu_util(pinned)
                   if s["metric"].get("slurmjobid", "") in live]
            groups = partition_view(raw, node_types,
                                    include_configured=False)[0]
    else:
        # Full-window view: partition rows only — no VRAM query (plan §2).
        groups = partition_views(
            pinned, sources.gpu_util(pinned), node_types)[0]
    partition_types = gpu_groups.partition_gpu_types(nodes)
    queue, totals, waiting_jobs, queue_available = _queue_snapshot(
        now, partition_types)
    try:
        # One window-wide sacct dump, shared with the VRAM enrichment
        # (plan §3); its chunked fetch publishes progress under the
        # collapsed progress key the browser polls. Records are filtered
        # against the bounds the dump was actually fetched for — the TTL
        # can outlive this request's epoch window, so a later hit must
        # not recompute bounds from a newer clock.
        records, coverage, fetched_start, fetched_end = sources.sacct_window(
            since_hours, progress_key=cache.vram_progress_key(since_hours))
        wait_history, wait_history_coverage = completed_wait_summary(
            records, node_types, fetched_start, fetched_end, coverage)
        wait_history_available = bool(coverage["successful_batches"])
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
                "wait_per_gpu_hour_weighted": None,
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


def _queue_snapshot(now, partition_types):
    """Pending-job rows, per-GPU-type summary, and reachability.

    A failed or missing squeue is an explicit ``available=False`` state,
    never an empty queue: an operator must be able to tell "nothing
    pending" from "the queue is unreadable". squeue runs on every queue
    request — the queue is a live scheduler snapshot and a time-window
    change must refetch it rather than redisplay a cached view (the
    Prometheus side stays cached through the shared window sources).
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


@router.get("/api/partitions/queue/progress")
def api_partition_queue_progress(since_hours: float = Query(24, gt=0, le=720),
                                 running_only: bool = Query(False)):
    """Batched accounting progress for the window's shared sacct dump.

    The browser polls this while the queue loader is in flight; the
    dump's chunked fetch records its daily-batch state into
    ``progress_store`` (single-flighted through the same TTL cache as
    the dump itself). Only the cache-miss leader that runs the fetch
    touches the key, so a poll never observes a finished fetch's stale
    state.
    """
    return cache.progress_store.get(
        cache.vram_progress_key(since_hours))


@router.get("/api/partitions/vram/progress")
def api_part_vram_progress(since_hours: float = Query(24, gt=0, le=720),
                           running_only: bool = Query(False),
                           partition: str = ""):
    """Batched accounting progress for the VRAM route's sacct enrichment.

    The browser polls this while the VRAM loader is in flight. The
    enrichment reads the same window-wide dump as the queue's wait
    history (plan §3), so this poll and the queue's progress poll return
    the same batch state under the one collapsed key. No in-flight fetch
    reads as JSON null.
    """
    return cache.progress_store.get(
        cache.vram_progress_key(since_hours))


@router.get("/api/partitions/vram", response_model=VramResponse)
def api_part_vram(since_hours: float = Query(24, gt=0, le=720),
                  running_only: bool = Query(False),
                  partition: str = "",
                  weight: str = Query("alloc", pattern="^(alloc|eff)$")):
    pinned, _nodes, node_types = _pinned(since_hours)
    live = None
    if running_only:
        # Live snapshot first: with no running GPU jobs we must not issue
        # the broad window range queries at all.
        live = _live_ids()
        if not live:
            return {"window": window(pinned[0], pinned[1]),
                    "step": pinned[2], "total": 0, "enriched_frac": 0.0,
                    "failed_batches": 0, "jobs": []}
    raw, vram_gb, window_records = sources.gather(
        lambda: sources.gpu_util(pinned),
        lambda: sources.vram_gb(pinned),
        lambda: sources.sacct_window(
            since_hours,
            progress_key=cache.vram_progress_key(since_hours)),
    )
    # The VRAM chart consumes the job rows' utilization aggregates only
    # (mean util, effective GPU-hours); the per-GPU VRAM comes from the
    # ``vram_gb`` series above, so no VRAM % query runs here (plan §2).
    jobs_view = job_views(pinned, raw, node_types)
    records, total, enriched_frac, failed_batches = vram_job_records(
        jobs_view, vram_gb, node_types, weight, window_records,
        live=live, partition=partition)
    return {
        "window": window(pinned[0], pinned[1]),
        "step": pinned[2],
        "total": total,
        "enriched_frac": enriched_frac,
        "failed_batches": failed_batches,
        "jobs": records,
    }
