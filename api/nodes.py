"""Routes: GET /api/nodes, GET /api/nodes/{name}."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
from api.schemas import NodeDetailResponse, NodesResponse
from domain.common import series_payload, step_for_range, window
from domain.partitions import node_current, node_job_start
from prom import PrometheusError
from promql import label_eq, selector

router = APIRouter()


@router.get("/api/nodes", response_model=NodesResponse)
def api_nodes(gpu_only: bool = True, refresh: bool = Query(False)):
    if refresh:
        # Forced refresh bypasses both the dashboard's 30-second
        # scontrol cache and the Prometheus client's response cache
        # (60 s range / 20 s instant) instead of redrawing cached data.
        deps.get_prom().clear_cache()
        deps.route_cache.invalidate(
            cache.scontrol_nodes_key(), cache.node_current_key())
    nodes = deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes)
    if gpu_only:
        nodes = [n for n in nodes if n["gpus"]]
    try:
        cur, jobs_by_node, _, _ = node_current()
    except PrometheusError:
        cur, jobs_by_node = {}, {}
    for n in nodes:
        n["gpu_group"] = gpu_groups.node_gpu_group(n)
        c = cur.get(n["name"], {})
        n["current_util"] = c.get("util")
        n["current_vram"] = c.get("vram")
        # scontrol's own AllocTRES (parsed into gpus_alloc by
        # parse_scontrol_nodes), not the live Prometheus series count: a
        # node's monitoring exporter going silent must not make a fully
        # allocated node read as idle.
        n["gpus_alloc"] = min(n.get("gpus_alloc", 0), n["gpus"])
        n["active_jobs"] = sorted(
            jobs_by_node.get(n["name"], []),
            key=lambda j: j["util"], reverse=True,
        )[:10]
    return {
        "time": int(deps.now()),
        "count": len(nodes),
        "nodes": nodes,
    }


@router.get("/api/nodes/{name}", response_model=NodeDetailResponse)
def api_node_detail(
        name: str, view: str = Query("job_start", pattern="^(job_start|1|6|24)$")):
    now = int(deps.now())
    if view == "job_start":
        start = node_job_start(name, now)
    else:
        start = now - int(float(view) * 3600)
    step = step_for_range(now - start)
    prom = deps.get_prom()
    sel = selector(label_eq("instance", name))

    def fetch():
        util = prom.query_range(
            "max by (slurmjobid, gpu) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        vram = prom.query_range(
            # ``gpu`` is job-local (every 1-GPU job reports gpu="0"), so the
            # grouping must keep ``slurmjobid`` or co-located jobs merge.
            "avg by (slurmjobid, gpu) (slurm_job_memory_usage_gpu%s / "
            "slurm_job_memory_total_gpu%s * 100)" % (sel, sel),
            start, now, step,
        )
        return util, vram

    util, vram = deps.route_cache.get_or_set(
        cache.node_detail_key(name, view, start), 30, fetch)
    return {
        "node": name,
        "view": view,
        "window": window(start, now),
        "step": step,
        "series": {
            "utilization": series_payload(util),
            "vram": series_payload(vram),
        },
    }
