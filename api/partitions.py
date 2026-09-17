"""Routes: GET /api/partitions, GET /api/partitions/vram."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
from api.schemas import PartitionsResponse, VramResponse
from domain.common import window
from domain.partitions import (
    gpu_capacity,
    node_current,
    partition_window,
    pending_queue_status,
    started_wait_summary,
)
from domain.vram import vram_job_records
from slurm import SlurmError

router = APIRouter()


def _queue_snapshot(now, partition_types):
    """Pending-job rows, per-GPU-type summary, and reachability.

    A failed or missing squeue is an explicit ``available=False`` state,
    never an empty queue: an operator must be able to tell "nothing
    pending" from "the queue is unreadable". The raw squeue rows are
    cached for 30 s; ``wait_s`` is computed afterwards from ``now`` so a
    displayed waiting age is current at response time, not at cache-fill
    time. ``partition_types`` (the partition -> GPU-types map from the
    same cached scontrol node snapshot every other group in this route
    uses) drives the pending rows' type eligibility — no extra scontrol
    call.
    """
    try:
        jobs = deps.route_cache.get_or_set(
            cache.queue_pending_key(), 30, deps.queue_pending)
    except SlurmError:
        return {}, [], False
    summary, waiting_jobs = pending_queue_status(jobs, now, partition_types)
    return summary, waiting_jobs, True


@router.get("/api/partitions", response_model=PartitionsResponse)
def api_partitions(since_hours: float = Query(24, gt=0, le=168),
                   running_only: bool = Query(False)):
    nodes = deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes)
    node_types = gpu_groups.build_node_index(nodes)
    partition_types = gpu_groups.partition_gpu_types(nodes)
    groups, trend, instances, occupancy, job_groups, start, now, step = \
        partition_window(since_hours, running_only, node_gpu_types=node_types)
    _, _, allocs_by_node, allocs_by_group = node_current(node_types)
    gpu_capacity(groups, instances, nodes, allocs_by_group)
    for g in groups:
        avg_alloc = occupancy.get(g["name"])
        total = g.get("gpus_total") or 0
        if avg_alloc is not None and total > 0:
            g["mean_occupancy"] = round(min(100.0, avg_alloc / total * 100.0), 1)
        else:
            g["mean_occupancy"] = None

    # Current queue and historical wait degrade independently: a dead
    # squeue must not hide a valid sacct history and vice versa, and
    # neither failure may turn the whole partitions response into an
    # error or masquerade as a zero.
    queue, waiting_jobs, queue_available = _queue_snapshot(now, partition_types)
    try:
        wait_history = started_wait_summary(job_groups, start, now)
        wait_history_available = True
    except SlurmError:
        wait_history = {}
        wait_history_available = False

    # One summary entry per visible name: the union of utilization rows,
    # live pending groups, and historical wait groups, plus __total__.
    # A partition with zero pending jobs still renders. started_jobs/
    # avg_wait_s come from the historical wait join; gpus=None only where
    # pending demand is genuinely unknowable (N/A node counts).
    merged = {}
    for name in ({g["name"] for g in groups}
                 | set(queue) | set(wait_history)):
        entry = {"jobs": 0, "gpus": 0, "gpus_min": 0,
                 "started_jobs": 0, "avg_wait_s": None}
        if name in queue:
            entry.update(queue[name])
        if name in wait_history:
            entry.update(wait_history[name])
        merged[name] = entry
    merged.setdefault("__total__",
                      {"jobs": 0, "gpus": 0, "gpus_min": 0,
                       "started_jobs": 0, "avg_wait_s": None})
    return {
        "window": window(start, now),
        "step": step,
        "partitions": groups,
        "trend": trend,
        "queue": merged,
        "queue_available": queue_available,
        "waiting_jobs": waiting_jobs,
        "wait_history_available": wait_history_available,
    }


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
