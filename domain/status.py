"""Status tab domain logic: the cluster's state right now, and its job
throughput over the past day and week — every job figure split into
CPU jobs and GPU jobs.

Two halves, fed by different sources:

- ``live_status`` — occupancy and node states from the scontrol node
  snapshot, running jobs from the scontrol job snapshot, pending jobs
  from squeue. Each source degrades on its own: an unreadable source
  yields null figures, never zeros.
- ``reduce_chunk`` / ``merge_history`` — the cluster-wide sacct history.
  Each day-aligned chunk of accounting rows is reduced at fetch time to
  hourly counters (so 170k raw rows a week are never cached), then the
  chunks merge into hourly series and day/week summaries.

A job is a GPU job when its request or allocation names at least one
GPU (sacct ReqTRES/AllocTRES, squeue tres_per_*/tres_req_str, scontrol
AllocTRES); everything else is a CPU job. Queue wait is Eligible ->
Start: the time a job spent queued for the scheduler, excluding time
blocked by a dependency, a hold or an array throttle.
"""

import re
from array import array
from collections import defaultdict

from domain.common import sacct_epoch, wait_percentiles
from slurm import array_task_count

HOUR = 3600

CLASSES = ("cpu", "gpu")

HISTORY_HOURS = 168
"""The history window: the past seven days, hour-aligned."""

DAY_HOURS = 24

PHANTOM_AGE_S = 30 * 86400
"""Fallback phantom cut-off when the live job set is unavailable: an
accounting row still "running" with a start older than this is treated
as a stale record, not a job holding resources."""

FAILED_STATES = frozenset({
    "FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL",
    "DEADLINE",
})
"""Final states the Status tab counts as failed: the job ended without
completing, for a reason other than being cancelled."""

# States in which a job has not finished yet: an End timestamp next to
# one of these (a requeued run's end, a completing job) is not a final
# outcome.
_NON_TERMINAL = frozenset({
    "PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "CONFIGURING",
    "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESIZING", "STAGE_OUT",
    "SIGNALING", "STOPPED", "SPECIAL_EXIT", "RESV_DEL_HOLD",
})

# scontrol job states that hold an allocation right now.
_RUNNING_STATES = frozenset({"RUNNING", "COMPLETING"})

# Node state tokens whose unallocated capacity cannot take new work —
# what ``sinfo %C`` reports as "other".
_UNAVAILABLE_NODE_STATES = frozenset({
    "DOWN", "DRAIN", "DRAINED", "DRAINING", "FAIL", "FAILING",
    "NOT_RESPONDING", "MAINT", "INVALID", "INVALID_REG", "FUTURE",
    "POWERED_DOWN", "POWERING_DOWN", "POWER_DOWN", "REBOOT_ISSUED",
    "UNKNOWN",
})

_ARRAY_RANGE = re.compile(r"_\[(.+)\]$")


# ---- classification -------------------------------------------------

def base_state(state):
    """A Slurm state's base token: ``CANCELLED by 1234`` -> CANCELLED,
    ``MIXED+DRAIN`` -> MIXED."""
    return (state or "").replace("+", " ").split(" ", 1)[0].strip("*").upper()


def outcome(state):
    """The Status tab's outcome group of a final state: completed,
    failed (``FAILED_STATES``), cancelled, or other (PREEMPTED, …)."""
    base = base_state(state)
    if base == "COMPLETED":
        return "completed"
    if base == "CANCELLED":
        return "cancelled"
    if base in FAILED_STATES:
        return "failed"
    return "other"


def array_tasks(jobid):
    """How many array tasks one accounting row stands for.

    A pending (or cancelled-while-pending) array keeps its not-yet-run
    tasks in one row, ``1234_[0-11%3]``; every task that started has a
    row of its own. Counting the range's tasks keeps "submitted" the
    same whether a task has started yet or not.
    """
    m = _ARRAY_RANGE.search(jobid or "")
    return array_task_count(m.group(1)) if m else 1


def job_class(gpus):
    """``"gpu"`` for a job that requested or holds a GPU, else ``"cpu"``."""
    return "gpu" if gpus and gpus > 0 else "cpu"


# ---- history: per-chunk reduction -------------------------------------

def _empty_cell():
    return {"submitted": 0, "started": 0, "ended": {}, "cpu_s": 0.0,
            "gpu_s": 0.0}


def reduce_chunk(records, lo, hi, now, live_ids):
    """Reduce one chunk's sacct status rows to hourly counters.

    ``[lo, hi)`` are the chunk's epoch bounds. sacct returns every job
    pending or running at any time inside them, so a job can appear in
    several chunks; each EVENT is counted only in the chunk its
    timestamp falls in, which makes summing chunks exact without any
    JobID dedupe:

    - submitted: Submit in the chunk, weighted by array tasks;
    - started: Start in the chunk, with its Eligible -> Start queue
      wait as a sample (only when Eligible is known and not after
      Start);
    - ended: End in the chunk with a final state, per base state,
      weighted by array tasks (a cancelled pending array);
    - CPU/GPU-seconds held: the job's [Start, End) interval clipped to
      the chunk. A row with no End ("still running") is clipped to
      ``now`` and counted only when its raw job ID is in ``live_ids``
      (the running jobs scontrol knows): sacct keeps stale "running"
      rows for jobs that died without an accounting record, and one of
      those would otherwise hold its cores in every hour of the window.
      With ``live_ids`` None (scontrol unavailable) rows started more
      than ``PHANTOM_AGE_S`` ago are treated as stale instead, and the
      rest are recorded as unverified.

    Returns ``{"lo", "hi", "records", "hours", "waits", "phantoms",
    "unverified"}`` — hours maps an hour's epoch to per-class counters,
    waits an hour's epoch to per-class ``array('l')`` samples, and the
    last two are the raw IDs of stale and unverified running rows.
    """
    hours = {}
    waits = {}
    phantoms = set()
    unverified = set()
    epoch_of = {}

    def epoch(text):
        # Array tasks share one Submit; memoize the zone conversion.
        if not text:
            return None
        if text not in epoch_of:
            epoch_of[text] = sacct_epoch(text)
        return epoch_of[text]

    def cell(ts, cls):
        hour = int(ts // HOUR) * HOUR
        per_hour = hours.get(hour)
        if per_hour is None:
            per_hour = hours[hour] = {c: _empty_cell() for c in CLASSES}
        return per_hour[cls]

    def hold(a, b, cls, ncpus, gpus):
        ts = int(a // HOUR) * HOUR
        while ts < b:
            seconds = min(b, ts + HOUR) - max(a, ts)
            c = cell(ts, cls)
            c["cpu_s"] += seconds * ncpus
            c["gpu_s"] += seconds * gpus
            ts += HOUR

    for r in records:
        cls = job_class(r.get("gpus"))
        tasks = array_tasks(r.get("jobid"))
        submit = epoch(r.get("submit"))
        if submit is not None and lo <= submit < hi:
            cell(submit, cls)["submitted"] += tasks
        start = epoch(r.get("start"))
        if start is not None and lo <= start < hi:
            cell(start, cls)["started"] += 1
            eligible = epoch(r.get("eligible"))
            if eligible is not None and eligible <= start:
                hour = int(start // HOUR) * HOUR
                per_hour = waits.setdefault(
                    hour, {c: array("l") for c in CLASSES})
                per_hour[cls].append(int(start - eligible))
        end = epoch(r.get("end"))
        state = base_state(r.get("state"))
        if (end is not None and lo <= end < hi
                and state not in _NON_TERMINAL):
            ended = cell(end, cls)["ended"]
            ended[state] = ended.get(state, 0) + tasks
        if start is None:
            continue
        if end is None:
            raw_id = r.get("jobid_raw") or r.get("jobid")
            if live_ids is not None:
                if raw_id not in live_ids:
                    phantoms.add(raw_id)
                    continue
            elif start < now - PHANTOM_AGE_S:
                phantoms.add(raw_id)
                continue
            else:
                unverified.add(raw_id)
            end = now
        a, b = max(start, lo), min(end, hi, now)
        if b > a:
            hold(a, b, cls, r.get("ncpus") or 0, r.get("gpus") or 0)
    return {"lo": lo, "hi": hi, "records": len(records), "hours": hours,
            "waits": waits, "phantoms": phantoms, "unverified": unverified}


# ---- history: merge ---------------------------------------------------

def _outcome_counts(states):
    """``{STATE: n}`` final-state counts to the four outcome groups."""
    out = {"completed": 0, "failed": 0, "cancelled": 0, "other": 0}
    for state, n in states.items():
        out[outcome(state)] += n
    return out


def _summary(cells, samples):
    """One window x class summary from its hourly cells and waits."""
    states = defaultdict(int)
    submitted = started = 0
    for c in cells:
        submitted += c["submitted"]
        started += c["started"]
        for state, n in c["ended"].items():
            states[state] += n
    groups = _outcome_counts(states)
    return {
        "submitted": submitted,
        "started": started,
        **groups,
        "ended": sum(groups.values()),
        "states": dict(sorted(states.items())),
        **wait_percentiles(samples),
    }


def merge_history(chunks, start, now, total_chunks, running_verified):
    """Merge reduced chunks into the Status tab's history payload.

    ``start`` is the hour-aligned window start, ``now`` its end.
    ``chunks`` holds only the chunks that fetched; an hour no fetched
    chunk covers reads as null in every series (a gap, never a zero)
    and is left out of the summaries, which ``coverage`` discloses.

    The week window is the whole history, ``[start, now]``; the day
    window starts 24 hours before the current hour. Both include the
    current, partial hour, so a "past 24 h" figure covers between 24
    and 25 hours — the response states the exact bounds.
    """
    current_hour = int(now // HOUR) * HOUR
    axis = list(range(start, current_hour + 1, HOUR))
    day_start = current_hour - DAY_HOURS * HOUR

    covered = set()
    hours = {}
    waits = {}
    phantoms, unverified = set(), set()
    records = 0
    for chunk in chunks:
        records += chunk["records"]
        phantoms |= chunk["phantoms"]
        unverified |= chunk["unverified"]
        hour = int(chunk["lo"] // HOUR) * HOUR
        while hour < chunk["hi"]:
            covered.add(hour)
            hour += HOUR
        for hour, per_class in chunk["hours"].items():
            merged = hours.setdefault(hour, {c: _empty_cell() for c in CLASSES})
            for cls, c in per_class.items():
                m = merged[cls]
                m["submitted"] += c["submitted"]
                m["started"] += c["started"]
                m["cpu_s"] += c["cpu_s"]
                m["gpu_s"] += c["gpu_s"]
                for state, n in c["ended"].items():
                    m["ended"][state] = m["ended"].get(state, 0) + n
        for hour, per_class in chunk["waits"].items():
            merged = waits.setdefault(hour, {c: [] for c in CLASSES})
            for cls, samples in per_class.items():
                merged[cls].extend(samples)
    # A row judged stale in one chunk and unverified in another (chunks
    # cached from fetches with and without the live job set) counts
    # once, as stale.
    unverified -= phantoms

    empty = _empty_cell()
    series = {}
    for cls in CLASSES:
        s = {k: [] for k in ("submitted", "started", "completed", "failed",
                             "cancelled", "other", "wait_avg_s", "wait_p50_s",
                             "cpus_alloc", "gpus_alloc")}
        for hour in axis:
            if hour not in covered:
                for values in s.values():
                    values.append(None)
                continue
            c = hours.get(hour, {}).get(cls, empty)
            s["submitted"].append(c["submitted"])
            s["started"].append(c["started"])
            for group, n in _outcome_counts(c["ended"]).items():
                s[group].append(n)
            w = wait_percentiles(waits.get(hour, {}).get(cls, ()))
            s["wait_avg_s"].append(w["wait_avg_s"])
            s["wait_p50_s"].append(w["wait_p50_s"])
            seconds = min(now, hour + HOUR) - hour
            if seconds > 0:
                s["cpus_alloc"].append(round(c["cpu_s"] / seconds, 1))
                s["gpus_alloc"].append(round(c["gpu_s"] / seconds, 2))
            else:
                s["cpus_alloc"].append(None)
                s["gpus_alloc"].append(None)
        series[cls] = s

    summary = {}
    for name, lo in (("day", day_start), ("week", start)):
        in_window = [h for h in axis if h >= lo and h in covered]
        per_class = {}
        all_samples = []
        for cls in CLASSES:
            cells = [hours[h][cls] for h in in_window if h in hours]
            samples = []
            for h in in_window:
                samples.extend(waits.get(h, {}).get(cls, ()))
            all_samples.extend(samples)
            per_class[cls] = _summary(cells, samples)
        per_class["all"] = _summary(
            [hours[h][cls] for h in in_window if h in hours for cls in CLASSES],
            all_samples)
        summary[name] = per_class

    failed_chunks = total_chunks - len(chunks)
    return {
        "available": bool(chunks),
        "window": {"start": start, "end": int(now)},
        "windows": {"day": {"start": day_start, "end": int(now)},
                    "week": {"start": start, "end": int(now)}},
        "summary": summary,
        "series": {"t": axis, **series},
        "coverage": {
            "chunks": total_chunks,
            "failed_chunks": failed_chunks,
            "complete": failed_chunks == 0,
            "records": records,
            "stale_running_rows": len(phantoms),
            "unverified_running_rows": len(unverified),
            "running_verified": running_verified,
        },
    }


# ---- live -------------------------------------------------------------

def _node_unavailable(node):
    """True when a node's unallocated capacity cannot take new work."""
    state = node.get("state_full") or node.get("state") or ""
    if "*" in state:  # the not-responding suffix
        return True
    tokens = {t.strip("*~#!%$@^-").upper() for t in state.split("+")}
    return bool(tokens & _UNAVAILABLE_NODE_STATES)


def _capacity(nodes, total_key, alloc_key):
    total = alloc = unavailable = 0
    for n in nodes:
        t = n.get(total_key) or 0
        a = min(n.get(alloc_key) or 0, t)
        total += t
        alloc += a
        if _node_unavailable(n):
            unavailable += t - a
    return {
        "alloc": alloc,
        "idle": total - alloc - unavailable,
        "unavailable": unavailable,
        "total": total,
        "occupancy_pct": round(alloc / total * 100, 1) if total else None,
    }


def _pending_class(job):
    return job_class(max(job.get("gpus") or 0, job.get("req_gpus") or 0))


def _pending_summary(jobs, now):
    out = {}
    for cls in CLASSES:
        rows = [j for j in jobs if _pending_class(j) == cls]
        ages = []
        for j in rows:
            eligible = sacct_epoch(j.get("eligible")) if j.get("eligible") else None
            if eligible is not None:
                ages.append(max(0, int(now - eligible)))
        out[cls] = {
            "jobs": len(rows),
            "tasks": sum(j.get("array_tasks") or 1 for j in rows),
            "eligible_jobs": len(ages),
            "blocked_jobs": len(rows) - len(ages),
            "eligible_age_avg_s": round(sum(ages) / len(ages)) if ages else None,
            "eligible_age_max_s": max(ages) if ages else None,
        }
    return out


def live_status(nodes, jobs, pending, now):
    """The cluster right now.

    ``nodes`` is the scontrol node snapshot, ``jobs`` the scontrol job
    snapshot (``{jobid: record}``), ``pending`` the squeue pending rows;
    each is None when its source failed, and every figure that needs it
    is then null — an unreadable source must never read as an idle
    cluster or an empty queue.

    Occupancy is allocated / total over every node (idle and
    unavailable included), the figure ``sinfo %C`` reports; capacity on
    down or draining nodes that is not allocated is "unavailable", the
    rest "idle". Running jobs split by class also carry the cores and
    GPUs they hold. Pending jobs are counted per record (one pending
    array is one job, its ``tasks`` its array size); a pending job is
    "eligible" once the scheduler may start it and "blocked" while a
    dependency or hold still keeps it back.

    Partition rows count a pending job in every partition it lists
    (non-additive across rows, like the Partitions tab's flexible
    jobs); a running job has exactly one partition.
    """
    out = {
        "time": int(now),
        "nodes_available": nodes is not None,
        "jobs_available": jobs is not None,
        "queue_available": pending is not None,
        "cpu": None, "gpu": None, "nodes": None,
        "running": None, "pending": None, "pending_reasons": None,
        "partitions": None,
    }
    running = None
    if jobs is not None:
        running = [j for j in jobs.values()
                   if base_state(j.get("state")) in _RUNNING_STATES]
        out["running"] = {
            cls: {
                "jobs": sum(1 for j in running if job_class(j.get("gpus")) == cls),
                "cpus": sum(j.get("ncpus") or 0 for j in running
                            if job_class(j.get("gpus")) == cls),
                "gpus": sum(j.get("gpus") or 0 for j in running
                            if job_class(j.get("gpus")) == cls),
            }
            for cls in CLASSES
        }
    if pending is not None:
        out["pending"] = _pending_summary(pending, now)
        reasons = defaultdict(lambda: {c + "_jobs": 0 for c in CLASSES})
        for j in pending:
            reasons[j.get("reason") or "None"][_pending_class(j) + "_jobs"] += 1
        out["pending_reasons"] = sorted(
            ({"reason": r, **counts} for r, counts in reasons.items()),
            key=lambda r: (-(r["cpu_jobs"] + r["gpu_jobs"]), r["reason"]))
    if nodes is None:
        return out

    out["cpu"] = _capacity(nodes, "cpus", "cpus_alloc")
    out["gpu"] = _capacity(nodes, "gpus", "gpus_alloc")
    by_state = defaultdict(lambda: {"cpu_nodes": 0, "gpu_nodes": 0})
    for n in nodes:
        by_state[base_state(n.get("state_full") or n.get("state"))][
            ("gpu" if n.get("gpus") else "cpu") + "_nodes"] += 1
    out["nodes"] = {
        "total": len(nodes),
        "unavailable": sum(1 for n in nodes if _node_unavailable(n)),
        "by_state": [{"state": s, **c} for s, c in sorted(by_state.items())],
    }

    parts = {}
    for n in nodes:
        for name in filter(None, (n.get("partitions") or "").split(",")):
            p = parts.setdefault(name, {
                "name": name, "nodes": 0, "cpus_alloc": 0, "cpus_total": 0,
                "gpus_alloc": 0, "gpus_total": 0})
            p["nodes"] += 1
            p["cpus_total"] += n.get("cpus") or 0
            p["cpus_alloc"] += min(n.get("cpus_alloc") or 0, n.get("cpus") or 0)
            p["gpus_total"] += n.get("gpus") or 0
            p["gpus_alloc"] += min(n.get("gpus_alloc") or 0, n.get("gpus") or 0)
    running_by_part = defaultdict(int)
    for j in running or ():
        running_by_part[j.get("partition") or ""] += 1
    pending_by_part = defaultdict(int)
    for j in pending or ():
        for name in set(filter(None, (j.get("partition") or "").split(","))):
            pending_by_part[name] += 1
    rows = []
    for name in sorted(parts):
        p = parts[name]
        p["kind"] = "gpu" if p["gpus_total"] else "cpu"
        p["running_jobs"] = running_by_part[name] if running is not None else None
        p["pending_jobs"] = pending_by_part[name] if pending is not None else None
        rows.append(p)
    out["partitions"] = rows
    return out
