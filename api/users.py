"""Route: GET /api/users."""

from fastapi import APIRouter, Query

from api.schemas import UsersResponse
from domain.common import running_gpu_job_ids, window
from domain.jobs import fetch_job_window, unmonitored_running_jobs

router = APIRouter()


@router.get("/api/users", response_model=UsersResponse)
def api_users(since_hours: float = Query(24, gt=0, le=168)):
    """Per-user GPU-activity aggregation over the window.

    Built from the same job window the Jobs tab uses (utilization and VRAM
    range queries, user label preserved, TTL-cached with that tab) plus the
    running-only instant liveness check — no sacct, so the list stays cheap.
    ``util_gpu_hours`` is the utilization-weighted GPU-hours
    (mean util x series hours), i.e. the same definition the Jobs tab's
    effective GPU-hours use before the allocation factor. Users are
    ordered by it (descending), ties broken by name.
    """
    jobs, start, now, _ = fetch_job_window(since_hours)
    live = running_gpu_job_ids()
    # A user whose only running work sits on a node with a silent exporter
    # would otherwise be absent from this list entirely. Those jobs carry
    # no utilization samples, so they raise the user's job counts without
    # touching the utilization means below.
    jobs = jobs + unmonitored_running_jobs({j["jobid"] for j in jobs})
    agg = {}
    for j in jobs:
        u = j["user"]
        if not u:
            continue
        a = agg.setdefault(u, {
            "jobs": 0, "running_jobs": 0, "util_sum": 0.0,
            "util_samples": 0, "util_gpu_hours": 0.0,
            "vram_sum": 0.0, "vram_n": 0, "gpu_types": set(),
        })
        a["jobs"] += 1
        if j["jobid"] in live or not j.get("monitored", True):
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
            # used as this weight without squaring it. A user whose jobs all
            # ran on non-reporting nodes has no samples at all — null, not
            # 0.0, which would read as "measured, and idle".
            "mean_util": round(a["util_sum"] / a["util_samples"], 2)
            if a["util_samples"] else None,
            "util_gpu_hours": round(a["util_gpu_hours"], 2),
            "vram_avg": round(a["vram_sum"] / a["vram_n"], 1)
            if a["vram_n"] else None,
            "gpu_types": sorted(a["gpu_types"]),
        }
        for u, a in agg.items()
    ]
    users.sort(key=lambda r: (-r["util_gpu_hours"], r["user"]))
    return {
        "window": window(start, now),
        "count": len(users),
        "users": users,
    }
