"""Routes: GET /api/jobs, GET /api/jobs/{jobid}."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import JobDetailResponse, JobsResponse
from domain.common import series_payload, series_values, window
from domain.jobs import efficiency_histogram
from domain.metadata import (
    active_jobs,
    apply_metadata,
    enrich,
    resolve_sacct_metadata,
    resolve_scontrol_metadata,
)
from domain.views import job_views
from prom import PrometheusError
from promql import label_eq, selector

router = APIRouter()


def _pinned(since_hours):
    """The shared pinned window plus its scontrol node-type index."""
    pinned = sources.pinned_window(since_hours)
    nodes = deps.route_cache.get_or_set(
        cache.scontrol_nodes_key(), 30, deps.show_nodes)
    return pinned, gpu_groups.build_node_index(nodes)


@router.get("/api/jobs", response_model=JobsResponse)
def api_jobs(
    since_hours: float = Query(24, gt=0, le=720),
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
        # redrawing the same data; the next request re-pins the window
        # and re-reads the live snapshot.
        deps.get_prom().clear_cache()
        deps.route_cache.invalidate(cache.pinned_window_key(since_hours),
                                    cache.snapshot_key())
    live = None
    if running_only:
        # Live snapshot first: with no running GPU jobs we must not issue
        # the broad window range query at all.
        try:
            live = sources.live_snapshot()["live_ids"]
        except PrometheusError:
            live = set()
        if not live:
            start, now, _ = sources.pinned_window(since_hours)
            return {"window": window(start, now), "count": 0,
                    "total_candidates": 0, "partitions": [], "jobs": [],
                    "efficiency_histogram": efficiency_histogram([])}
    pinned, node_types = _pinned(since_hours)
    start, now, step = pinned
    # Both window sources fetch concurrently (plan §2): the Jobs tab's
    # two range queries share one pinned window with every other tab,
    # and neither may wait on the other.
    util, vram = sources.gather(
        lambda: sources.gpu_util(pinned),
        lambda: sources.vram_pct(pinned),
    )
    jobs_view = job_views(pinned, util, node_types, vram)
    # Copies: the view's job dicts are memoized shared state that
    # /api/users and the VRAM route read raw — enrichment (and the
    # gpu_group tag) below must never write back into them.
    jobs = [dict(j) for j in jobs_view]
    for j in jobs:
        j["gpu_group"] = gpu_groups.job_gpu_group(j, node_types)
    if live is not None:
        jobs = [j for j in jobs if j["jobid"] in live]
    if partition:
        jobs = [j for j in jobs if j["gpu_group"] == partition]
    if user:
        # The typed label is matched in process (casefold), not in the
        # Prometheus query: every tab reads one shared unfiltered fetch,
        # and a single-user request must not re-fetch the window.
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
def api_job_detail(jobid: str, since_hours: float = Query(24, gt=0, le=720)):
    start, now, step = sources.pinned_window(since_hours)

    def fetch():
        prom = deps.get_prom()
        sel = selector(label_eq("slurmjobid", jobid))
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
    rows_by_id, active = sources.gather(
        lambda: sources.sacct_rows([jobid]),
        active_jobs,
    )
    meta = (resolve_scontrol_metadata(jobid, observed, active)
            or resolve_sacct_metadata(jobid, observed,
                                      rows_by_id.get(jobid)))
    if meta:
        # Copy so the cached sacct row is not mutated; the human-readable
        # start/end strings are preserved as-is.
        meta = dict(meta)
    # Summary-row figures (PLAN-2): mean utilization is a plain time
    # average over every matched GPU series in this window; gpu_hours_eff
    # starts as the same Prometheus-only estimate job_view uses, then
    # apply_metadata below overwrites it with the allocation-based figure
    # (and sets gpu_hours_alloc) once metadata resolves — the same
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
