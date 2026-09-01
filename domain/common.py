"""Low-level helpers shared across every domain and route module.

Prometheus response shaping (series/window envelopes), the query-step
heuristic, and the "which jobs are live right now" check are used by
jobs, partitions, vram, and every route's response body alike, so they
have no single domain owner — they live here instead.
"""

import deps


def step_for_range(seconds):
    if seconds <= 48 * 3600:
        return 120
    if seconds <= 3 * 86400:
        return 300
    if seconds <= 7 * 86400:
        return 600
    return 900


def series_values(series):
    """[(ts, float)] for a prom range result item, skipping stale NaN-like 0s."""
    out = []
    for ts, val in series["values"]:
        try:
            out.append((float(ts), float(val)))
        except ValueError:
            continue
    return out


def series_payload(series_list):
    """[{"metric": ..., "values": [(ts, float), ...]}, ...] for a prom
    range result list; the response shape shared by every detail series."""
    return [{"metric": s["metric"], "values": series_values(s)}
            for s in series_list]


def job_window(since_hours, now=None):
    now = now or int(deps.now())
    return now - int(since_hours * 3600), now


def window(start, now):
    """The ``window`` envelope shared by every route's response body."""
    return {"start": start, "end": now}


def running_gpu_job_ids():
    """Job IDs with a live Prometheus GPU-utilization series.

    A live series is the shared definition of "running" for both the Jobs
    and Partitions running-only controls; it avoids an unbounded sacct scan.
    """
    series = deps.get_prom().query_instant(
        "count by (slurmjobid) (slurm_job_utilization_gpu)")
    return {s["metric"]["slurmjobid"] for s in series
            if s["metric"].get("slurmjobid")}
