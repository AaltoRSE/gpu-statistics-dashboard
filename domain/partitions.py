"""Partition/node-capacity domain logic: occupancy, capacity joins,
current node state, and a node's live-job-start window.
"""

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import cache
import deps
import gpu_groups
from domain.common import job_window, running_gpu_job_ids, series_values, step_for_range
from prom import PrometheusError
from promql import label_eq, label_in, selector
from slurm import SlurmError, expand_node_list

CLUSTER_TZ = ZoneInfo("Europe/Helsinki")
"""sacct prints naive cluster-local (Europe/Helsinki) strings; interpret
them on that wall clock, never the process's (deployment hosts vary)."""


def _sacct_epoch(value):
    """A sacct Europe/Helsinki-naive string to epoch seconds; None when
    missing/invalid."""
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if naive.tzinfo is not None:
        return naive.timestamp()
    return naive.replace(tzinfo=CLUSTER_TZ).timestamp()




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
    ``sum``/``count`` ``by (instance, gpu_type)`` so each raw exporter
    label resolves against its own scontrol node before canonical types
    merge. Per-timestamp sum / count gives the utilization trend, the
    count itself is the occupancy series, and the matcher must be injected
    into the metric selector before aggregation.

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
        # Preserve the source instance through aggregation: a raw label
        # resolves against that node's scontrol GRES list before aliases
        # merge into canonical GPU types. Per-timestamp sum/count is the
        # utilization trend; the count series is occupancy.
        util_sums = deps.get_prom().query_range(
            "sum by (instance, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        gpu_counts = deps.get_prom().query_range(
            "count by (instance, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        return stats, util_sums, gpu_counts, start, now, step

    key = cache.partition_window_key(since_hours, running_only)
    stats, util_sums, gpu_counts, start, now, step = \
        deps.route_cache.get_or_set(key, 60, fetch)
    out = aggregate_partition_stats(stats, node_gpu_types)
    trend_out, occupancy = aggregate_gpu_type_series(
        util_sums, gpu_counts, node_gpu_types)
    if not running_only:
        out, trend_out, occupancy = _include_configured_gpu_types(
            out, trend_out, occupancy, node_gpu_types)
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
    """Utilization trend and mean occupancy from instance-aware series.

    ``util_sums`` and ``gpu_counts`` preserve each raw exporter label's
    ``instance``. Resolve it against that node's configured scontrol GRES
    types, then merge aliases into canonical groups per timestamp. The
    resulting sum/count is the utilization trend; mean merged count is
    occupancy.
    """
    node_gpu_types = node_gpu_types or {}
    sums = defaultdict(dict)   # group -> {ts: numerator}
    counts = defaultdict(dict)  # group -> {ts: series count}
    for s in util_sums:
        group = gpu_groups.gpu_group_name(s["metric"], node_gpu_types)
        for ts, v in series_values(s):
            sums[group][ts] = sums[group].get(ts, 0.0) + v
    for s in gpu_counts:
        group = gpu_groups.gpu_group_name(s["metric"], node_gpu_types)
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


def _include_configured_gpu_types(groups, trend, occupancy, node_gpu_types):
    """Add configured but unmeasured scontrol GPU types as explicit no-data rows."""
    configured = sorted({gpu_type for types in (node_gpu_types or {}).values()
                         for gpu_type in types if gpu_type})
    existing = {group["name"] for group in groups}
    missing = [
        {"name": gpu_type, "mean_util": None, "max_util": None,
         "job_count": 0}
        for gpu_type in configured if gpu_type not in existing
    ]
    for group in missing:
        trend[group["name"]] = []
        occupancy[group["name"]] = None
    return groups + missing, trend, occupancy


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
       profile). Its eventual nodes are unknowable pre-scheduling, so the
       named type is the one place the demand is certainly wanted,
       regardless of %P — unless the label is a base name that split into
       several configured pools (plain v100 behind v100_16gb/v100_32gb):
       that request is ambiguous and counts toward every pool its
       requested partitions could land on, exactly like an untyped
       request, instead of stranding in a capacity-less synthetic row;
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
        # A typed request belongs to its canonical type(s), even when %P
        # lists several partitions: the named type is the one place the
        # demand is certainly wanted. A base label that split into several
        # configured memory pools (scontrol reports plain v100 for both
        # V100 pool sizes) is ambiguous — it maps to every eligible pool
        # behind the requested partitions, exactly like an untyped
        # request, instead of stranding in a capacity-less synthetic row.
        eligible = set()
        for p in (job["partition"] or "").split(","):
            eligible |= partition_types.get(p.strip(), set())
        configured = sorted(eligible) or _all_types(partition_types)
        tokens = gpu_groups._tokens(typed)
        matches = [t for t in configured
                   if tokens and gpu_groups._tokens(t) <= tokens]
        if len(matches) > 1:
            return sorted(matches)
        target = gpu_groups.canonical_gpu_type(typed, configured)
        if target == typed.casefold():
            # Still ambiguous (or absorbed) after canonicalization: keep
            # every eligible pool the request could land in rather than a
            # synthetic base group.
            return sorted(eligible) or [target]
        return sorted({target})
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


def _median(values):
    """Ordinary median of a numeric list, empty input yielding None.

    Odd count: the middle value. Even count: the mean of the two middle
    values, rounded to an integer ONLY for integer inputs ([1, 2] -> 2,
    not 1); float lists keep the exact mean (display rounding happens at
    the caller).
    """
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    a, b = ordered[middle - 1], ordered[middle]
    mean = (a + b) / 2
    return round(mean) if isinstance(a, int) and isinstance(b, int) else mean


def _wait_statistics(samples):
    """Completed-job wait summary: percentiles, average, sample count.

    P50 is the ordinary median (mean of the two middle waits when the
    sample count is even, integer-rounded). P90 is nearest-rank
    (ceil(0.90 * n)), so the reported percentile is always an observed
    wait. ``wait_samples`` counts the valid Submit->Start records behind
    those figures.
    """
    ordered = sorted(samples)
    if not ordered:
        return {"wait_p50_s": None, "wait_p90_s": None,
                "wait_avg_s": None, "wait_samples": 0,
                "wait_per_gpu_hour_weighted": None}
    p90 = ordered[-(-9 * len(ordered) // 10) - 1]
    return {"wait_p50_s": _median(ordered), "wait_p90_s": p90,
            "wait_avg_s": round(sum(ordered) / len(ordered)),
            "wait_samples": len(ordered),
            "wait_per_gpu_hour_weighted": None}


def wait_empty():
    """The zero-sample wait-statistics shape (all null / all zero)."""
    return _wait_statistics([])


progress_store = {}
"""Per-window latest batched accounting progress for the queue loader.

The queue endpoint polls this while the first seven-day accounting fetch
works through its daily batches, so the UI can show real progress instead
of one opaque spinner. Each fetch updates, then clears, its entry.
"""


def completed_wait_summary(records, node_gpu_types, window_start, window_end,
                           accounting_coverage=None):
    """Completed-job wait metrics grouped from bounded sacct records.

    Each accepted record is a completed allocation that started inside the
    requested window and has valid Submit, Start, positive elapsed time, and
    a positive *typed* GPU allocation. The normalized ratio is GPU-hour
    weighted: ``sum(wait_s) / sum(elapsed_s * gpus)``, i.e. wait-hours
    per allocated GPU-hour; seconds cancel. Aggregating totals instead of
    taking a median of per-job ratios keeps short jobs (a 96-second array
    element waiting 5 h is 202 h/GPU-h on its own) from outweighing the
    GPU-hours of large jobs. Prometheus is deliberately absent: short
    jobs need not survive a scrape to be counted.
    """
    waits = defaultdict(list)
    wait_sums = defaultdict(int)
    gpu_hour_sums = defaultdict(float)
    excluded = defaultdict(int)
    examined = 0
    for rec in records:
        examined += 1
        if rec.get("state") != "COMPLETED":
            excluded["state"] += 1
            continue
        started = _sacct_epoch(rec.get("start"))
        submitted = _sacct_epoch(rec.get("submit"))
        if started is None or submitted is None:
            excluded["timestamps"] += 1
            continue
        if not window_start <= started <= window_end:
            excluded["start_outside_window"] += 1
            continue
        if started < submitted:
            excluded["negative_wait"] += 1
            continue
        if (rec.get("elapsed_s") or 0) <= 0:
            excluded["nonpositive_elapsed"] += 1
            continue
        if (rec.get("gpus") or 0) <= 0 or not rec.get("gpu_type"):
            excluded["missing_typed_gpu_allocation"] += 1
            continue
        group = gpu_groups.job_gpu_group({
            "nodes": sorted(expand_node_list(rec.get("node_list", ""))),
            "gpu_type": rec["gpu_type"],
        }, node_gpu_types)
        wait = int(started - submitted)
        for name in (group, WAIT_TOTAL_KEY):
            waits[name].append(wait)
            wait_sums[name] += wait
            gpu_hour_sums[name] += rec["elapsed_s"] * rec["gpus"]

    summary = {}
    for name in set(waits) | {WAIT_TOTAL_KEY}:
        entry = _wait_statistics(waits.get(name, []))
        gpu_hours = gpu_hour_sums.get(name, 0.0)
        entry["wait_per_gpu_hour_weighted"] = (
            round(wait_sums[name] / gpu_hours, 2) if gpu_hours > 0 else None)
        summary[name] = entry
    accounting_coverage = accounting_coverage or {}
    coverage = {
        "records_examined": examined,
        "valid_samples": {name: len(samples) for name, samples in
                          waits.items() if name != WAIT_TOTAL_KEY},
        "excluded": dict(excluded),
        "failed_batches": accounting_coverage.get("failed_batches", 0),
        "complete": accounting_coverage.get("complete", True),
    }
    return summary, coverage
