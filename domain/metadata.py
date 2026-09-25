"""Array-parent resolution: merging sacct/scontrol rows onto job dicts.

A Prometheus job ID may be a bare Slurm array parent, which has no
single sacct/scontrol row of its own — only one row per physical task.
This module merges those task rows into the single metadata dict a
job listing needs, from either the active-controller snapshot
(scontrol) or the historical record (sacct), with the same merge
logic either way.
"""

import cache
import deps
import sources
from slurm import SlurmError, expand_node_list


def active_jobs():
    """The 30 s-cached scontrol snapshot, or {} when scontrol is down."""
    try:
        return deps.route_cache.get_or_set(
            cache.scontrol_jobs_key(), 30, deps.show_jobs)
    except SlurmError:
        return {}


def _jobid_sort_key(jobid):
    """Deterministic numeric key tolerating ``parent_task``-suffixed IDs."""
    base, sep, task = jobid.partition("_")

    def _num(value):
        try:
            return int(value)
        except ValueError:
            return 0

    return (_num(base), 1 if sep else 0, _num(task) if sep else 0)


def merge_job_rows(matches):
    """Merge physical task rows of one job into a single metadata dict.

    Rows are sorted by the suffix-tolerant numeric job-ID key; name/user/
    account/partition come from the first row, RUNNING wins the state,
    the earliest start wins, ``node_list`` becomes the expanded sorted
    union, and GPU/CPU counts and allocation seconds are summed. Used by
    both the active (scontrol) and historical (sacct) resolvers.
    """
    matches.sort(key=lambda r: _jobid_sort_key(r["jobid"]))
    base = dict(matches[0])
    if any(r.get("state") == "RUNNING" for r in matches):
        base["state"] = "RUNNING"
    starts = sorted(r.get("start") or "" for r in matches if r.get("start"))
    if starts:
        base["start"] = starts[0]
    base["node_list"] = ",".join(sorted(
        {n for r in matches for n in expand_node_list(r.get("node_list"))}))
    base["gpus"] = sum(r.get("gpus") or 0 for r in matches)
    base["ncpus"] = sum(r.get("ncpus") or 0 for r in matches)
    base["allocation_seconds"] = sum(
        (r.get("gpus") or 0) * (r.get("elapsed_s") or 0) for r in matches)
    return base


def resolve_scontrol_metadata(jobid, observed_nodes, metadata):
    """Resolve an active job from a ``scontrol show job`` snapshot.

    ``jobid`` may be a bare Slurm array parent: every physical record whose
    ``JobId`` or ``ArrayJobId`` equals it and whose node list intersects the
    observed instances is merged, so a parent spread over several nodes
    keeps name, state, start, and summed GPU allocation instead of being
    left blank. Matching requires the node intersection even for exact
    ``JobId`` hits, so an unrelated parent batch row is never absorbed.
    Returns None when no record matches.
    """
    metadata = metadata or {}
    observed = {n for n in observed_nodes if n}
    if not observed:
        return None
    matches = [
        r for r in metadata.values()
        if (r["jobid"] == jobid or r.get("array_jobid") == jobid)
        and observed & expand_node_list(r.get("node_list"))
    ]
    return merge_job_rows(matches) if matches else None


def resolve_sacct_metadata(jobid, observed_nodes, rows):
    """Resolve the sacct rows for a Prometheus job ID.

    ``rows`` is the ID's enriched sacct rows (from the shared dump index
    or the per-ID row cache): exact IDs — by either the ``jobid`` or the
    raw ``jobid_raw`` spelling — match directly, without a node check.
    A bare Slurm array parent (e.g. ``19975109``) has no exact row: the
    dump holds only its task rows (``19975109_0`` …), so every task whose
    node list intersects the observed instances is merged into one row
    with the same aggregation as the active scontrol path — including
    historical arrays whose multiple matching tasks no longer exist in
    the controller.
    """
    rows = rows or []
    for row in rows:
        if row.get("jobid") == jobid or row.get("jobid_raw") == jobid:
            return row
    observed = {n for n in observed_nodes if n}
    if not observed:
        return None
    prefix = jobid + "_"
    matches = [task for task in rows
               if task.get("jobid", "").startswith(prefix)
               and observed & expand_node_list(task.get("node_list"))]
    return merge_job_rows(matches) if matches else None


def apply_metadata(job, row):
    """Copy one resolved metadata row onto a job dict (no key deletion)."""
    for key in ("name", "state", "start", "end", "node_list", "account"):
        if row.get(key):
            job[key] = row[key]
    if row.get("gpus"):
        job["gpus"] = row["gpus"]
        if row.get("gpu_type"):
            job["gpu_type"] = row["gpu_type"]
    if row.get("ncpus"):
        job["ncpus"] = row["ncpus"]
    # Merged scontrol rows carry allocation_seconds; sacct rows (and the
    # single-record case) derive the same value from elapsed x GPUs.
    seconds = row.get("allocation_seconds") or \
        (row.get("elapsed_s") or 0) * (row.get("gpus") or 0)
    if seconds and row.get("gpus"):
        alloc = seconds / 3600.0
        util = job.get("mean_util") or 0.0
        job["gpu_hours_alloc"] = round(alloc, 2)
        job["gpu_hours_eff"] = round(alloc * util / 100.0, 2)


def enrich(jobs):
    """Resolve metadata for a job list from one parallel source gather.

    Both reads go through sources (plan §1): the jobs' per-ID sacct rows
    (the row cache batches every ID into one fetch) and the active-job
    scontrol snapshot run concurrently, and the snapshot failing must not
    take the listing down — an active-job miss (or a failed call) falls
    back to the sacct rows.
    """
    ids = [j["jobid"] for j in jobs]
    if not ids:
        return
    rows_by_id, active = sources.gather(
        lambda: sources.sacct_rows(ids),
        active_jobs,
    )
    for job in jobs:
        row = (resolve_scontrol_metadata(job["jobid"], job["nodes"], active)
               or resolve_sacct_metadata(job["jobid"], job["nodes"],
                                         rows_by_id.get(job["jobid"])))
        if not row:
            continue
        apply_metadata(job, row)
