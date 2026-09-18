"""Selected-user utilization history for the Users tab.

One per-GPU Prometheus range query scoped to the user derives both chart
views: the overall utilization line (mean across every observed GPU series
at each timestamp) and the per-job lines (mean across that job's observed
GPU series). Raw results are TTL-cached; reshaping per view is cheap.
"""

from collections import defaultdict

import cache
import deps
from domain.common import job_window, series_values, step_for_range, window
from promql import label_eq, selector


def fetch_user_activity(username, since_hours):
    """Per-GPU utilization series for one Slurm user over the window.

    Returns ``(start, now, step, per_series)`` where ``per_series`` is a
    list of ``(slurmjobid, [(ts, value), ...])`` — the raw means the
    response builder reshapes into aggregate and per-job views.
    """
    start, now = job_window(since_hours)
    step = step_for_range(now - start)
    sel = selector(label_eq("user", username))

    def fetch():
        return deps.get_prom().query_range(
            "max by (slurmjobid, instance, gpu) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )

    key = cache.user_activity_key(username, since_hours)
    series = deps.route_cache.get_or_set(key, 60, fetch)

    by_job = defaultdict(list)
    for s in series:
        m = s["metric"]
        for ts, v in series_values(s):
            by_job[m["slurmjobid"]].append((ts, v))
    return start, now, step, sorted(by_job.items())


def build_user_activity(username, since_hours):
    """Response-ready aggregate and per-job series for one user.

    ``aggregate`` averages across every observed GPU series of the user at
    each timestamp; each job's values average across that job's GPUs.
    Timestamps ascend; jobs sort by jobid. No utilization data yields empty
    lists, not an error.
    """
    start, now, step, per_job = fetch_user_activity(username, since_hours)

    # ts -> (sum, n) across every GPU series of the user.
    all_samples = defaultdict(lambda: [0.0, 0])
    jobs_out = []
    for jid, samples in per_job:
        by_ts = defaultdict(lambda: [0.0, 0])
        for ts, v in samples:
            bucket = by_ts[ts]
            bucket[0] += v
            bucket[1] += 1
            all_ts = all_samples[ts]
            all_ts[0] += v
            all_ts[1] += 1
        jobs_out.append({
            "jobid": jid,
            "values": [(ts, round(s / n, 2)) for ts, (s, n) in sorted(
                by_ts.items())],
        })
    jobs_out.sort(key=lambda j: j["jobid"])
    aggregate = [(ts, round(s / n, 2)) for ts, (s, n) in sorted(
        all_samples.items())]
    return {
        "user": username,
        "window": window(start, now),
        "step": step,
        "aggregate": aggregate,
        "jobs": jobs_out,
    }
