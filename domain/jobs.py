"""Job-window aggregation: fetching and ranking jobs over a time window.

The core Prometheus fetch every Jobs-tab-shaped view builds on (the
Jobs tab itself, the Users tab's aggregation, and the VRAM
distribution chart all call ``fetch_job_window``), plus the
efficiency histogram used by the Jobs tab's chart.
"""

from collections import defaultdict

import cache
import deps
from domain.common import job_window, series_values, step_for_range
from promql import label_eq, selector


def fetch_job_window(since_hours, include_vram=True, user=None):
    """Fetch job-level utilization (and optionally vram) series for a window.

    Returns (jobs, start, now, step) where jobs is a list of dicts aggregated
    from Prometheus over the window (no sacct enrichment yet). When ``user``
    is given, the utilization query is scoped to that Slurm user so the whole
    window is never pulled for a single-user request.

    The two Prometheus range queries are cached under SEPARATE identities
    (cache.job_utilization_key / cache.job_vram_key): every job-list-shaped
    view — Jobs, Users, and the VRAM chart's records — shares ONE utilization
    fetch per window, and a utilization-only caller (include_vram=False)
    neither pays for nor blocks on the VRAM query.
    """
    start, now = job_window(since_hours)
    step = step_for_range(now - start)
    sel = selector(label_eq("user", user)) if user else ""

    def fetch_utilization():
        util = deps.get_prom().query_range(
            "max by (slurmjobid, instance, job, user, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        return util, start, now, step

    # Cache the series TOGETHER WITH the window it was fetched for: a
    # cache hit must report the samples' own window envelope, never a
    # freshly recomputed one that drifts past the cached data (the
    # envelope is the UI's displayed range).
    util, start, now, step = deps.route_cache.get_or_set(
        cache.job_utilization_key(since_hours, user), 60, fetch_utilization)

    def fetch_vram():
        vram = deps.get_prom().query_range(
            "avg by (slurmjobid, instance, gpu) (slurm_job_memory_usage_gpu / "
            "slurm_job_memory_total_gpu * 100)",
            start, now, step,
        )
        return vram, start, now, step

    vram = []
    if include_vram:
        # The VRAM series query carries NO user selector (per-job peaks,
        # not per-user), so its cache identity is user-independent: a
        # user-scoped Jobs request shares the same VRAM fetch as the
        # global window instead of duplicating it. The cached entry
        # carries the window it was fetched for; a hit whose envelope
        # differs from the resolved utilization window (e.g. the
        # utilization entry expired and re-fetched while the VRAM entry
        # survived, or vice versa) is REFETCHED so vram_avg never spans
        # a different interval than util — the response aggregates the
        # two series into one window and must not mix bounds.
        vram_entry = deps.route_cache.get_or_set(
            cache.job_vram_key(since_hours), 60, fetch_vram)
        if vram_entry[1:] == (start, now, step):
            vram = vram_entry[0]
        else:
            vram, start, now, step = fetch_vram()
            deps.route_cache.set(
                cache.job_vram_key(since_hours), 60,
                (vram, start, now, step))
    return _aggregate_job_window(util, vram, start, now, step)


def _aggregate_job_window(util, vram, start, now, step):
    """Aggregate cached utilization (+ optional VRAM) series into the
    job dicts every job-list-shaped view consumes."""
    vram_by_job = defaultdict(list)
    for s in vram:
        m = s["metric"]
        for ts, v in series_values(s):
            vram_by_job[m["slurmjobid"]].append(v)

    jobs = {}
    for s in util:
        m = s["metric"]
        jid = m["slurmjobid"]
        values = series_values(s)
        if not values:
            continue
        total = sum(v for _, v in values)
        job = jobs.setdefault(jid, {
            "jobid": jid,
            "user": m.get("user", ""),
            "partition": m.get("job", ""),
            "gpu_type": m.get("gpu_type", ""),
            "nodes": set(),
            "eff_sum": 0.0,
            "eff_samples": 0,
            "eff_hours": 0.0,
            "max_util": 0.0,
        })
        job["nodes"].add(m.get("instance", ""))
        job["eff_sum"] += total
        job["eff_samples"] += len(values)
        job["eff_hours"] += total * step / 3600.0 / 100.0
        job["max_util"] = max(job["max_util"], max(v for _, v in values))

    out = []
    for jid, job in jobs.items():
        vv = vram_by_job.get(jid)
        mean_util = (round(job["eff_sum"] / job["eff_samples"], 2)
                     if job["eff_samples"] else 0.0)
        out.append({
            "jobid": jid,
            "user": job["user"],
            "partition": job["partition"],
            "gpu_type": job["gpu_type"],
            "nodes": sorted(n for n in job["nodes"] if n),
            "mean_util": mean_util,
            "max_util": round(job["max_util"], 2),
            "gpu_hours_eff": round(job["eff_hours"], 2),
            "vram_avg": round(sum(vv) / len(vv), 1) if vv else None,
            # Internal aggregands used only by api_users to calculate the
            # true sample-weighted utilization across a user's jobs.
            "_util_sum": job["eff_sum"],
            "_util_samples": job["eff_samples"],
        })
    out.sort(key=lambda j: j["gpu_hours_eff"], reverse=True)
    return out, start, now, step


def efficiency_histogram(jobs, bin_width=10):
    """GPU-hours by mean-utilization bucket, all buckets zero-filled.

    Bins each job by ``mean_util`` ("efficiency" elsewhere in this API) into
    ``bin_width``-wide buckets from 0 to 100, summing ``gpu_hours_eff`` per
    bucket. Every bucket is always present in the result, in order, even
    when no job falls in it — a bucket a caller silently omits reads as "no
    capacity wasted here", identical to a bucket that legitimately has none,
    when it actually means "no bar for this position at all". A job's
    ``mean_util`` is clamped into ``[0, 100)`` before bucketing so an
    out-of-range measurement still lands in the nearest boundary bucket
    rather than dropping out of the total.
    """
    n_buckets = 100 // bin_width
    totals = [0.0] * n_buckets
    for job in jobs:
        idx = int(min(max(job["mean_util"], 0), 100 - 1e-9) // bin_width)
        totals[idx] += job.get("gpu_hours_eff") or 0
    return [
        {"bucket_start": i * bin_width, "bucket_end": (i + 1) * bin_width,
         "gpu_hours": round(totals[i], 2)}
        for i in range(n_buckets)
    ]
