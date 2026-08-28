"""GPU efficiency admin dashboard — FastAPI app.

Data is collected on demand: every API call queries Prometheus, sacct and
scontrol live (with short in-memory TTL caches). No background collection.
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import ConfigError, load_config
from prom import PromClient, PrometheusError
from slurm import SlurmError, sacct_jobs, show_nodes, show_partitions

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
        _prom = PromClient(cfg["api_base"], cfg["username"], cfg["password"], cfg["timeout"])
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


def _fetch_job_window(since_hours, include_vram=True):
    """Fetch job-level utilization (and optionally vram) series for a window.

    Returns (jobs, start, end) where jobs is a list of dicts aggregated from
    Prometheus over the window (no sacct enrichment yet).
    """
    start, now = _job_window(since_hours)
    step = _step_for_range(now - start)

    def fetch():
        util = get_prom().query_range(
            "max by (slurmjobid, instance, job, user, gpu_type) (slurm_job_utilization_gpu)",
            start, now, step,
        )
        vram = []
        if include_vram:
            vram = get_prom().query_range(
                "avg by (slurmjobid, instance, job) (slurm_job_memory_usage_gpu / "
                "slurm_job_memory_total_gpu * 100)",
                start, now, step,
            )
        return util, vram, start, now, step

    key = ("jobs", since_hours, include_vram)
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
        out.append({
            "jobid": jid,
            "user": job["user"],
            "partition": job["partition"],
            "gpu_type": job["gpu_type"],
            "nodes": sorted(n for n in job["nodes"] if n),
            "mean_util": round(job["eff_sum"] / job["eff_samples"], 2)
            if job["eff_samples"] else 0.0,
            "max_util": round(job["max_util"], 2),
            "gpu_hours_eff": round(job["eff_hours"], 2),
            "vram_avg": round(sum(vv) / len(vv), 1) if vv else None,
        })
    out.sort(key=lambda j: j["gpu_hours_eff"], reverse=True)
    return out, start, now, step


def _enrich(jobs, since_hours):
    ids = [j["jobid"] for j in jobs]
    start_iso = datetime.fromtimestamp(
        time.time() - since_hours * 3600, tz=timezone.utc
    ).strftime("%Y-%m-%d")
    if not ids:
        return
    meta = _cached(("sacct", tuple(ids), start_iso), 300,
                   lambda: sacct_jobs(ids, start_iso))
    for job in jobs:
        row = meta.get(job["jobid"])
        if not row:
            continue
        for key in ("name", "state", "start", "end", "node_list", "account"):
            if row.get(key):
                job[key] = row[key]
        if row.get("gpus"):
            job["gpus"] = row["gpus"]
            if row.get("gpu_type"):
                job["gpu_type"] = row["gpu_type"]
        if row.get("elapsed_s") and row.get("gpus"):
            alloc = row["gpus"] * row["elapsed_s"] / 3600.0
            util = job.get("mean_util") or 0.0
            job["gpu_hours_alloc"] = round(alloc, 2)
            job["gpu_hours_eff"] = round(alloc * util / 100.0, 2)
            job["efficiency"] = round(util, 1)


@app.exception_handler(PrometheusError)
def prom_error_handler(request, exc):
    return JSONResponse(502, {"error": "prometheus_unreachable", "detail": str(exc)})


@app.exception_handler(SlurmError)
def slurm_error_handler(request, exc):
    return JSONResponse(502, {"error": "slurm_unreachable", "detail": str(exc)})


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


@app.get("/api/jobs")
def api_jobs(
    since_hours: float = Query(24, gt=0, le=168),
    partition: str = "",
    user: str = "",
    search: str = "",
    limit: int = Query(500, ge=1, le=2000),
):
    jobs, start, now, _ = _fetch_job_window(since_hours)
    if partition:
        jobs = [j for j in jobs if j["partition"] == partition]
    if user:
        jobs = [j for j in jobs if j["user"] == user.lower()]
    # Bound the sacct enrichment cost before it; name search therefore only
    # covers the top-``limit`` jobs by effective GPU hours.
    jobs = jobs[:limit]
    _enrich(jobs, since_hours)
    if search:
        needle = search.lower()
        jobs = [
            j for j in jobs
            if needle in j["jobid"] or needle in (j.get("name") or "").lower()
        ]
    partitions = sorted({j["partition"] for j in jobs if j["partition"]})
    return {
        "window": {"start": start, "end": now},
        "count": len(jobs),
        "partitions": partitions,
        "jobs": jobs,
    }


@app.get("/api/jobs/{jobid}")
def api_job_detail(jobid: str, since_hours: float = Query(24, gt=0, le=168)):
    start, now = _job_window(since_hours)
    step = _step_for_range(now - start)
    prom = get_prom()

    def fetch():
        util = prom.query_range(
            'max by (slurmjobid, instance, gpu) (slurm_job_utilization_gpu{slurmjobid="%s"})'
            % jobid,
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
    meta = _cached(
        ("sacct", (jobid,), datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        300,
        lambda: sacct_jobs([jobid], datetime.now(timezone.utc).strftime("%Y-%m-%d")).get(jobid),
    )
    return {"jobid": jobid, "window": {"start": start, "end": now}, "step": step,
            "metadata": meta, "series": series}


def _partition_window(since_hours, group_by="partition", now=None):
    start, now = _job_window(since_hours, now)
    step = _step_for_range(now - start)
    label = "job" if group_by == "partition" else "gpu_type"

    def fetch():
        stats = get_prom().query_range(
            "max by (slurmjobid, instance, %s) (slurm_job_utilization_gpu)" % label,
            start, now, step,
        )
        trend = get_prom().query_range(
            "avg by (%s) (slurm_job_utilization_gpu)" % label,
            start, now, step,
        )
        return stats, trend, start, now, step

    key = ("parts", since_hours, group_by)
    stats, trend, start, now, step = _cached(key, 60, fetch)

    out = aggregate_partition_stats(stats, label)
    trend_out = {
        s["metric"].get(label, "unknown"): _series_values(s) for s in trend
    }
    return out, trend_out, start, now, step


def aggregate_partition_stats(stats, label):
    """Time-weighted mean utilization per group from collapsed-max series.

    Each series is the per-(job, node) max utilization across its window;
    averaging samples is a time-weighted mean (GPU devices are collapsed, so
    this is utilization, not GPU-hours).
    """
    parts = {}
    for s in stats:
        m = s["metric"]
        name = m.get(label, "unknown")
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



def _match_scontrol_partition(name, scontrol_parts):
    """Map a Prometheus short partition label to full Slurm partition(s).

    Prometheus ``job`` labels are shortened (``b300``, ``v100_32g``); Slurm
    names are full (``gpu-b300-288g-ellis``). Normalize ``_`` to ``-`` and
    match on substring. A Prometheus partition may map to several Slurm
    partitions (e.g. ``h200`` -> many ``gpu-h200-*``).
    """
    norm = name.replace("_", "-")
    matches = [p for p in scontrol_parts if norm in p["name"].replace("_", "-")]
    if not matches:
        return None

    seen = []
    for p in matches:
        for n in p["nodes"].split(","):
            if n and n != "(null)" and n not in seen:
                seen.append(n)

    state = "UP" if {p["state"] for p in matches} == {"UP"} else "MIXED"
    return {"name": matches[0]["name"], "nodes": ",".join(seen),
            "state": state, "slurm_partitions": [p["name"] for p in matches]}


@app.get("/api/partitions")
def api_partitions(since_hours: float = Query(24, gt=0, le=168),
                   group_by: str = Query("partition", pattern="^(partition|gpu_type)$")):
    parts, trend, start, now, step = _partition_window(since_hours, group_by)
    scontrol_parts = _cached("scontrol_partitions", 300, show_partitions)
    for p in parts:
        sp = _match_scontrol_partition(p["name"], scontrol_parts)
        if sp:
            p.update(sp)
    return {
        "window": {"start": start, "end": now},
        "step": step,
        "partitions": parts,
        "trend": trend,
    }


def _node_current():
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
        return inst_util, inst_vram, active

    inst_util, inst_vram, active = _cached("node_current", 30, fetch)
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
    return cur, jobs_by_node


@app.get("/api/nodes")
def api_nodes(gpu_only: bool = True):
    nodes = _cached("scontrol_nodes", 30, show_nodes)
    if gpu_only:
        nodes = [n for n in nodes if n["gpus"]]
    try:
        cur, jobs_by_node = _node_current()
    except PrometheusError:
        cur, jobs_by_node = {}, {}
    for n in nodes:
        c = cur.get(n["name"], {})
        n["current_util"] = c.get("util")
        n["current_vram"] = c.get("vram")
        n["active_jobs"] = sorted(
            jobs_by_node.get(n["name"], []),
            key=lambda j: j["util"], reverse=True,
        )[:10]
    return {
        "time": int(time.time()),
        "count": len(nodes),
        "nodes": nodes,
    }


@app.get("/api/nodes/{name}")
def api_node_detail(name: str, window_hours: float = Query(1, gt=0, le=48)):
    start, now = _job_window(window_hours)
    step = _step_for_range(now - start)
    prom = get_prom()

    def fetch():
        util = prom.query_range(
            'max by (slurmjobid, gpu) (slurm_job_utilization_gpu{instance="%s"})' % name,
            start, now, step,
        )
        vram = prom.query_range(
            'avg by (gpu) (slurm_job_memory_usage_gpu{instance="%s"} / '
            'slurm_job_memory_total_gpu{instance="%s"} * 100)' % (name, name),
            start, now, step,
        )
        return util, vram

    util, vram = _cached(("nodedetail", name, window_hours), 30, fetch)
    return {
        "node": name,
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
