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


def partition_window(since_hours, running_only=False, now=None,
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
    start, now = job_window(since_hours, now)
    step = step_for_range(now - start)
    sel = ""
    if running_only:
        live = running_gpu_job_ids()
        if not live:
            return [], {}, {}, {}, start, now, step
        sel = selector(label_in("slurmjobid", live))

    def fetch():
        stats = deps.get_prom().query_range(
            "max by (slurmjobid, instance, job, gpu_type) "
            "(slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        trend = deps.get_prom().query_range(
            "avg by (job, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        # Concurrent allocated GPUs per group, for the window-average
        # occupancy chart (same selector so running-only matches here too).
        occ = deps.get_prom().query_range(
            "count by (job, gpu_type) (slurm_job_utilization_gpu%s)" % sel,
            start, now, step,
        )
        return stats, trend, occ, start, now, step

    key = cache.partition_window_key(since_hours, running_only)
    stats, trend, occ, start, now, step = deps.route_cache.get_or_set(key, 60, fetch)
    # One canonical group name per (job, gpu_type) pair, derived from the
    # summary series' instances so summary, trend, and occupancy agree.
    aliases = gpu_groups.pair_aliases(stats, node_gpu_types)
    out = aggregate_partition_stats(stats, node_gpu_types, aliases)
    trend_out = {
        gpu_groups.gpu_group_name(s["metric"], node_gpu_types, aliases):
        series_values(s)
        for s in trend
    }
    # Window-average allocated GPU count per group, for mean occupancy.
    occupancy = {}
    for s in occ:
        values = series_values(s)
        if values:
            name = gpu_groups.gpu_group_name(s["metric"], node_gpu_types, aliases)
            occupancy[name] = sum(v for _, v in values) / len(values)
    # Observed instances per group, for the capacity join in api_partitions.
    instances = {}
    for s in stats:
        m = s["metric"]
        inst = m.get("instance", "")
        if inst:
            name = gpu_groups.gpu_group_name(m, node_gpu_types, aliases)
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
        name = gpu_groups.gpu_group_name(m, node_gpu_types, aliases)
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


def gpu_capacity(groups, instances, nodes, allocs):
    """Join metric groups to scontrol GPU capacity.

    ``instances`` maps group -> observed instance names (built in
    ``partition_window``). Capacity is summed over **all** scontrol nodes
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
            if (n["gpus"] and not gpu_groups.is_mig_gres(n.get("gpu_type"))
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
