"""Per-job VRAM records for the Partitions tab's VRAM distribution chart."""

from collections import defaultdict

import gpu_groups
import sources
from domain.metadata import resolve_sacct_metadata


def _dump_index(records):
    """``{jobid: row}`` plus ``{jobid_raw: row}`` over the sacct dump.

    Prometheus ``slurmjobid`` labels key on the raw numeric ID, which for
    an array task is not derivable from the ``jobid`` spelling — both
    spellings index the same row.
    """
    index = {}
    for row in records:
        for key in (row.get("jobid", ""), row.get("jobid_raw", "")):
            if key:
                index.setdefault(key, row)
    return index


def vram_job_records(jobs, raw_vram, node_gpu_types, weight,
                     window_records, live=None, partition=""):
    """Per-job VRAM records for the utilization-filtered distribution chart.

    Every input is a source the route already fetched (plan §1/§2) — this
    function fetches nothing except per-ID row-cache fallbacks for jobs
    the shared dump cannot enrich: ``jobs`` is the window's job view
    (domain.views.job_views), ``raw_vram`` the unscoped per-GPU
    peak-VRAM-GB series (sources.vram_gb), ``window_records`` the shared
    sacct window dump ``(records, coverage, start, end)`` from
    sources.sacct_window, and ``live`` the running-only filter (the live
    snapshot's job-ID set; None keeps every job).

    Each record carries the job's canonical GPU group (the Slurm
    partition, MIG GRES profiles split out), its time-weighted mean
    utilization, its average per-GPU peak VRAM (GB), and its allocated
    GPU-hours from sacct. Binning and the utilization range filter happen
    client-side so the slider can rebin without refetching. A non-empty
    ``partition`` keeps only jobs of that group, so the candidate
    ``total`` applies to the selected group.

    Returns (records, total, enriched_frac, failed_batches) where
    ``total`` equals ``len(records)``, ``enriched_frac`` is the fraction
    of returned records whose allocated GPU-hours the enrichment
    resolved, and ``failed_batches`` counts the dump's failed day chunks
    (their records' gpu_hours stay null).
    """
    dump_records, coverage = window_records[0], window_records[1]
    # Per-GPU peak VRAM (GB) over the window; a 0 sample means the GPU was
    # never reported with memory and cannot be a peak.
    peaks = defaultdict(list)
    for s in raw_vram:
        jid = s["metric"].get("slurmjobid", "")
        vals = [v for _, v in s["values"] if v > 0]
        if jid and vals:
            peaks[jid].append(max(vals))

    # ``jobs`` is memoized shared state (job_views) — never mutated;
    # the group rides on the record instead.
    records = []
    nodes_by_job = {}
    for j in jobs:
        group = gpu_groups.job_gpu_group(j, node_gpu_types)
        if live is not None and j["jobid"] not in live:
            continue
        if partition and group != partition:
            continue
        pk = peaks.get(j["jobid"])
        if not pk:
            continue
        nodes_by_job[j["jobid"]] = j.get("nodes") or []
        records.append({
            "jobid": j["jobid"],
            "user": j["user"],
            "partition": group,
            "gpu_type": j["gpu_type"],
            "mean_util": j["mean_util"],
            "vram_gb": round(sum(pk) / len(pk), 1),
            "gpu_hours": None,
            "gpu_hours_eff": j.get("gpu_hours_eff") or 0.0,
        })
    total = len(records)
    enriched_frac = 0.0
    failed_batches = coverage.get("failed_batches", 0)
    if records:
        by_id = _dump_index(dump_records)
        # Array parents have no exact row in the dump (only their task
        # rows, spelled parent_task); those go to the per-ID row cache in
        # ONE batched call, then resolve through the same historical
        # merge the job listings use.
        misses = sorted({r["jobid"] for r in records if r["jobid"] not in by_id})
        fallback = sources.sacct_rows(misses) if misses else {}
        enriched = 0
        for r in records:
            row = by_id.get(r["jobid"])
            if row is None:
                rows = fallback.get(r["jobid"]) or []
                row = resolve_sacct_metadata(
                    r["jobid"], nodes_by_job.get(r["jobid"], []), rows)
            # Key presence, not truthiness: a valid row with elapsed 0
            # (Slurm reports 00:00:00 for a just-started job) resolved
            # fine and must count as coverage, emitting 0.0 GPU-hours.
            # But elapsed data must be PRESENT: a row without it is an
            # unresolved record, not a zero-hour one.
            if row and row.get("gpus") and row.get("elapsed_s") is not None:
                r["gpu_hours"] = round(row["gpus"] * row["elapsed_s"] / 3600.0, 2)
                enriched += 1
        # The client discloses enrichment coverage: gpu_hours nulls in the
        # distribution mean sacct could not be queried for that record.
        enriched_frac = enriched / len(records)
    wkey = "gpu_hours" if weight == "alloc" else "gpu_hours_eff"
    records.sort(key=lambda r: (r.get(wkey) or 0.0), reverse=True)
    return records, total, enriched_frac, failed_batches
