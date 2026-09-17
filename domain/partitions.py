"""Partition/node-capacity domain logic: occupancy, capacity joins,
current node state, and a node's live-job-start window.
"""

from collections import defaultdict
from datetime import datetime

import cache
import deps
import gpu_groups
from domain.common import job_window, running_gpu_job_ids, series_values, step_for_range
from prom import PrometheusError
from promql import label_eq, label_in, selector
from slurm import SlurmError


def _sacct_epoch(value):
    """sacct start/end string to epoch seconds; None when missing/invalid."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


WAIT_TOTAL_KEY = "__total__"
"""Cluster-wide sentinel key in ``started_wait_summary``'s result.

The queue endpoint consumes it while building its unique totals; it is
never a GPU type and must never leak into the public ``queue`` map.
"""


def partition_window(since_hours, running_only=False, now=None,
                     node_gpu_types=None):
    """GPU-type utilization window.

    Groups are keyed by the canonical GPU type (gpu_groups.canonical_gpu_type):
    the short scontrol GRES type, MIG profiles split out from their node's
    whole-GPU pool), not by Slurm partition — priority-only partitions over
    the same hardware share one group. Summary data keeps the
    ``slurmjobid`` label (per-job/per-node max, so the job identity
    survives for running-only matching); the trend/occupancy queries are
    ``sum``/``count`` ``by (gpu_type)`` — per-timestamp sum / count gives
    the utilization trend, the count itself is the occupancy series —
    and have no job identity, so the matcher must be injected into the
    metric selector before the aggregation.

    Also returns ``job_groups`` — the canonical group(s) each observed job
    belongs to — for the historical wait join in ``started_wait_summary``.
    """
    start, now = job_window(since_hours, now)
    step = step_for_range(now - start)
    sel = ""
    if running_only:
        live = running_gpu_job_ids()
        if not live:
            return [], {}, {}, {}, {}, start, now, step
        sel = selector(label_in("slurmjobid", live))

    def fetch():
        stats = deps.get_prom().query_range(
            "max by (slurmjobid, instance, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        # Utilization numerators and per-timestamp series counts, both
        # keyed by the raw exporter gpu_type label: per-timestamp
        # sum/count is the utilization trend and the count series is the
        # occupancy series (same selector so running-only matches both).
        util_sums = deps.get_prom().query_range(
            "sum by (gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        gpu_counts = deps.get_prom().query_range(
            "count by (gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        return stats, util_sums, gpu_counts, start, now, step

    key = cache.partition_window_key(since_hours, running_only)
    stats, util_sums, gpu_counts, start, now, step = \
        deps.route_cache.get_or_set(key, 60, fetch)
    out = aggregate_partition_stats(stats, node_gpu_types)
    trend_out, occupancy = aggregate_gpu_type_series(
        util_sums, gpu_counts, node_gpu_types)
    # Observed instances per group, for the capacity join in api_partitions.
    instances = {}
    for s in stats:
        m = s["metric"]
        inst = m.get("instance", "")
        if inst:
            name = gpu_groups.gpu_group_name(m, node_gpu_types)
            instances.setdefault(name, set()).add(inst)
    # Canonical group per observed job, for the historical wait join in
    # started_wait_summary. Built from the same non-empty summary series
    # as the rows; raw job-ID sets stay server-side.
    job_groups = {}
    for s in stats:
        m = s["metric"]
        if not series_values(s):
            continue
        jobid = m.get("slurmjobid", "")
        if jobid:
            name = gpu_groups.gpu_group_name(m, node_gpu_types)
            job_groups.setdefault(jobid, set()).add(name)
    return out, trend_out, instances, occupancy, job_groups, start, now, step


def aggregate_gpu_type_series(util_sums, gpu_counts, node_gpu_types=None):
    """Utilization trend and mean occupancy from raw-label sum/count series.

    ``util_sums`` (``sum by (gpu_type)``) and ``gpu_counts`` (``count by
    (gpu_type)``) are keyed by the exporter's raw ``gpu_type`` label, which
    aliases one hardware type under several spellings (``NVIDIA H200`` /
    ``h200``; several V100 model labels). Canonicalize every label against
    the configured scontrol types, sum numerators and counts per canonical
    group at each timestamp, and divide only where the merged count is
    positive — that per-timestamp sum/count IS the utilization trend. The
    count series itself is the occupancy series: a group's value is the
    mean of its merged per-timestamp counts (window-average concurrent
    allocated GPUs). Trend timestamps are sorted. Returns
    ``({group: [(ts, util)], ...}, {group: mean_count})``.
    """
    node_gpu_types = node_gpu_types or {}
    configured = sorted({t for types in node_gpu_types.values()
                         for t in types})

    def canonical(label):
        # Same resolution gpu_group_name applies to the per-series labels:
        # exact/aliased/MIG-profile labels canonicalize against the
        # configured scontrol types; anything unresolvable keeps its
        # normalized own identity.
        return gpu_groups.canonical_gpu_type(label, configured)

    sums = defaultdict(dict)   # group -> {ts: numerator}
    counts = defaultdict(dict)  # group -> {ts: series count}
    for s in util_sums:
        group = canonical(s["metric"].get("gpu_type", ""))
        for ts, v in series_values(s):
            sums[group][ts] = sums[group].get(ts, 0.0) + v
    for s in gpu_counts:
        group = canonical(s["metric"].get("gpu_type", ""))
        for ts, v in series_values(s):
            counts[group][ts] = counts[group].get(ts, 0.0) + v
    trend = {}
    for group, by_ts in sums.items():
        points = [(ts, by_ts[ts] / counts[group][ts])
                  for ts in sorted(by_ts) if counts[group].get(ts, 0.0) > 0]
        if points:
            trend[group] = points
    occupancy = {
        group: sum(by_ts.values()) / len(by_ts)
        for group, by_ts in counts.items() if by_ts
    }
    return trend, occupancy


def aggregate_partition_stats(stats, node_gpu_types=None):
    """Time-weighted mean utilization per GPU type from collapsed-max series.

    Each series is the per-(job, node) max utilization across its window;
    averaging samples is a time-weighted mean (GPU devices are collapsed, so
    this is utilization, not GPU-hours). Groups are keyed by the canonical
    GPU type (the short scontrol GRES type, MIG GRES profiles split out);
    partitions over the same hardware merge.
    """
    node_gpu_types = node_gpu_types or {}
    parts = {}
    for s in stats:
        m = s["metric"]
        name = gpu_groups.gpu_group_name(m, node_gpu_types)
        values = series_values(s)
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


def _node_gres(node):
    """``[(type, count), ...]`` for a node, falling back to its scalar
    ``gpu_type``/``gpus`` when ``gres`` is absent (a single-type node from
    a caller that predates the per-type breakdown)."""
    gres = node.get("gres")
    if gres is not None:
        return gres
    if node.get("gpu_type") and node.get("gpus"):
        return [(node["gpu_type"], node["gpus"])]
    return []


def _node_type_count(node, gtype):
    """This node's GPU count of exactly ``gtype`` (0 if it has none)."""
    return sum(c for t, c in _node_gres(node) if t == gtype)


def gpu_capacity(groups, instances, nodes, allocs):
    """Join GPU-type groups to scontrol GPU capacity.

    ``instances`` maps group -> observed instance names (built in
    ``partition_window``). Capacity is summed over **all** scontrol nodes
    carrying a GRES entry whose type exactly equals the group name —
    whole GPUs and MIG profiles alike, idle capacity included,
    independent of partition membership (several priority partitions
    over the same nodes therefore share one type total). A node with
    more than one GRES type (part whole, part MIG-sliced) contributes
    only the matching type's own count to each group, never its other
    type's and never the node's combined GPU count. Groups with no
    scontrol membership fall back to their observed instances (an
    exporter label that could not be resolved to a configured type).
    Allocated uses the exact per-group live GPU count from ``allocs``
    (a shared node's GPUs are counted only under the groups their jobs
    actually run in) and is capped at total.
    """
    nodes_by_name = {n["name"]: n for n in nodes}
    for g in groups:
        by_type = [n for n in nodes if _node_type_count(n, g["name"])]
        if by_type:
            total = sum(_node_type_count(n, g["name"]) for n in by_type)
        else:
            scope = [nodes_by_name[i] for i in instances.get(g["name"], ())
                     if i in nodes_by_name]
            total = sum(n["gpus"] for n in scope)
        g["gpus_alloc"] = int(min(allocs.get(g["name"], 0), total))
        g["gpus_total"] = int(total)
    return groups


def node_current(node_gpu_types=None):
    node_gpu_types = node_gpu_types or {}
    prom = deps.get_prom()

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

    inst_util, inst_vram, active, alloc = deps.route_cache.get_or_set(
        cache.node_current_key(), 30, fetch)
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
        group = gpu_groups.gpu_group_name(m, node_gpu_types)
        allocs_by_group[group] = allocs_by_group.get(group, 0) + count
    return cur, jobs_by_node, allocs_by_node, allocs_by_group


def node_job_start(name, now):
    """Earliest sacct start of jobs actively reporting on a node.

    Falls back to a six-hour window when the node has no live GPU jobs,
    sacct is unavailable, or no start value parses. Starts older than seven
    days are clamped to bound the window (and the payload).
    """
    fallback_start = now - 6 * 3600
    sel = selector(label_eq("instance", name))
    try:
        live = {
            s["metric"]["slurmjobid"]
            for s in deps.get_prom().query_instant(
                "count by (slurmjobid) (slurm_job_utilization_gpu%s)" % sel
            )
            if s["metric"].get("slurmjobid")
        }
    except PrometheusError:
        return fallback_start
    if not live:
        return fallback_start
    try:
        meta = deps.sacct_jobs(sorted(live))
    except SlurmError:
        return fallback_start
    starts = [e for e in (_sacct_epoch((meta.get(j) or {}).get("start"))
                          for j in live) if e]
    if not starts:
        return fallback_start
    return max(min(starts), now - 7 * 86400)


def _pending_gpu_types(job, partition_types):
    """The GPU-type groups one pending job counts toward.

    Resolution is exact and ordered:

    a. a typed request (``gres/gpu:a100:1`` in %b) is canonicalized
       against the configured GRES types (MIG profiles resolve to their
       profile) and belongs only to that type — its eventual nodes are
       unknowable pre-scheduling, so the named type is the one place the
       demand is certainly wanted, regardless of %P;
    b. an untyped GPU request (``gres/gpu:N``), an ``N/A``/absent %b with
       a GPU count, or a constraints-only request belongs to the
       deduplicated union of GPU types mapped from all its requested
       partitions (%P may list several): several priority partitions over
       the same ``h200`` hardware contribute once to ``h200``; genuinely
       different hardware contributes once per eligible type;
    c. an explicit untyped GPU request whose partitions map to no GPU
       type (or name none at all) is retained under ``unknown`` rather
       than silently dropped;
    d. a row with neither an explicit GPU request nor any GPU-backed
       partition mapping is CPU-only and resolves to ``[]`` — the caller
       omits it from summary and waiting list alike.

    Returns a sorted, duplicate-free list of canonical type names.
    """
    typed = (job.get("gpu_type") or "").strip()
    if typed:
        # A typed request belongs only to its canonical type, even when %P
        # lists several partitions: the named type is the one place the
        # demand is certainly wanted.
        return sorted({gpu_groups.canonical_gpu_type(
            typed, _all_types(partition_types))})
    # Untyped %b (bare gres/gpu:N, N/A, or constraints-only) or no GPU
    # request at all: eligibility comes from the requested partitions'
    # GPU-type union. An explicit GPU count with no resolvable partition
    # type stays visible under "unknown"; a CPU-only row resolves to [].
    union = set()
    for p in (job["partition"] or "").split(","):
        union |= partition_types.get(p.strip(), set())
    if job["gpus"] and not union:
        return ["unknown"]
    return sorted(union)


def _all_types(partition_types):
    """Every distinct GPU type any configured partition maps to."""
    return sorted({t for types in partition_types.values() for t in types})


def pending_queue_status(jobs, now, partition_types):
    """Aggregate pending jobs per GPU type and list the waiting ones.

    Returns ``(summary, totals, waiting_jobs)``. A job's eligible types
    come from ``_pending_gpu_types`` (%b request first, then the
    GPU-type union of its %P partitions); CPU-only jobs (no GPU request,
    no GPU-backed partition) are omitted from the summary and the
    waiting list entirely. Each included job is classified against its
    complete eligibility set: one eligible type is exclusive to that
    row; several make it flexible in every eligible row, so a row's
    figures are "demand that could land here", never disjoint slices —
    only ``totals`` counts each physical job exactly once. Per-row
    ``eligible_*`` fields (exclusive + flexible) are therefore
    deliberately non-additive across rows.

    GPU demand follows this dashboard's stated cluster invariant: a GPU
    job requests one node, so the demand a job contributes is its parsed
    per-job GPU request (squeue ``gpus``, from %b / TresPerNode)
    unchanged. ``totals`` carries ``unique_pending_jobs`` and
    ``unique_gpus_requested``, each counted once per physical job.

    Each ``waiting_jobs`` record carries the parsed squeue fields plus
    ``groups`` (the eligible GPU types above, for client-side type
    filtering), ``wait_s`` — ``max(0, now - submit)`` in seconds when
    the submit time parses, else ``null`` — and ``gpu_total``: the
    job's parsed per-job GPU request (0 for a constraints-only GPU-
    partition row). The list holds each included physical job once; the
    client filters by ``groups``.
    """
    out = defaultdict(lambda: {"exclusive_jobs": 0, "flexible_jobs": 0,
                               "exclusive_gpus": 0, "flexible_gpus": 0})
    totals = {"unique_pending_jobs": 0, "unique_gpus_requested": 0}
    waiting = []
    for job in jobs:
        groups = _pending_gpu_types(job, partition_types)
        if not groups:
            continue  # CPU-only: no GPU type it could run on
        # Eligibility is complete before classification: one eligible
        # type is exclusive; several make the job flexible in every
        # eligible row. Rows overlap by design — the cluster totals are
        # the only place each physical job counts exactly once.
        kind = "exclusive" if len(groups) == 1 else "flexible"
        gpu_demand = job["gpus"]
        for g in groups:
            out[g][kind + "_jobs"] += 1
            out[g][kind + "_gpus"] += gpu_demand
        totals["unique_pending_jobs"] += 1
        totals["unique_gpus_requested"] += gpu_demand
        submitted = _sacct_epoch(job["submit"])
        waiting.append({
            "jobid": job["jobid"],
            "user": job["user"],
            "partition": job["partition"],
            "state": job["state"],
            "submit": job["submit"],
            "start": job["start"],
            "reason": job["reason"],
            "nodes": job["nodes"],
            "gpus": job["gpus"],
            "gpu_type": job["gpu_type"],
            "groups": groups,
            "wait_s": (max(0, int(now - submitted))
                       if submitted is not None else None),
            "gpu_total": gpu_demand,
        })
    return dict(out), totals, waiting


_WAIT_BUCKETS = (
    ("lt_5m", 300),
    ("m5_to_30m", 1800),
    ("m30_to_2h", 7200),
    ("h2_to_12h", 43200),
)


def _wait_statistics(samples):
    """Completed-job wait summary: percentiles, average, and buckets.

    P50 is the ordinary median (mean of the two middle waits when the
    sample count is even). P90 is nearest-rank (ceil(0.90 * n)), so the
    reported percentile is always an observed wait. Buckets are
    half-open: <5m, 5-30m, 30m-2h, 2-12h, >=12h.
    """
    ordered = sorted(samples)
    buckets = {name: 0 for name, _ in _WAIT_BUCKETS}
    buckets["gte_12h"] = 0
    for wait in ordered:
        for name, upper in _WAIT_BUCKETS:
            if wait < upper:
                buckets[name] += 1
                break
        else:
            buckets["gte_12h"] += 1
    if not ordered:
        return {"wait_p50_s": None, "wait_p90_s": None,
                "wait_avg_s": None, "wait_samples": 0,
                "wait_buckets": buckets}
    middle = len(ordered) // 2
    p50 = (ordered[middle] if len(ordered) % 2
           else round((ordered[middle - 1] + ordered[middle]) / 2))
    p90 = ordered[-(-9 * len(ordered) // 10) - 1]
    return {"wait_p50_s": p50, "wait_p90_s": p90,
            "wait_avg_s": round(sum(ordered) / len(ordered)),
            "wait_samples": len(ordered), "wait_buckets": buckets}


def wait_empty():
    """The zero-sample wait-statistics shape (all null / all zero)."""
    return _wait_statistics([])


def started_wait_summary(job_groups, window_start, window_end):
    """Completed-job Submit → Start wait statistics per group.

    ``job_groups`` maps each Prometheus-observed job ID to its canonical
    group(s) — the same pairing that built the partition rows. The union
    of IDs is enriched through the cached explicit-ID ``sacct`` lookup
    (``squeue`` cannot see jobs after they leave the controller); only
    records whose ``submit`` and ``start`` both parse, whose start falls
    inclusively inside ``[window_start, window_end]``, and whose start is
    not before submit contribute. Invalid or missing times are excluded
    from every statistic, never read as 0. Returns
    ``{group: {"wait_p50_s", "wait_p90_s", "wait_avg_s",
    "wait_samples", "wait_buckets"}, "__total__": {...}}``.
    """
    ids = sorted(set(job_groups))
    default_total = wait_empty()
    if not ids:
        return {"__total__": dict(default_total)}
    records = deps.route_cache.get_or_set(
        cache.sacct_key(ids), 300,
        lambda: deps.sacct_jobs(ids))
    waits = defaultdict(list)
    # Iterate the REQUESTED ids, not records.items(): sacct indexes an
    # array task under both its JobID notation and its raw numeric
    # JobIDRaw, so scanning the response would count one physical job's
    # wait twice. job_groups is keyed by the raw slurmjobid label only.
    for jobid in ids:
        rec = records.get(jobid)
        if not rec:
            continue
        started = _sacct_epoch(rec.get("start"))
        submitted = _sacct_epoch(rec.get("submit"))
        if (started is None or submitted is None
                or not (window_start <= started <= window_end)
                or started < submitted):
            continue
        wait = int(started - submitted)
        for g in job_groups[jobid]:
            waits[g].append(wait)
        waits[WAIT_TOTAL_KEY].append(wait)
    names = set(waits) | {WAIT_TOTAL_KEY}
    return {name: _wait_statistics(waits.get(name, [])) for name in names}
