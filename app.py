"""GPU efficiency admin dashboard — FastAPI app.

Data is collected on demand: each route queries only the Prometheus /
sacct / scontrol sources it needs, with short in-memory TTL caches.
"""

import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import ConfigError, load_config
from prom import PromClient, PrometheusError
from slurm import SlurmError, expand_node_list, sacct_jobs, show_jobs, show_nodes

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="GPU Efficiency Dashboard")

_prom = None
_cache = {}
_cache_lock = None


def get_prom():
    global _prom
    if _prom is None:
        try:
            cfg = load_config()
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        _prom = PromClient(
            cfg["api_base"], cfg["username"], cfg["password"], cfg["timeout"])
    return _prom


def _cached(key, ttl, fn):
    import threading

    global _cache_lock
    if _cache_lock is None:
        _cache_lock = threading.Lock()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    value = fn()
    with _cache_lock:
        if len(_cache) > 256:
            _cache.clear()
        _cache[key] = (time.monotonic() + ttl, value)
    return value


def _step_for_range(seconds):
    if seconds <= 48 * 3600:
        return 120
    if seconds <= 3 * 86400:
        return 300
    if seconds <= 7 * 86400:
        return 600
    return 900


def _series_values(series):
    """[(ts, float)] for a prom range result item, skipping stale NaN-like 0s."""
    out = []
    for ts, val in series["values"]:
        try:
            out.append((float(ts), float(val)))
        except ValueError:
            continue
    return out


def _job_window(since_hours, now=None):
    now = now or int(time.time())
    return now - int(since_hours * 3600), now


def _invalidate_cache(*keys):
    """Drop cache entries; used by the forced-refresh path."""
    import threading

    global _cache_lock
    if _cache_lock is None:
        _cache_lock = threading.Lock()
    with _cache_lock:
        for key in keys:
            _cache.pop(key, None)


def _running_gpu_job_ids():
    """Job IDs with a live Prometheus GPU-utilization series.

    A live series is the shared definition of "running" for both the Jobs
    and Partitions running-only controls; it avoids an unbounded sacct scan.
    """
    series = get_prom().query_instant(
        "count by (slurmjobid) (slurm_job_utilization_gpu)")
    return {s["metric"]["slurmjobid"] for s in series
            if s["metric"].get("slurmjobid")}


def _jobid_matcher(job_ids):
    """PromQL selector fragment matching exactly one of a set of job IDs."""
    return 'slurmjobid=~"^(?:' + "|".join(re.escape(j) for j in sorted(job_ids)) + ')$"'


def _sacct_epoch(value):
    """sacct start/end string to epoch seconds; None when missing/invalid."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None

_MIG_GRES_RE = re.compile(r"^(?:[A-Za-z0-9]+_)?\d+[gm]\.\d+[gm]b?$",
                           re.IGNORECASE)


def _is_mig_gres(name):
    """True when a GRES name is a MIG profile (``h200_3g.71gb``, or the bare
    Prometheus profile ``3g.70gb``) rather than a whole GPU."""
    return bool(_MIG_GRES_RE.match(name or ""))


def _gpu_group_name(metric, node_gpu_types, aliases=None):
    """Canonical partition-view group name for one metric series.

    MIG GPUs must never merge into their node's whole-GPU pool: a series
    observed on a node whose scontrol GRES is a MIG profile belongs to that
    profile (``h200_3g.71gb``), not the bare family. A profile that cannot
    be resolved to a node falls back to ``<job>_<gpu_type>`` so it stays
    separated from whole GPUs; everything else keeps the Prometheus ``job``
    label (the Slurm partition).
    """
    job = metric.get("job", "") or ""
    gtype = metric.get("gpu_type", "") or ""
    if aliases is not None:
        alias = aliases.get((job, gtype))
        if alias:
            return alias
    inst = metric.get("instance", "")
    if inst:
        ntype = node_gpu_types.get(inst)
        if ntype and _is_mig_gres(ntype):
            return ntype
    if _is_mig_gres(gtype):
        return job + "_" + gtype if job else gtype
    return job or "unknown"


def _job_gpu_group(job, node_gpu_types):
    """Canonical partition-view group for a job from its observed nodes."""
    mig = set()
    for name in job.get("nodes") or []:
        ntype = node_gpu_types.get(name)
        if ntype and _is_mig_gres(ntype):
            mig.add(ntype)
    if not mig and _is_mig_gres(job.get("gpu_type") or ""):
        mig.add(job["gpu_type"])
    if not mig:
        return job.get("partition") or "unknown"
    if len(mig) == 1:
        return mig.pop()
    return (job.get("partition") or "unknown") + "_" + ",".join(sorted(mig))


def _node_gpu_group(node):
    """Canonical partition-view group for one node.

    MIG-gres nodes (``gpu_type=h200_3g.71gb``) always belong to their
    profile group, even when their ``partitions`` field also lists the
    whole-GPU partition; everything else uses the first non-empty
    partition, which is the group the Partitions tab keys on. Nodes
    without a GPU type (CPU-only) resolve to ``""``.
    """
    if not (node.get("gpu_type") or "").strip():
        return ""
    if _is_mig_gres(node["gpu_type"]):
        return node["gpu_type"]
    for p in (node.get("partitions") or "").split(","):
        p = p.strip()
        if p:
            return p
    return ""


def _fetch_job_window(since_hours, include_vram=True, user=None):
    """Fetch job-level utilization (and optionally vram) series for a window.

    Returns (jobs, start, end) where jobs is a list of dicts aggregated from
    Prometheus over the window (no sacct enrichment yet). When ``user`` is
    given, the utilization query is scoped to that Slurm user so the whole
    window is never pulled for a single-user request.
    """
    start, now = _job_window(since_hours)
    step = _step_for_range(now - start)
    sel = ""
    if user:
        escaped = user.replace("\\", "\\\\").replace('"', '\\"')
        sel = '{user="%s"}' % escaped

    def fetch():
        util = get_prom().query_range(
            "max by (slurmjobid, instance, job, user, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        vram = []
        if include_vram:
            vram = get_prom().query_range(
                "avg by (slurmjobid, instance, gpu) (slurm_job_memory_usage_gpu / "
                "slurm_job_memory_total_gpu * 100)",
                start, now, step,
            )
        return util, vram, start, now, step

    key = ("jobs", since_hours, include_vram, user)
    util, vram, start, now, step = _cached(key, 60, fetch)

    vram_by_job = defaultdict(list)
    for s in vram:
        m = s["metric"]
        for ts, v in _series_values(s):
            vram_by_job[m["slurmjobid"]].append(v)

    jobs = {}
    for s in util:
        m = s["metric"]
        jid = m["slurmjobid"]
        values = _series_values(s)
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
            # Average efficiency over the window; independent of job
            # duration and of sacct availability.
            "efficiency": round(mean_util, 1),
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


def _public_job(job):
    """Remove in-process aggregation fields from API job payloads."""
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _jobid_sort_key(jobid):
    """Deterministic numeric key tolerating ``parent_task``-suffixed IDs."""
    base, sep, task = jobid.partition("_")

    def _num(value):
        try:
            return int(value)
        except ValueError:
            return 0

    return (_num(base), 1 if sep else 0, _num(task) if sep else 0)


def _merge_job_rows(matches):
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


def _resolve_scontrol_metadata(jobid, observed_nodes, metadata):
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
    return _merge_job_rows(matches) if matches else None


def _resolve_sacct_metadata(jobid, observed_nodes, metadata):
    """Resolve the sacct rows for a Prometheus job ID.

    Exact IDs match directly. A bare Slurm array parent (e.g. ``19975109``)
    has no exact row: ``sacct -j`` returns only its task rows
    (``19975109_0`` …), so every task whose node list intersects the
    observed instances is merged into one row with the same aggregation
    as the active scontrol path — including historical arrays whose
    multiple matching tasks no longer exist in the controller.
    """
    metadata = metadata or {}
    row = metadata.get(jobid)
    if row:
        return row
    observed = {n for n in observed_nodes if n}
    if not observed:
        return None
    prefix = jobid + "_"
    matches = [task for key, task in metadata.items()
               if key.startswith(prefix)
               and observed & expand_node_list(task.get("node_list"))]
    return _merge_job_rows(matches) if matches else None


def _apply_metadata(job, row):
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


def _enrich(jobs, since_hours):
    ids = [j["jobid"] for j in jobs]
    if not ids:
        return
    # Explicit job IDs bound the sacct request, so no visible-window -S
    # date is passed: jobs that started before the chart window still get
    # their name, state, start, and GPU allocation.
    meta = _cached(("sacct", tuple(ids)), 300,
                   lambda: sacct_jobs(ids))
    # Active-job snapshot for array parents: scontrol only knows jobs the
    # controller still holds, so a miss (or a failed call) falls back to
    # the sacct rows above.
    try:
        active = _cached("scontrol_jobs", 30, show_jobs)
    except SlurmError:
        active = {}
    for job in jobs:
        row = (_resolve_scontrol_metadata(job["jobid"], job["nodes"], active)
               or _resolve_sacct_metadata(job["jobid"], job["nodes"], meta))
        if not row:
            continue
        _apply_metadata(job, row)


@app.exception_handler(PrometheusError)
def prom_error_handler(request, exc):
    return JSONResponse(content={"error": "prometheus_unreachable",
                                  "detail": str(exc)}, status_code=502)


@app.exception_handler(SlurmError)
def slurm_error_handler(request, exc):
    return JSONResponse(content={"error": "slurm_unreachable",
                                 "detail": str(exc)}, status_code=502)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "prometheus": get_prom().api_base,
        "time": datetime.now(timezone.utc).isoformat(),
    }


# Deep-link routes: the SPA is a single page, so every view path serves the
# same shell and the frontend (app.js) restores state from the URL path.
_VIEW_PATHS = ["/jobs", "/partitions", "/users", "/nodes"]


for _p in _VIEW_PATHS:
    app.add_api_route(
        _p,
        lambda: FileResponse(os.path.join(STATIC, "index.html")),
        include_in_schema=False,
    )


@app.get("/job/{jobid}")
def job_page(jobid: str):
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/node/{nodename}")
def node_page(nodename: str):
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/partition/{partition}")
def partition_page(partition: str):
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/user/{username}")
def user_page(username: str):
    return FileResponse(os.path.join(STATIC, "index.html"))


def _efficiency_extremes(jobs, count=30):
    """Top/bottom average-efficiency jobs with deterministic ties.

    ``efficiency_high`` is the highest-efficiency jobs, descending;
    ``efficiency_low`` is the lowest-efficiency jobs, ascending. Ties break by
    job ID so both lists are stable across calls.
    """
    ordered = sorted(jobs, key=lambda j: (j["efficiency"], j["jobid"]))
    low = ordered[:count]
    high = sorted(ordered[-count:], key=lambda j: (-j["efficiency"], j["jobid"]))
    return high, low


@app.get("/api/jobs")
def api_jobs(
    since_hours: float = Query(24, gt=0, le=168),
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
        # redrawing the same data; it also forces a fresh live-ID query.
        get_prom().clear_cache()
        _invalidate_cache(("jobs", since_hours, True, user or None))
    if running_only:
        # Live-ID check first: with no running GPU jobs we must not issue
        # the broad window range query at all.
        live = _running_gpu_job_ids()
        if not live:
            start, now = _job_window(since_hours)
            return {"window": {"start": start, "end": now}, "count": 0,
                    "partitions": [], "jobs": [],
                    "efficiency_high": [], "efficiency_low": []}
    # The user filter is pushed into the Prometheus query (server-side),
    # not applied after the fact: a single-user request must not pull and
    # scan the whole window for everyone else's jobs.
    jobs, start, now, _ = _fetch_job_window(since_hours, user=user or None)
    node_types = _gpu_type_by_node(_cached("scontrol_nodes", 30, show_nodes))
    for j in jobs:
        j["gpu_group"] = _job_gpu_group(j, node_types)
    if running_only:
        jobs = [j for j in jobs if j["jobid"] in live]
    if partition:
        jobs = [j for j in jobs if j["gpu_group"] == partition]
    if user:
        # PromQL's exact user matcher is case-sensitive; retain the typed
        # label case for the query, then accept capitalization drift here.
        jobs = [j for j in jobs if j["user"].casefold() == user.casefold()]
    # Efficiency extremes over the full filtered candidate set (before the
    # table limit and sacct enrichment): the high/low charts must not be
    # biased by job duration or the bounded table rows.
    high, low = _efficiency_extremes(jobs)
    # Bound the sacct enrichment cost before it; name search therefore only
    # covers the top-``limit`` jobs by effective GPU hours. Running-only
    # ignores the limit: every live GPU job is returned (the UI disables
    # the limit box while that mode is active).
    if not running_only:
        jobs = jobs[:limit]
    _enrich(jobs, since_hours)
    if search:
        needle = search.lower()
        jobs = [
            j for j in jobs
            if needle in j["jobid"] or needle in (j.get("name") or "").lower()
        ]
        # Name search matches sacct names, so the charts must show the same
        # bounded searched rows.
        high, low = _efficiency_extremes(jobs)
    partitions = sorted({j["gpu_group"] or j["partition"]
                         for j in jobs
                         if j.get("gpu_group") or j.get("partition")})
    return {
        "window": {"start": start, "end": now},
        "count": len(jobs),
        "partitions": partitions,
        "jobs": [_public_job(j) for j in jobs],
        "efficiency_high": [_public_job(j) for j in high],
        "efficiency_low": [_public_job(j) for j in low],
    }


@app.get("/api/users")
def api_users(since_hours: float = Query(24, gt=0, le=168)):
    """Per-user GPU-activity aggregation over the window.

    Built from the same job window the Jobs tab uses (utilization and VRAM
    range queries, user label preserved, TTL-cached with that tab) plus the
    running-only instant liveness check — no sacct, so the list stays cheap.
    ``util_gpu_hours`` is the utilization-weighted GPU-hours
    (mean util x series hours), i.e. the same definition the Jobs tab's
    effective GPU-hours use before the allocation factor. Users are
    ordered by it (descending), ties broken by name.
    """
    jobs, start, now, _ = _fetch_job_window(since_hours)
    live = _running_gpu_job_ids()
    agg = {}
    for j in jobs:
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
        }
        for u, a in agg.items()
    ]
    users.sort(key=lambda r: (-r["util_gpu_hours"], r["user"]))
    return {
        "window": {"start": start, "end": now},
        "count": len(users),
        "users": users,
    }


@app.get("/api/jobs/{jobid}")
def api_job_detail(jobid: str, since_hours: float = Query(24, gt=0, le=168)):
    start, now = _job_window(since_hours)
    step = _step_for_range(now - start)
    prom = get_prom()

    def fetch():
        util = prom.query_range(
            'max by (slurmjobid, instance, gpu) '
            '(slurm_job_utilization_gpu{slurmjobid="%s"})' % jobid,
            start, now, step,
        )
        vram = prom.query_range(
            'avg by (instance, gpu) (slurm_job_memory_usage_gpu{slurmjobid="%s"} / '
            'slurm_job_memory_total_gpu{slurmjobid="%s"} * 100)' % (jobid, jobid),
            start, now, step,
        )
        return util, vram

    util, vram = _cached(("jobdetail", jobid, since_hours), 60, fetch)
    series = {
        "utilization": [
            {"metric": s["metric"], "values": _series_values(s)} for s in util
        ],
        "vram": [
            {"metric": s["metric"], "values": _series_values(s)} for s in vram
        ],
    }
    observed = sorted({s["metric"].get("instance", "") for s in util
                       if s["metric"].get("instance")})
    sacct_meta = _cached(("sacct", (jobid,)), 300, lambda: sacct_jobs([jobid]))
    try:
        active = _cached("scontrol_jobs", 30, show_jobs)
    except SlurmError:
        active = {}
    meta = (_resolve_scontrol_metadata(jobid, observed, active)
            or _resolve_sacct_metadata(jobid, observed, sacct_meta))
    if meta:
        # Copy so the cached sacct row is not mutated; the human-readable
        # start/end strings are preserved as-is.
        meta = dict(meta)
    return {"jobid": jobid, "window": {"start": start, "end": now}, "step": step,
            "metadata": meta, "series": series}


def _gpu_type_by_node(nodes):
    """``{node name: scontrol gpu_type}`` for the partition analytics."""
    return {n["name"]: n.get("gpu_type") or "" for n in nodes}


def _partition_window(since_hours, running_only=False, now=None,
                      node_gpu_types=None):
    """Slurm-partition utilization window.

    Groups are keyed by the canonical GPU-group name (the Slurm partition,
    except MIG GPUs, which are keyed by the node's MIG GRES profile so a
    MIG node never counts against its whole-GPU pool). Summary data keeps
    the ``slurmjobid`` label (per-job/per-node max, so the job identity
    survives for running-only matching); trend data is a plain
    ``avg by (job, gpu_type)`` and has no job identity, so the matcher must
    be injected into the metric selector before the aggregation.
    """
    start, now = _job_window(since_hours, now)
    step = _step_for_range(now - start)
    sel = ""
    if running_only:
        live = _running_gpu_job_ids()
        if not live:
            return [], {}, {}, {}, start, now, step
        sel = "{" + _jobid_matcher(live) + "}"

    def fetch():
        stats = get_prom().query_range(
            "max by (slurmjobid, instance, job, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        trend = get_prom().query_range(
            "avg by (job, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        # Concurrent allocated GPUs per group, for the window-average
        # occupancy chart (same selector so running-only matches here too).
        occ = get_prom().query_range(
            "count by (job, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        return stats, trend, occ, start, now, step

    key = ("parts", since_hours, running_only)
    stats, trend, occ, start, now, step = _cached(key, 60, fetch)
    # One canonical group name per (job, gpu_type) pair, derived from the
    # summary series' instances so summary, trend, and occupancy agree.
    pairs = {(m.get("job", ""), m.get("gpu_type", "")) for m in
             (s["metric"] for s in stats)}
    aliases = {}
    for job, gtype in pairs:
        mig = {
            node_gpu_types.get(s["metric"].get("instance", ""))
            for s in stats
            if s["metric"].get("job", "") == job
            and s["metric"].get("gpu_type", "") == gtype
            and (node_gpu_types.get(s["metric"].get("instance", "")) or "")
            and _is_mig_gres(node_gpu_types.get(s["metric"].get("instance", "")))
        }
        aliases[(job, gtype)] = (mig.pop() if len(mig) == 1
                                 else _gpu_group_name(
                                     {"job": job, "gpu_type": gtype},
                                     node_gpu_types))
    out = aggregate_partition_stats(stats, node_gpu_types, aliases)
    trend_out = {
        _gpu_group_name(s["metric"], node_gpu_types, aliases):
        _series_values(s)
        for s in trend
    }
    # Window-average allocated GPU count per group, for mean occupancy.
    occupancy = {}
    for s in occ:
        values = _series_values(s)
        if values:
            name = _gpu_group_name(s["metric"], node_gpu_types, aliases)
            occupancy[name] = sum(v for _, v in values) / len(values)
    # Observed instances per group, for the capacity join in api_partitions.
    instances = {}
    for s in stats:
        m = s["metric"]
        inst = m.get("instance", "")
        if inst:
            name = _gpu_group_name(m, node_gpu_types, aliases)
            instances.setdefault(name, set()).add(inst)
    return out, trend_out, instances, occupancy, start, now, step


def aggregate_partition_stats(stats, node_gpu_types=None, aliases=None):
    """Time-weighted mean utilization per GPU group from collapsed-max series.

    Each series is the per-(job, node) max utilization across its window;
    averaging samples is a time-weighted mean (GPU devices are collapsed, so
    this is utilization, not GPU-hours). Groups are keyed by the canonical
    GPU-group name (the Slurm partition, MIG GRES profiles split out).
    """
    node_gpu_types = node_gpu_types or {}
    parts = {}
    for s in stats:
        m = s["metric"]
        name = _gpu_group_name(m, node_gpu_types, aliases)
        values = _series_values(s)
        if not values:
            continue
        p = parts.setdefault(
            name, {"name": name, "wsum": 0.0, "weight": 0, "max_util": 0.0,
                   "jobids": set()}
        )
        p["wsum"] += sum(v for _, v in values)
        p["weight"] += len(values)
        p["max_util"] = max(p["max_util"], max(v for _, v in values))
        p["jobids"].add(m.get("slurmjobid", ""))

    out = []
    for name, p in parts.items():
        out.append({
            "name": name,
            "mean_util": round(p["wsum"] / p["weight"], 2) if p["weight"] else 0.0,
            "max_util": round(p["max_util"], 1),
            "job_count": len(p["jobids"]),
        })
    out.sort(key=lambda p: p["mean_util"], reverse=True)
    return out


def _gpu_capacity(groups, instances, nodes, allocs):
    """Join metric groups to scontrol GPU capacity.

    ``instances`` maps group -> observed instance names (built in
    ``_partition_window``). Capacity is summed over **all** scontrol nodes
    whose ``gpu_type`` exactly equals the group name (MIG profiles), then
    over all **whole-GPU** nodes whose ``partitions`` list contains the
    group name (idle capacity included; MIG-gres nodes are excluded from
    the partition fallback because their GPUs belong to profile groups); a
    node shared by several partitions therefore counts toward each of
    them, matching how Slurm admits jobs to each. Groups with no scontrol
    membership fall back to their observed instances. Allocated
    uses the exact per-group live GPU count from ``allocs`` (a shared node's
    GPUs are counted only under the groups their jobs actually run in) and
    is capped at total.
    """
    nodes_by_name = {n["name"]: n for n in nodes}
    for g in groups:
        by_type = [
            n for n in nodes
            if n["gpus"] and n.get("gpu_type") and n["gpu_type"] == g["name"]
        ]
        by_partition = [
            n for n in nodes
            if (n["gpus"] and not _is_mig_gres(n.get("gpu_type"))
                and g["name"] in (n["partitions"] or "").split(","))
        ]
        if by_type:
            scope = by_type
        elif by_partition:
            scope = by_partition
        else:
            scope = [nodes_by_name[i] for i in instances.get(g["name"], ())
                     if i in nodes_by_name]
        total = sum(n["gpus"] for n in scope)
        g["gpus_alloc"] = int(min(allocs.get(g["name"], 0), total))
        g["gpus_total"] = int(total)
    return groups


@app.get("/api/partitions")
def api_partitions(since_hours: float = Query(24, gt=0, le=168),
                   running_only: bool = Query(False)):
    nodes = _cached("scontrol_nodes", 30, show_nodes)
    node_types = _gpu_type_by_node(nodes)
    groups, trend, instances, occupancy, start, now, step = _partition_window(
        since_hours, running_only, node_gpu_types=node_types)
    _, _, allocs_by_node, allocs_by_group = _node_current(node_types)
    _gpu_capacity(groups, instances, nodes, allocs_by_group)
    for g in groups:
        avg_alloc = occupancy.get(g["name"])
        total = g.get("gpus_total") or 0
        if avg_alloc is not None and total > 0:
            g["mean_occupancy"] = round(min(100.0, avg_alloc / total * 100.0), 1)
        else:
            g["mean_occupancy"] = None
    return {
        "window": {"start": start, "end": now},
        "step": step,
        "partitions": groups,
        "trend": trend,
    }


# sacct -j over tens of thousands of IDs exceeds the command timeout, so
# the VRAM chart enriches at most this many jobs (top by effective
# GPU-hours); the response reports the total candidate count so the UI
# can disclose the truncation.
_VRAM_RECORD_CAP = 2000


def _vram_job_records(since_hours, running_only=False, partition="",
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
    start, now = _job_window(since_hours)
    step = _step_for_range(now - start)
    live = None
    if running_only:
        live = _running_gpu_job_ids()
        if not live:
            return [], 0, start, now, step
    jobs, start, now, step = _fetch_job_window(since_hours, include_vram=False)
    for j in jobs:
        j["gpu_group"] = _job_gpu_group(j, node_gpu_types)
    if live is not None:
        jobs = [j for j in jobs if j["jobid"] in live]
    if partition:
        jobs = [j for j in jobs if j["gpu_group"] == partition]
    sel = "" if live is None else "{" + _jobid_matcher(live) + "}"

    def fetch():
        return get_prom().query_range(
            "max by (slurmjobid, instance, gpu) (slurm_job_memory_usage_gpu%s / "
            "1073741824)" % sel,
            start, now, step,
        )

    vram = _cached(("vram_gb", since_hours, running_only), 60, fetch)
    # Per-GPU peak VRAM (GB) over the window; a 0 sample means the GPU was
    # never reported with memory and cannot be a peak.
    peaks = defaultdict(list)
    for s in vram:
        jid = s["metric"].get("slurmjobid", "")
        vals = [v for _, v in _series_values(s) if v > 0]
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
    records = records[:_VRAM_RECORD_CAP]
    ids = sorted({r["jobid"] for r in records})
    if ids:
        meta = _cached(("sacct", tuple(ids)), 300, lambda: sacct_jobs(ids))
        for r in records:
            row = meta.get(r["jobid"]) or {}
            if row.get("gpus") and row.get("elapsed_s"):
                r["gpu_hours"] = round(row["gpus"] * row["elapsed_s"] / 3600.0, 2)
    wkey = "gpu_hours" if weight == "alloc" else "gpu_hours_eff"
    records.sort(key=lambda r: (r.get(wkey) or 0.0), reverse=True)
    return records, total, start, now, step


@app.get("/api/partitions/vram")
def api_part_vram(since_hours: float = Query(24, gt=0, le=168),
                  running_only: bool = Query(False),
                  partition: str = "",
                  weight: str = Query("alloc", pattern="^(alloc|eff)$")):
    node_types = _gpu_type_by_node(_cached("scontrol_nodes", 30, show_nodes))
    records, total, start, now, step = _vram_job_records(
        since_hours, running_only, partition, node_types, weight)
    return {
        "window": {"start": start, "end": now},
        "step": step,
        "total": total,
        "jobs": records,
    }


def _node_current(node_gpu_types=None):
    node_gpu_types = node_gpu_types or {}
    prom = get_prom()

    def fetch():
        inst_util = prom.query_instant("max by (instance) (slurm_job_utilization_gpu)")
        inst_vram = prom.query_instant(
            "avg by (instance) (slurm_job_memory_usage_gpu / "
            "slurm_job_memory_total_gpu * 100)"
        )
        active = prom.query_instant(
            "max by (instance, slurmjobid, job, user) (slurm_job_utilization_gpu)"
        )
        # The exporter publishes one utilization series per allocated GPU, so
        # the series count per node equals the allocated GPU count. The ``gpu``
        # label is job-local (every 1-GPU job says gpu="0"), so it must not be
        # used for allocation accounting.
        alloc = prom.query_instant(
            "count by (instance, job, gpu_type) (slurm_job_utilization_gpu)")
        return inst_util, inst_vram, active, alloc

    inst_util, inst_vram, active, alloc = _cached("node_current", 30, fetch)
    cur = {}
    for s in inst_util:
        cur[s["metric"]["instance"]] = {"util": float(s["value"][1])}
    for s in inst_vram:
        cur.setdefault(s["metric"]["instance"], {})["vram"] = float(s["value"][1])
    jobs_by_node = defaultdict(list)
    for s in active:
        m = s["metric"]
        jobs_by_node[m["instance"]].append(
            {
                "jobid": m.get("slurmjobid", ""),
                "job": m.get("job", ""),
                "user": m.get("user", ""),
                "util": float(s["value"][1]),
            }
        )
    allocs_by_node = {}
    allocs_by_group = {}
    for s in alloc:
        m = s["metric"]
        inst = m.get("instance", "")
        if not inst:
            continue
        count = int(float(s["value"][1]))
        allocs_by_node[inst] = allocs_by_node.get(inst, 0) + count
        group = _gpu_group_name(m, node_gpu_types)
        allocs_by_group[group] = allocs_by_group.get(group, 0) + count
    return cur, jobs_by_node, allocs_by_node, allocs_by_group


@app.get("/api/nodes")
def api_nodes(gpu_only: bool = True, refresh: bool = Query(False)):
    if refresh:
        # Forced refresh bypasses both the dashboard's 30-second
        # scontrol cache and the Prometheus client's response cache
        # (60 s range / 20 s instant) instead of redrawing cached data.
        get_prom().clear_cache()
        _invalidate_cache("scontrol_nodes", "node_current")
    nodes = _cached("scontrol_nodes", 30, show_nodes)
    if gpu_only:
        nodes = [n for n in nodes if n["gpus"]]
    try:
        cur, jobs_by_node, allocs, _ = _node_current()
    except PrometheusError:
        cur, jobs_by_node, allocs, _ = {}, {}, {}, {}
    for n in nodes:
        n["gpu_group"] = _node_gpu_group(n)
        c = cur.get(n["name"], {})
        n["current_util"] = c.get("util")
        n["current_vram"] = c.get("vram")
        n["gpus_alloc"] = min(allocs.get(n["name"], 0), n["gpus"])
        n["active_jobs"] = sorted(
            jobs_by_node.get(n["name"], []),
            key=lambda j: j["util"], reverse=True,
        )[:10]
    return {
        "time": int(time.time()),
        "count": len(nodes),
        "nodes": nodes,
    }


def _node_job_start(name, now):
    """Earliest sacct start of jobs actively reporting on a node.

    Falls back to a six-hour window when the node has no live GPU jobs,
    sacct is unavailable, or no start value parses. Starts older than seven
    days are clamped to bound the window (and the payload).
    """
    fallback_start = now - 6 * 3600
    try:
        live = {
            s["metric"]["slurmjobid"]
            for s in get_prom().query_instant(
                'count by (slurmjobid) (slurm_job_utilization_gpu{instance="%s"})'
                % name
            )
            if s["metric"].get("slurmjobid")
        }
    except PrometheusError:
        return fallback_start
    if not live:
        return fallback_start
    try:
        meta = sacct_jobs(sorted(live))
    except SlurmError:
        return fallback_start
    starts = [e for e in (_sacct_epoch((meta.get(j) or {}).get("start"))
                          for j in live) if e]
    if not starts:
        return fallback_start
    return max(min(starts), now - 7 * 86400)


@app.get("/api/nodes/{name}")
def api_node_detail(
        name: str, view: str = Query("job_start", pattern="^(job_start|1|6|24)$")):
    now = int(time.time())
    if view == "job_start":
        start = _node_job_start(name, now)
    else:
        start = now - int(float(view) * 3600)
    step = _step_for_range(now - start)
    prom = get_prom()

    def fetch():
        util = prom.query_range(
            'max by (slurmjobid, gpu) '
            '(slurm_job_utilization_gpu{instance="%s"})' % name,
            start, now, step,
        )
        vram = prom.query_range(
            # ``gpu`` is job-local (every 1-GPU job reports gpu="0"), so the
            # grouping must keep ``slurmjobid`` or co-located jobs merge.
            'avg by (slurmjobid, gpu) (slurm_job_memory_usage_gpu{instance="%s"} / '
            'slurm_job_memory_total_gpu{instance="%s"} * 100)' % (name, name),
            start, now, step,
        )
        return util, vram

    util, vram = _cached(("nodedetail", name, view, start), 30, fetch)
    return {
        "node": name,
        "view": view,
        "window": {"start": start, "end": now},
        "step": step,
        "series": {
            "utilization": [
                {"metric": s["metric"], "values": _series_values(s)} for s in util
            ],
            "vram": [
                {"metric": s["metric"], "values": _series_values(s)} for s in vram
            ],
        },
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
