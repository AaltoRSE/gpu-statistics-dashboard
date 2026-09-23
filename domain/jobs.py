"""Job-window aggregation: the per-job view of the shared window series.

The per-GPU raw series is fetched once per pinned window by
``sources.gpu_util``; this module turns the derived per-job series (plus
optionally the VRAM % series) into the job dicts every Jobs-tab-shaped
view — the Jobs tab itself, the Users tab's aggregation, and the VRAM
distribution chart — consumes, plus the efficiency histogram used by the
Jobs tab's chart.
"""

from domain.common import series_values


def job_aggregates(q1_series, step, vram_series=()):
    """Aggregate per-job/per-instance utilization series into job dicts.

    ``q1_series`` is the ``max by (slurmjobid, instance, job, user,
    gpu_type)`` shape derived from the per-GPU raw window series (see
    domain.views.job_view); ``vram_series`` is the optional Q2 VRAM %
    series whose per-job mean fills ``vram_avg``. The aggregation itself
    is the one ``fetch_job_window`` always ran — values are merged
    across a job's instances into per-job sums, and the sample-weighted
    mean, max, and GPU-hour estimate come off those sums.

    Returns the list sorted by ``gpu_hours_eff`` descending, as before.
    """
    vram_by_job = {}
    for s in vram_series:
        m = s["metric"]
        for ts, v in series_values(s):
            vram_by_job.setdefault(m["slurmjobid"], []).append(v)

    jobs = {}
    for s in q1_series:
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
    return out


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
