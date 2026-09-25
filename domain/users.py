"""Per-user aggregation over the shared window job view.

/api/users and the Groups tab both need the same shape: the window's
jobs rolled up per user, keeping the raw sample-weighted aggregands so a
further roll-up (a group's mean utilization) can re-weight across all of
a group's jobs instead of averaging per-user means. One function, one
definition — the Groups tab's per-user rows must equal the Users tab's.
"""


def aggregate_users(jobs_view, live):
    """The per-user rows over one window's job view.

    ``jobs_view`` is the memoized job-side view (domain.views.job_views);
    ``live`` is the running-job ID set from the live snapshot. Rows keep
    the internal ``_util_sum``/``_util_samples`` aggregands the response
    schema strips: a group's mean utilization must be
    sum(samples-weighted sums) / sum(samples) over all member jobs, and
    its observed GPU-hours derive from the sample counts.

    ``util_gpu_hours`` is the utilization-weighted GPU-hours (mean util
    x series hours) — the same definition the Jobs tab's effective
    GPU-hours use before the allocation factor; users are ordered by it
    (descending), ties broken by name.
    """
    agg = {}
    for j in jobs_view:
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
            # Internal aggregands for the Groups tab's re-weighting; the
            # response schema strips them from /api/users.
            "_util_sum": a["util_sum"],
            "_util_samples": a["util_samples"],
            "_vram_sum": a["vram_sum"],
            "_vram_n": a["vram_n"],
        }
        for u, a in agg.items()
    ]
    users.sort(key=lambda r: (-r["util_gpu_hours"], r["user"]))
    return users
