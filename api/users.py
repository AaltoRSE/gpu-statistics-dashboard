"""Routes: GET /api/users, GET /api/users/categories."""

from fastapi import APIRouter, Query

from api.schemas import UserCategoriesResponse, UsersResponse
from domain.common import job_window, running_gpu_job_ids, window
from domain.jobs import fetch_job_window
from domain.partitions import completed_job_window
from domain.users import classify_user, completed_category_summary, require_group
from slurm import SlurmError

router = APIRouter()


@router.get("/api/users", response_model=UsersResponse)
def api_users(since_hours: float = Query(24, gt=0, le=720)):
    """Per-user GPU-activity aggregation over the window.

    Built from the same job window the Jobs tab uses (utilization and VRAM
    range queries, user label preserved, TTL-cached with that tab) plus the
    running-only instant liveness check — no sacct, so the list stays cheap.
    ``util_gpu_hours`` is the utilization-weighted GPU-hours
    (mean util x series hours), i.e. the same definition the Jobs tab's
    effective GPU-hours use before the allocation factor. Users are
    ordered by it (descending), ties broken by name. Each row also carries
    its Ellis/Non-Ellis category: the classification reads the route
    cache's per-user NSS answers (populated by the categories endpoint and
    shared with it), so the list itself never waits on the directory
    service.
    """
    # The list carries a category column, so a missing "ellis" group is
    # a hard 503 here too — even with zero users in the window (the
    # per-user classify call would never touch NSS).
    require_group()
    jobs, start, now, _ = fetch_job_window(since_hours)
    live = running_gpu_job_ids()
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
            "user_category": classify_user(u),
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
        "window": window(start, now),
        "count": len(users),
        "users": users,
    }


@router.get("/api/users/categories",
            response_model=UserCategoriesResponse)
def api_user_categories(since_hours: float = Query(24, gt=0, le=720)):
    """Ellis versus Non-Ellis totals for the window's completed GPU jobs.

    Accounting-backed and deliberately independent of the per-user list:
    the Prometheus job window comes from the shared ``fetch_job_window``
    cache and the sacct records from the shared ``completed_job_window``
    single-flight (the same entry ``/api/partitions/queue`` uses), so one
    bounded scan serves every route. A slow or failed accounting query
    errors here — never into the user table's path. The summary filters
    against the bounds the records were actually fetched for, not bounds
    recomputed from a newer clock.
    """
    # The window's Prometheus job dicts carry the internal _util_sum/
    # _util_samples aggregands the utilization column joins on. The
    # accounting fetch's own cached epoch bounds (not a fresh clock read)
    # define the qualifying cohort.
    # Same preflight as /api/users: with an empty cohort no per-user
    # classification runs, yet the Ellis/Non-Ellis split is still the
    # endpoint's contract — an unresolvable group is unknowable, not
    # "everyone Non-Ellis".
    require_group()
    jobs, _, _, _ = fetch_job_window(since_hours)
    start, now = job_window(since_hours)
    records, accounting_coverage, window_start, window_end = \
        completed_job_window(since_hours, start, now)
    if not accounting_coverage.get("successful_batches"):
        raise SlurmError(
            "completed-jobs accounting returned no successful batches")
    categories, coverage = completed_category_summary(
        records, jobs, window_start, window_end, accounting_coverage)
    return {
        "window": window(window_start, window_end),
        "categories": categories,
        "coverage": coverage,
    }
