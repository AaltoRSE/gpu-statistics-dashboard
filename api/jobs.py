"""Routes: GET /api/jobs, GET /api/jobs/{jobid}."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
from api.schemas import JobDetailResponse, JobsResponse
from domain.common import (
    job_window,
    running_gpu_job_ids,
    series_payload,
    series_values,
    step_for_range,
    window,
)
from domain.jobs import efficiency_histogram, fetch_job_window
from domain.metadata import (
    apply_metadata,
    enrich,
    resolve_sacct_metadata,
    resolve_scontrol_metadata,
)
from promql import label_eq, selector
from slurm import SlurmError

router = APIRouter()


@router.get("/api/jobs", response_model=JobsResponse)
def api_jobs(
    since_hours: float = Query(24, gt=0, le=168),
    partition: str = "",
    user: str = "",
    search: str = "",
    limit: int = Query(100, ge=1, le=1000),
    running_only: bool = Query(False),
    refresh: bool = Query(False),
):
    user = user.strip()
    if refresh:
        # Forced refresh bypasses both the app's 60-second window cache
        # and the Prometheus client's 20/60 s response cache instead of
        # redrawing the same data; it also forces a fresh live-ID query.
        deps.get_prom().clear_cache()
        deps.route_cache.invalidate(
            cache.job_window_key(since_hours, True, user or None))
    if running_only:
        # Live-ID check first: with no running GPU jobs we must not issue
        # the broad window range query at all.
        live = running_gpu_job_ids()
        if not live:
            start, now = job_window(since_hours)
            return {"window": window(start, now), "count": 0,
                    "total_candidates": 0, "partitions": [], "jobs": [],
                    "efficiency_histogram": efficiency_histogram([])}
    # The user filter is pushed into the Prometheus query (server-side),
    # not applied after the fact: a single-user request must not pull and
    # scan the whole window for everyone else's jobs.
    jobs, start, now, _ = fetch_job_window(since_hours, user=user or None)
    node_types = gpu_groups.build_node_index(
        deps.route_cache.get_or_set(cache.scontrol_nodes_key(), 30, deps.show_nodes))
    for j in jobs:
        j["gpu_group"] = gpu_groups.job_gpu_group(j, node_types)
    if running_only:
        jobs = [j for j in jobs if j["jobid"] in live]
    if partition:
        jobs = [j for j in jobs if j["gpu_group"] == partition]
    if user:
        # PromQL's exact user matcher is case-sensitive; retain the typed
        # label case for the query, then accept capitalization drift here.
        jobs = [j for j in jobs if j["user"].casefold() == user.casefold()]
    # Histogram over the full filtered candidate set (before the table
    # limit and sacct enrichment): the chart must not be biased by the
    # bounded table rows.
    histogram = efficiency_histogram(jobs)
    # The pre-limit candidate count: how many jobs matched partition/user/
    # running_only before the sacct-enrichment cap below. A search that
    # matches nothing can then tell the difference between "no such job in
    # the window" and "outside the top `limit` by GPU-hours" instead of
    # just looking empty either way.
    total_candidates = len(jobs)
    # Bound the sacct enrichment cost before it; name search therefore only
    # covers the top-``limit`` jobs by effective GPU hours. Running-only
    # ignores the limit: every live GPU job is returned (the UI disables
    # the limit box while that mode is active).
    if not running_only:
        jobs = jobs[:limit]
    enrich(jobs)
    if search:
        needle = search.lower()
        jobs = [
            j for j in jobs
            if needle in j["jobid"] or needle in (j.get("name") or "").lower()
        ]
        # Name search matches sacct names, so the chart must show the same
        # bounded searched rows.
        histogram = efficiency_histogram(jobs)
    partitions = sorted({j["gpu_group"] or j["partition"]
                         for j in jobs
                         if j.get("gpu_group") or j.get("partition")})
    return {
        "window": window(start, now),
        "count": len(jobs),
        "total_candidates": total_candidates,
        "partitions": partitions,
        "jobs": jobs,
        "efficiency_histogram": histogram,
    }


@router.get("/api/jobs/{jobid}", response_model=JobDetailResponse)
def api_job_detail(jobid: str, since_hours: float = Query(24, gt=0, le=168)):
    start, now = job_window(since_hours)
    step = step_for_range(now - start)
    prom = deps.get_prom()

    sel = selector(label_eq("slurmjobid", jobid))

    def fetch():
        util = prom.query_range(
            "max by (slurmjobid, instance, gpu) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        vram = prom.query_range(
            "avg by (instance, gpu) (slurm_job_memory_usage_gpu%s / "
            "slurm_job_memory_total_gpu%s * 100)" % (sel, sel),
            start, now, step,
        )
        return util, vram

    util, vram = deps.route_cache.get_or_set(
        cache.job_detail_key(jobid, since_hours), 60, fetch)
    series = {
        "utilization": series_payload(util),
        "vram": series_payload(vram),
    }
    observed = sorted({s["metric"].get("instance", "") for s in util
                       if s["metric"].get("instance")})
    sacct_meta = deps.route_cache.get_or_set(
        cache.sacct_key([jobid]), 300, lambda: deps.sacct_jobs([jobid]))
    try:
        active = deps.route_cache.get_or_set(
            cache.scontrol_jobs_key(), 30, deps.show_jobs)
    except SlurmError:
        active = {}
    meta = (resolve_scontrol_metadata(jobid, observed, active)
            or resolve_sacct_metadata(jobid, observed, sacct_meta))
    if meta:
        # Copy so the cached sacct row is not mutated; the human-readable
        # start/end strings are preserved as-is.
        meta = dict(meta)
    # Summary-row figures (PLAN-2): mean utilization is a plain time
    # average over every matched GPU series in this window; gpu_hours_eff
    # starts as the same Prometheus-only estimate fetch_job_window uses,
    # then apply_metadata below overwrites it with the allocation-based
    # figure (and sets gpu_hours_alloc) once metadata resolves — the same
    # override the Jobs-list endpoint applies, reused here rather than
    # duplicated.
    all_values = [v for s in util for _, v in series_values(s)]
    mean_util = round(sum(all_values) / len(all_values), 2) if all_values else 0.0
    summary = {
        "mean_util": mean_util,
        "gpu_hours_eff": round(sum(all_values) * step / 3600.0 / 100.0, 2),
    }
    if meta:
        apply_metadata(summary, meta)
    return {"jobid": jobid, "window": window(start, now), "step": step,
            "metadata": meta, "series": series,
            "mean_util": summary["mean_util"],
            "gpu_hours_eff": summary["gpu_hours_eff"],
            "gpu_hours_alloc": summary.get("gpu_hours_alloc"),
            "elapsed_s": (meta or {}).get("elapsed_s") or None}
