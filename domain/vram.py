"""Per-job VRAM records for the Partitions tab's VRAM distribution chart."""

from collections import defaultdict

import cache
import deps
import gpu_groups
from domain.common import job_window, running_gpu_job_ids, series_values, step_for_range
from domain.jobs import fetch_job_window
from promql import label_in, selector


def vram_job_records(since_hours, running_only=False, partition="",
                      node_gpu_types=None, weight="alloc"):
    """Per-job VRAM records for the utilization-filtered distribution chart.

    Each record carries the job's canonical GPU group (the Slurm partition,
    MIG GRES profiles split out), its time-weighted mean utilization, its
    average per-GPU peak VRAM (GB), and its allocated GPU-hours from sacct.
    Binning and the utilization range filter happen client-side so the
    slider can rebin without refetching. A non-empty ``partition`` keeps
    only jobs of that group, so the candidate ``total`` and the enrichment
    cap apply to the selected group.
    Returns (records, total, start, now, step) where ``total`` counts
    candidates before the enrichment cap.
    """
    node_gpu_types = node_gpu_types or {}
    start, now = job_window(since_hours)
    step = step_for_range(now - start)
    live = None
    if running_only:
        live = running_gpu_job_ids()
        if not live:
            return [], 0, start, now, step
    jobs, start, now, step = fetch_job_window(since_hours, include_vram=False)
    for j in jobs:
        j["gpu_group"] = gpu_groups.job_gpu_group(j, node_gpu_types)
    if live is not None:
        jobs = [j for j in jobs if j["jobid"] in live]
    if partition:
        jobs = [j for j in jobs if j["gpu_group"] == partition]
    sel = "" if live is None else selector(label_in("slurmjobid", live))

    def fetch():
        return deps.get_prom().query_range(
            "max by (slurmjobid, instance, gpu) (slurm_job_memory_usage_gpu%s / "
            "1073741824)" % sel,
            start, now, step,
        )

    vram = deps.route_cache.get_or_set(
        cache.vram_key(since_hours, running_only), 60, fetch)
    # Per-GPU peak VRAM (GB) over the window; a 0 sample means the GPU was
    # never reported with memory and cannot be a peak.
    peaks = defaultdict(list)
    for s in vram:
        jid = s["metric"].get("slurmjobid", "")
        vals = [v for _, v in series_values(s) if v > 0]
        if jid and vals:
            peaks[jid].append(max(vals))
    records = []
    for j in jobs:
        pk = peaks.get(j["jobid"])
        if not pk:
            continue
        records.append({
            "jobid": j["jobid"],
            "user": j["user"],
            "partition": j["gpu_group"],
            "gpu_type": j["gpu_type"],
            "mean_util": j["mean_util"],
            "vram_gb": round(sum(pk) / len(pk), 1),
            "gpu_hours": None,
            "gpu_hours_eff": j.get("gpu_hours_eff") or 0.0,
        })
    # Pre-cap selection stays effective-GPU-hours driven: it is the only
    # allocation-derived figure available before the sacct enrichment below,
    # and it correlates with real allocation hours. The chosen weight then
    # orders the capped, enriched set for the client.
    records.sort(key=lambda r: r["gpu_hours_eff"], reverse=True)
    total = len(records)
    records = records[:deps.VRAM_RECORD_CAP]
    ids = sorted({r["jobid"] for r in records})
    if ids:
        meta = deps.route_cache.get_or_set(
            cache.sacct_key(ids), 300, lambda: deps.sacct_jobs(ids))
        for r in records:
            row = meta.get(r["jobid"]) or {}
            if row.get("gpus") and row.get("elapsed_s"):
                r["gpu_hours"] = round(row["gpus"] * row["elapsed_s"] / 3600.0, 2)
    wkey = "gpu_hours" if weight == "alloc" else "gpu_hours_eff"
    records.sort(key=lambda r: (r.get(wkey) or 0.0), reverse=True)
    return records, total, start, now, step
