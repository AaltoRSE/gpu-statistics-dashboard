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
from slurm import SlurmError, expand_node_list


def fetch_job_window(since_hours, include_vram=True, user=None):
    """Fetch job-level utilization (and optionally vram) series for a window.

    Returns (jobs, start, end) where jobs is a list of dicts aggregated from
    Prometheus over the window (no sacct enrichment yet). When ``user`` is
    given, the utilization query is scoped to that Slurm user so the whole
    window is never pulled for a single-user request.
    """
    start, now = job_window(since_hours)
    step = step_for_range(now - start)
    sel = selector(label_eq("user", user)) if user else ""

    def fetch():
        util = deps.get_prom().query_range(
            "max by (slurmjobid, instance, job, user, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        vram = []
        if include_vram:
            vram = deps.get_prom().query_range(
                "avg by (slurmjobid, instance, gpu) (slurm_job_memory_usage_gpu / "
                "slurm_job_memory_total_gpu * 100)",
                start, now, step,
            )
        return util, vram, start, now, step

    key = cache.job_window_key(since_hours, include_vram, user)
    util, vram, start, now, step = deps.route_cache.get_or_set(key, 60, fetch)

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
            "monitored": True,
            # Internal aggregands used only by api_users to calculate the
            # true sample-weighted utilization across a user's jobs.
            "_util_sum": job["eff_sum"],
            "_util_samples": job["eff_samples"],
        })
    out.sort(key=lambda j: j["gpu_hours_eff"], reverse=True)
    return out, start, now, step


def unmonitored_running_jobs(known_ids, user=None):
    """Running GPU jobs the controller knows but Prometheus never reported.

    Job discovery is otherwise Prometheus-only (bare ``sacct`` is
    ACL-restricted), so a node whose exporter stops publishing takes every
    job on it out of the dashboard entirely — the node reads idle while
    Slurm has it fully allocated. ``scontrol show job`` is not tied to any
    node's exporter, so it still sees that work.

    Returns job dicts shaped like ``fetch_job_window``'s, but with every
    utilization-derived figure left as ``None`` rather than 0: these jobs
    have no measurements at all, and a 0 would read as "ran at 0%
    efficiency", polluting the efficiency histogram and the
    lowest-efficiency chart with jobs that were never measured. They carry
    ``monitored: False`` so callers can label them and keep them out of
    utilization aggregates.

    A job already known to Prometheus under its Slurm array *parent* ID is
    not re-added under its physical task ID.
    """
    try:
        active = deps.route_cache.get_or_set(
            cache.scontrol_jobs_key(), 30, deps.show_jobs)
    except SlurmError:
        return []
    out = []
    for row in active.values():
        if row.get("state") != "RUNNING" or not row.get("gpus"):
            continue
        if row["jobid"] in known_ids or (row.get("array_jobid") or "") in known_ids:
            continue
        if user and (row.get("user") or "").casefold() != user.casefold():
            continue
        # scontrol reports every partition a job may run in; the first is
        # the one the Partitions tab keys on.
        partition = (row.get("partition") or "").split(",")[0].strip()
        out.append({
            "jobid": row["jobid"],
            "user": row.get("user") or "",
            "partition": partition,
            "gpu_type": row.get("gpu_type") or "",
            "nodes": sorted(expand_node_list(row.get("node_list"))),
            "mean_util": None,
            "max_util": None,
            "gpu_hours_eff": None,
            "vram_avg": None,
            "monitored": False,
            "_util_sum": 0.0,
            "_util_samples": 0,
        })
    return out


def sort_by_effective_hours(jobs):
    """Rank by effective GPU-hours, unmeasured jobs last.

    ``gpu_hours_eff`` is ``None`` for a job Prometheus never saw, which
    cannot be compared against a float; those sort to the end rather than
    to the bottom of the ranking as if they were zero.
    """
    jobs.sort(key=lambda j: (j.get("gpu_hours_eff") is None,
                             -(j.get("gpu_hours_eff") or 0.0)))
    return jobs


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
        # A job Prometheus never measured has no efficiency to bucket; it
        # must not land in the 0-10% bar as if it had run idle.
        if job.get("mean_util") is None:
            continue
        idx = int(min(max(job["mean_util"], 0), 100 - 1e-9) // bin_width)
        totals[idx] += job.get("gpu_hours_eff") or 0
    return [
        {"bucket_start": i * bin_width, "bucket_end": (i + 1) * bin_width,
         "gpu_hours": round(totals[i], 2)}
        for i in range(n_buckets)
    ]
