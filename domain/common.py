"""Low-level helpers shared across every domain and route module.

Prometheus response shaping (series/window envelopes), the query-step
heuristic, and the batch-computed ``max/sum/count by`` aggregation are
used by jobs, partitions, vram, and every route's response body alike,
so they have no single domain owner — they live here instead.
"""

from datetime import datetime

import deps
from slurm import CLUSTER_TZ


def sacct_epoch(value):
    """A sacct Europe/Helsinki-naive string to epoch seconds; None when
    missing/invalid.

    sacct prints naive cluster-local (Europe/Helsinki) strings; interpret
    them on that wall clock, never the process's (deployment hosts vary).
    """
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if naive.tzinfo is not None:
        return naive.timestamp()
    return naive.replace(tzinfo=CLUSTER_TZ).timestamp()


def median(values):
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


def wait_percentiles(samples):
    """Wait summary of a list of waits in seconds: P50, P90, average and
    sample count, shared by the Partitions tab's completed-job waits and
    the Status tab's queue waits.

    P50 is the ordinary median (mean of the two middle waits when the
    sample count is even, integer-rounded). P90 is nearest-rank
    (ceil(0.90 * n)), so the reported percentile is always an observed
    wait. No samples reads as null figures with a 0 count — never a
    zero wait.
    """
    ordered = sorted(samples)
    if not ordered:
        return {"wait_p50_s": None, "wait_p90_s": None,
                "wait_avg_s": None, "wait_samples": 0}
    p90 = ordered[-(-9 * len(ordered) // 10) - 1]
    return {"wait_p50_s": median(ordered), "wait_p90_s": p90,
            "wait_avg_s": round(sum(ordered) / len(ordered)),
            "wait_samples": len(ordered)}


def step_for_range(seconds):
    if seconds <= 48 * 3600:
        return 120
    if seconds <= 3 * 86400:
        return 300
    if seconds <= 7 * 86400:
        return 600
    if seconds <= 31 * 86400:
        return 1800
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


def aggregate_by(series, labels, op, live=None):
    """Batch-computed stand-in for a PromQL ``max/sum/count by (labels)``
    aggregation over a range result (plan §2).

    The per-GPU raw window series feeds four PromQL aggregations today
    (Q1/Q3/Q4/Q5 — per-job max, partition summary, per-type sums and
    counts, each with a running-only matcher variant). Fetching the raw
    series ONCE per pinned window and computing these aggregations in
    process turns four upstream queries into one; this function produces
    Prometheus-shaped output the existing consumers run on unchanged.

    ``series`` items are parsed or raw prom range results (``values``
    entries may be ``["ts", "v"]`` pairs or already-parsed ``(ts, v)``
    tuples — both go through ``float()``). ``labels`` names the group-by
    labels; ``op`` is ``"max"``, ``"sum"`` or ``"count"`` (``count``
    counts the members holding a value at each timestamp). ``live``, when
    given, is the set of ``slurmjobid`` values to keep — the in-process
    replacement for the ``{slurmjobid=~…}`` matcher the running-only
    queries used. Output: one series per group (metric dict holding ONLY
    the group labels, values sorted by ts), groups with no values dropped.
    """
    members = {}  # group key -> {ts: [values]}
    for s in series:
        m = s.get("metric") or {}
        if live is not None and m.get("slurmjobid", "") not in live:
            continue
        key = tuple(m.get(label, "") for label in labels)
        for entry in s.get("values") or []:
            try:
                ts, v = float(entry[0]), float(entry[1])
            except (TypeError, ValueError):
                continue
            members.setdefault(key, {}).setdefault(ts, []).append(v)
    out = []
    for key, by_ts in members.items():
        values = []
        for ts in sorted(by_ts):
            vs = by_ts[ts]
            if op == "max":
                values.append((ts, max(vs)))
            elif op == "sum":
                values.append((ts, sum(vs)))
            else:  # count
                values.append((ts, float(len(vs))))
        if values:
            out.append({
                "metric": dict(zip(labels, key)),
                "values": values,
            })
    return out
