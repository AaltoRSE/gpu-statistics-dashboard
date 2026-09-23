"""Route: GET /api/users."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import UsersResponse
from domain.common import window
from domain.views import window_views

router = APIRouter()


@router.get("/api/users", response_model=UsersResponse)
def api_users(since_hours: float = Query(24, gt=0, le=720)):
    """Per-user GPU-activity aggregation over the window.

    Built from the same shared window sources the Jobs tab uses (plan
    §1/§2): the per-GPU utilization and VRAM % range queries, the
    scontrol node snapshot, and the running-only live snapshot all gather
    in parallel, then the memoized window view supplies the job rows —
    no sacct, so the list stays cheap.
    ``util_gpu_hours`` is the utilization-weighted GPU-hours
    (mean util x series hours), i.e. the same definition the Jobs tab's
    effective GPU-hours use before the allocation factor. Users are
    ordered by it (descending), ties broken by name.
    """
    pinned = sources.pinned_window(since_hours)
    util, vram, live_snap, nodes = sources.gather(
        lambda: sources.gpu_util(pinned),
        lambda: sources.vram_pct(pinned),
        lambda: sources.live_snapshot(),
        lambda: deps.route_cache.get_or_set(
            cache.scontrol_nodes_key(), 30, deps.show_nodes),
    )
    node_types = gpu_groups.build_node_index(nodes)
    views = window_views(pinned, util, vram, node_types)
    live = live_snap["live_ids"]

    agg = {}
    for j in views["jobs"]:
        u = j["user"]
        if not u:
            continue
        a = agg.setdefault(u, {
            "jobs": 0, "running_jobs": 0, "util_sum": 0.0,
            "util_samples": 0, "util_gpu_hours": 0.0,
            "vram_sum": 0.0, "vram_n": 0, "gpu_types": set(),
        })
        a["jobs"] += 1
        if j["jobid"] in live:
            a["running_jobs"] += 1
        a["util_sum"] += j.get("_util_sum", 0.0)
        a["util_samples"] += j.get("_util_samples", 0)
        a["util_gpu_hours"] += j.get("gpu_hours_eff") or 0.0
        if j.get("gpu_type"):
            a["gpu_types"].add(j["gpu_type"])
        v = j.get("vram_avg")
        if v is not None:
            a["vram_sum"] += v
            a["vram_n"] += 1
    users = [
        {
            "user": u,
            "jobs": a["jobs"],
            "running_jobs": a["running_jobs"],
            # Sample-weighted mean utilization across the user's GPU series;
            # effective GPU-hours already include utilization and cannot be
            # used as this weight without squaring it.
            "mean_util": round(a["util_sum"] / a["util_samples"], 2)
            if a["util_samples"] else 0.0,
            "util_gpu_hours": round(a["util_gpu_hours"], 2),
            "vram_avg": round(a["vram_sum"] / a["vram_n"], 1)
            if a["vram_n"] else None,
            "gpu_types": sorted(a["gpu_types"]),
        }
        for u, a in agg.items()
    ]
    users.sort(key=lambda r: (-r["util_gpu_hours"], r["user"]))
    return {
        "window": window(pinned[0], pinned[1]),
        "count": len(users),
        "users": users,
    }
