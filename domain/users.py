"""User-category aggregation: Ellis versus Non-Ellis classification of
the cluster's completed GPU jobs.

The classification boundary lives in ``deps.user_in_group`` (the one
NSS-aware function, patched by tests); this module caches its result per
(username, group) pair and aggregates the shared bounded accounting scan
(``domain.partitions.completed_job_window``'s records) into the two
category rows the Users tab renders above the per-user table.

The cohort is one explicit thing: completed accounting allocations with a
positive elapsed time and GPU count that started inside the fetched
accounting window. Prometheus never changes the cohort — it only adds the
utilization column for the jobs it happened to observe.
"""

from collections import defaultdict

import cache
import deps
from domain.partitions import sacct_epoch, wait_statistics

ELLIS_CATEGORY = "Ellis"
NON_ELLIS_CATEGORY = "Non-Ellis"

CATEGORIES = (ELLIS_CATEGORY, NON_ELLIS_CATEGORY)

GROUP_NAME = "ellis"
"""The cluster's exact NSS group; no prefix/suffix variants count."""

_CLASSIFY_TTL = 300
"""Seconds a username's membership answer stays cached in route_cache."""


def classify_user(username, group_name=GROUP_NAME):
    """The public category label for one username, cached per pair.

    Concurrent /api/users and /api/users/categories requests for the same
    user share one directory-service lookup instead of repeating it. An
    NSS-absent username classifies as Non-Ellis here (deps.user_in_group
    already returns False for it); the missing-group 503 propagates.
    """
    def fetch():
        return (ELLIS_CATEGORY if deps.user_in_group(username, group_name)
                else NON_ELLIS_CATEGORY)
    return deps.route_cache.get_or_set(
        cache.user_group_key(username, group_name), _CLASSIFY_TTL, fetch)


def require_group(group_name=GROUP_NAME):
    """Fail fast when the classification boundary cannot resolve the
    category's group at all.

    A missing group makes Ellis-versus-Non-Ellis UNKNOWABLE — silently
    labeling everyone Non-Ellis would fabricate data — so both user
    endpoints preflight this and surface the boundary's explicit 503 even
    when the window's cohort is empty (no qualifying records means no
    per-user classify call would ever touch NSS). The probe goes through
    the same ``deps.user_in_group`` boundary with a username no member
    list can contain, so no NSS logic is duplicated here.
    """
    probe = "__ellis_group_probe__"
    deps.user_in_group(probe, group_name)


def _prometheus_join_ids(record):
    """The record's accounting ID(s) that can match a Prometheus series'
    slurmjobid label: the raw numeric ID first, the display ID for
    fixtures/older records without one."""
    ids = []
    for key in ("jobid_raw", "jobid"):
        value = (record.get(key) or "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def completed_category_summary(records, prometheus_jobs, window_start,
                               window_end, accounting_coverage=None):
    """Ellis/Non-Ellis totals from the bounded sacct records.

    A qualifying job is one completed accounting allocation with a
    non-empty user, a parseable start inside the inclusive fetched
    accounting window, positive elapsed time, and a positive GPU count;
    it counts once and adds ``elapsed_s * gpus / 3600`` allocated
    GPU-hours. Jobs with a parseable submit not later than start also
    contribute their Submit->Start wait to the category's samples; jobs
    lacking valid submit data still count for Jobs/GPU h but never for
    the wait columns. Malformed records are rejected by reason (counted
    in the returned coverage) instead of being allowed to alter any
    denominator.

    ``prometheus_jobs`` are the window's job dicts with the internal
    ``_util_sum``/``_util_samples`` aggregands; only series whose
    slurmjobid matches the qualifying cohort's raw/fallback ID feed the
    sample-weighted mean utilization, and a category none of whose jobs
    was observed reads null — missing scrape coverage, not a zero.
    """
    waits = {name: [] for name in CATEGORIES}
    wait_sums = {name: 0 for name in CATEGORIES}
    wait_gpu_seconds = {name: 0 for name in CATEGORIES}
    gpu_seconds = {name: 0 for name in CATEGORIES}
    jobs = {name: 0 for name in CATEGORIES}
    excluded = defaultdict(int)

    cohort_ids = {name: set() for name in CATEGORIES}
    for rec in records:
        if rec.get("state") != "COMPLETED":
            excluded["state"] += 1
            continue
        started = sacct_epoch(rec.get("start"))
        if started is None:
            excluded["timestamps"] += 1
            continue
        if not window_start <= started <= window_end:
            excluded["start_outside_window"] += 1
            continue
        if (rec.get("elapsed_s") or 0) <= 0:
            excluded["nonpositive_elapsed"] += 1
            continue
        if (rec.get("gpus") or 0) <= 0:
            excluded["nonpositive_gpus"] += 1
            continue
        user = rec.get("user") or ""
        if not user:
            excluded["missing_user"] += 1
            continue
        category = classify_user(user)
        jobs[category] += 1
        gpu_seconds[category] += rec["elapsed_s"] * rec["gpus"]
        cohort_ids[category].update(_prometheus_join_ids(rec))
        # A job lacking valid submit data keeps its Jobs/GPU-h
        # contribution but never invents a wait sample — and must not
        # dilute the wait ratio either: the wait/GPU-hour denominator
        # covers ONLY the wait-valid subset, the partition table's
        # established formula.
        submitted = sacct_epoch(rec.get("submit"))
        if submitted is not None and submitted <= started:
            wait = int(started - submitted)
            waits[category].append(wait)
            wait_sums[category] += wait
            wait_gpu_seconds[category] += rec["elapsed_s"] * rec["gpus"]

    util_sum = {name: 0.0 for name in CATEGORIES}
    util_samples = {name: 0 for name in CATEGORIES}
    for series in prometheus_jobs or []:
        jid = str(series.get("jobid", ""))
        if not jid:
            continue
        for name in CATEGORIES:
            if jid in cohort_ids[name]:
                util_sum[name] += series.get("_util_sum", 0.0)
                util_samples[name] += series.get("_util_samples", 0)
                break

    rows = []
    for name in CATEGORIES:
        stats = wait_statistics(waits[name])
        gpu_hours = gpu_seconds[name] / 3600.0
        rows.append({
            "category": name,
            "jobs": jobs[name],
            "gpu_hours": round(gpu_hours, 2),
            "wait_per_gpu_hour": (
                round(wait_sums[name] / wait_gpu_seconds[name], 2)
                if wait_gpu_seconds[name] else None),
            "wait_p50_s": stats["wait_p50_s"],
            "wait_p90_s": stats["wait_p90_s"],
            "wait_avg_s": stats["wait_avg_s"],
            "wait_samples": stats["wait_samples"],
            "mean_util": (round(util_sum[name] / util_samples[name], 2)
                          if util_samples[name] else None),
        })

    accounting_coverage = accounting_coverage or {}
    coverage = {
        "records_examined": len(records),
        # WaitHistoryCoverage semantics: per-key wait-sample counts.
        "valid_samples": {name: len(waits[name]) for name in CATEGORIES},
        "excluded": excluded,
        "failed_batches": accounting_coverage.get("failed_batches", 0),
        "complete": accounting_coverage.get("complete", True),
    }
    return rows, coverage
