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
    partition_wait_stats,
    partition_window,
    pending_gpu_queue,
)
from domain.vram import vram_job_records

router = APIRouter()


@router.get("/api/partitions", response_model=PartitionsResponse)
def api_partitions(since_hours: float = Query(24, gt=0, le=168),
                   running_only: bool = Query(False)):
    nodes = deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes)
    node_types = gpu_groups.build_node_index(nodes)
    (groups, trend, instances, occupancy, observed_jobs,
     start, now, step) = partition_window(
        since_hours, running_only, node_gpu_types=node_types)
    _, _, allocs_by_node, allocs_by_group = node_current(node_types)
    gpu_capacity(groups, instances, nodes, allocs_by_group)
    for g in groups:
        avg_alloc = occupancy.get(g["name"])
        total = g.get("gpus_total") or 0
        if avg_alloc is not None and total > 0:
            g["mean_occupancy"] = round(min(100.0, avg_alloc / total * 100.0), 1)
        else:
            g["mean_occupancy"] = None
    wait_stats = partition_wait_stats(observed_jobs)
    for g in groups:
        stats = wait_stats.get(g["name"]) or {}
        g["average_wait_seconds"] = stats.get("average_wait_seconds")
        g["wait_sample_count"] = stats.get("wait_sample_count", 0)
        g["wait_candidate_count"] = stats.get("wait_candidate_count", 0)
    active_jobs = deps.route_cache.get_or_set(
        cache.scontrol_jobs_key(), 30, deps.show_jobs)
    queue = pending_gpu_queue(active_jobs, node_types, now)
    return {
        "window": window(start, now),
        "step": step,
        "partitions": groups,
        "trend": trend,
        "queue": queue,
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
