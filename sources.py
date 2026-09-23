"""Shared external data sources: one owner per external read (plan §1).

Every route used to fetch its own Prometheus range queries, scontrol
snapshots and sacct dumps, so the same upstream data was pulled again
and again — the per-GPU utilization window alone ran once per tab, and
wait history and the VRAM enrichment each ran their own sacct calls.
This module gives each external read exactly one owner function with
its own cache identity, TTL and single-flight; a later commit rewires
``domain/`` and ``api/`` onto it so every tab computes its own view
from one shared fetch.

The raw calls still go through ``deps.*`` (``deps.get_prom()``,
``deps.sacct_allocations(...)``, ``deps.now()``, ``deps.route_cache``),
never by importing the underlying name directly, so the test seams stay
exactly where they are today.
"""

import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import cache
import deps
import gpu_groups
from domain.common import step_for_range
from slurm import CLUSTER_TZ, SlurmError

# The per-GPU raw series every Jobs/Users/partition view derives from
# (plan §2): one query replaces Q1's four aggregations, each of which is
# computed in process by domain.common.aggregate_by. The exporter
# publishes one series per allocated GPU with a job-local ``gpu`` label.
_GPU_UTIL_QUERY = (
    "max by (slurmjobid, instance, gpu, job, user, gpu_type) "
    "(slurm_job_utilization_gpu)"
)
_VRAM_PCT_QUERY = (
    "avg by (slurmjobid, instance, gpu) "
    "(slurm_job_memory_usage_gpu / slurm_job_memory_total_gpu * 100)"
)
# Q6 without any matcher; running-only is filtered in process later.
_VRAM_GB_QUERY = (
    "max by (slurmjobid, instance, gpu) "
    "(slurm_job_memory_usage_gpu / 1073741824)"
)


def pinned_window(since_hours):
    """The ``(start, end, step)`` triple every window source keys on.

    Cached for the same 60 s as window data: all tabs opened within one
    TTL read sources fetched for identical bounds, and each response
    reports the window its data actually covers (plan §1) instead of
    every route recomputing bounds from a clock that has moved on.
    """
    def compute():
        now = int(deps.now())
        start = now - int(since_hours * 3600)
        return start, now, step_for_range(now - start)

    return deps.route_cache.get_or_set(
        cache.pinned_window_key(since_hours), 60, compute)


def gather(*thunks):
    """Run thunks in parallel, returning their results in thunk order.

    Each call gets its own ``ThreadPoolExecutor`` (no shared pool), so a
    thunk that itself fans out via ``gather`` cannot deadlock waiting on
    a pool slot held by its own caller. The first exception propagates;
    other finished results are simply discarded. A single thunk runs
    inline — no pool, no thread switch.
    """
    thunks = list(thunks)
    if not thunks:
        return []
    if len(thunks) == 1:
        return [thunks[0]()]
    with ThreadPoolExecutor(max_workers=len(thunks)) as pool:
        futures = [pool.submit(t) for t in thunks]
        return [f.result() for f in futures]


def _range_pair(entry):
    """One ``["ts", "v"]`` / ``(ts, v)`` pair to parsed floats, or None
    when unparseable or NaN (the skips of domain.common.series_values,
    plus NaN, which would poison max/sum aggregations downstream)."""
    try:
        ts, v = float(entry[0]), float(entry[1])
    except (TypeError, ValueError):
        return None
    return None if v != v else (ts, v)


def _parse_range_result(result):
    """A Prometheus range result to parsed series
    ``[{"metric": m, "values": [(ts, v), ...]}, ...]``.

    Values become floats once, here at the source (plan §2), so every
    consumer's per-sample conversion is free. Unparseable or NaN samples
    are skipped, leaving the series in place.
    """
    out = []
    for s in result:
        values = [p for p in (_range_pair(e) for e in s.get("values") or ())
                  if p is not None]
        out.append({"metric": s.get("metric") or {}, "values": values})
    return out


def _parse_instant_result(result):
    """A Prometheus instant result to ``[(metric, (ts, v)), ...]``,
    skipping series whose value does not parse (or is NaN)."""
    out = []
    for s in result:
        pair = _range_pair(s.get("value") or ())
        if pair is not None:
            out.append((s.get("metric") or {}, pair))
    return out


def _window_source(name, query, win):
    """One cached Prometheus range fetch, keyed by the pinned window it
    was fetched for. All tabs within one pinned-window TTL share the
    fetch; a re-pinned window addresses a different key."""
    start, end, step = win

    def fetch():
        return _parse_range_result(
            deps.get_prom().query_range(query, start, end, step))

    return deps.route_cache.get_or_set(
        cache.window_source_key(name, start, end, step), 60, fetch)


def gpu_util(win):
    """Per-GPU raw utilization series for the pinned window."""
    return _window_source("gpu_util", _GPU_UTIL_QUERY, win)


def vram_pct(win):
    """Per-GPU mean VRAM % series for the pinned window (Q2, unchanged)."""
    return _window_source("vram_pct", _VRAM_PCT_QUERY, win)


def vram_gb(win):
    """Per-GPU peak VRAM GB series for the pinned window (Q6 without a
    matcher; running-only is filtered in process later)."""
    return _window_source("vram_gb", _VRAM_GB_QUERY, win)


def live_snapshot(node_gpu_types=None):
    """Everything the current-instant views need, from two queries.

    ``node_current`` used to make four instant queries and the live-ID
    check a fifth (plan §1). Two parallel queries — the per-GPU
    utilization snapshot and the per-node VRAM average — cover all of
    them: the exporter publishes one utilization series per allocated
    GPU, so counting series per node IS the allocation count, and the
    ``gpu`` label is job-local (every 1-GPU job says gpu="0") and must
    never be used for that accounting (the why from
    domain.partitions.node_current). Cached 30 s under one key so
    running-only views and the Nodes view read the same instant.

    The derivation assumes ``node_gpu_types`` is the same node index for
    every caller within the TTL — in practice each builds it from the
    30 s-cached ``scontrol_nodes()`` snapshot — and the returned dict is
    shared by all callers of one TTL: treat it as read-only.

    Returns ``{"live_ids", "node_util", "node_vram", "jobs_by_node",
    "allocs_by_node", "allocs_by_group"}``.
    """
    node_gpu_types = node_gpu_types or {}

    def fetch():
        def per_gpu():
            return _parse_instant_result(
                deps.get_prom().query_instant(_GPU_UTIL_QUERY))

        def node_vram_q():
            return _parse_instant_result(
                deps.get_prom().query_instant(
                    "avg by (instance) (slurm_job_memory_usage_gpu / "
                    "slurm_job_memory_total_gpu * 100)"))

        per_gpu, vram = gather(per_gpu, node_vram_q)

        live_ids = set()
        node_util = {}
        jobs_acc = {}  # (instance, slurmjobid, job, user) -> max util
        allocs_by_node = {}
        allocs_by_group = {}
        for m, (_, v) in per_gpu:
            jobid = m.get("slurmjobid", "")
            if jobid:
                live_ids.add(jobid)
            inst = m.get("instance", "")
            if not inst:
                continue
            node_util[inst] = max(node_util.get(inst, v), v)
            gkey = (inst, jobid, m.get("job", ""), m.get("user", ""))
            jobs_acc[gkey] = max(jobs_acc.get(gkey, v), v)
            # One exporter series per allocated GPU (see docstring), so
            # the per-series count attributes allocations exactly like
            # today's ``count by (instance, job, gpu_type)`` query.
            allocs_by_node[inst] = allocs_by_node.get(inst, 0) + 1
            group = gpu_groups.gpu_group_name(m, node_gpu_types)
            allocs_by_group[group] = allocs_by_group.get(group, 0) + 1
        jobs_by_node = {}
        for (inst, jobid, job, user), util in jobs_acc.items():
            jobs_by_node.setdefault(inst, []).append(
                {"jobid": jobid, "job": job, "user": user, "util": util})
        return {
            "live_ids": live_ids,
            "node_util": node_util,
            "node_vram": {m.get("instance", ""): v for m, (_, v) in vram},
            "jobs_by_node": jobs_by_node,
            "allocs_by_node": allocs_by_node,
            "allocs_by_group": allocs_by_group,
        }

    return deps.route_cache.get_or_set(cache.snapshot_key(), 30, fetch)


def scontrol_nodes():
    """The 30 s-cached ``scontrol show nodes`` snapshot."""
    return deps.route_cache.get_or_set(
        cache.scontrol_nodes_key(), 30, deps.show_nodes)


def scontrol_jobs():
    """The 30 s-cached ``scontrol show job -o`` snapshot."""
    return deps.route_cache.get_or_set(
        cache.scontrol_jobs_key(), 30, deps.show_jobs)


def _helsinki_chunks(start_local, end_local):
    """Chunk bounds aligned to Europe/Helsinki midnights:
    ``[window_start, first_midnight)``, full days,
    ``[last_midnight, window_end)``. Naive local datetimes in, same out;
    degenerate windows (start >= end, or both bounds inside one day)
    yield a single partial chunk.
    """
    midnight = datetime.time.min
    first_midnight = datetime.datetime.combine(
        start_local.date() + datetime.timedelta(days=1), midnight)
    last_midnight = datetime.datetime.combine(end_local.date(), midnight)
    chunks = []
    cursor = start_local
    if cursor < end_local and cursor < first_midnight:
        edge_end = min(first_midnight, end_local)
        chunks.append((cursor, edge_end))
        cursor = edge_end
    while cursor < last_midnight and cursor < end_local:
        # cursor is on a midnight here; the next midnight never passes
        # last_midnight (which is end_local's own midnight at most).
        chunk_end = cursor + datetime.timedelta(days=1)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    if cursor < end_local:
        chunks.append((cursor, end_local))
    return chunks


def sacct_window(since_hours, progress=None, progress_key=None):
    """One window-wide sacct allocation dump, shared by wait history and
    VRAM enrichment (plan §3).

    Instead of every route running its own ``sacct`` calls, the window's
    allocations are fetched once as day-aligned chunks (Helsinki
    midnights): the two edge chunks — which touch the moving window
    bounds, so running jobs' Elapsed/State must be current — are always
    fetched fresh when the 300 s window entry expires, while full past
    days are immutable and cached per day for 1 h, shared across every
    window size that covers them. Chunks run on four workers, each
    retried once; a chunk that fails twice is counted in the coverage
    without discarding the others. A job spanning midnight appears in
    both chunks; its final state lives in the later chunk, so duplicates
    are dropped by JobID keeping the NEWEST chunk's row.

    Returns ``(records, coverage, start, end)`` where start/end are the
    epoch bounds actually fetched — cached alongside the records so a
    later hit filters against these bounds, not bounds recomputed from a
    newer clock (the queue route's "cache the fetched bounds" behavior).

    ``progress_key`` scopes progress publication (see cache.progress_store):
    only the cache-miss leader that actually assembles the dump seeds,
    updates and clears the key; a follower that joins via the route-cache
    Future never touches it.
    """

    def fetch():
        now = deps.now()
        start, end = now - since_hours * 3600, now
        start_local = datetime.datetime.fromtimestamp(
            start, CLUSTER_TZ).replace(tzinfo=None)
        end_local = datetime.datetime.fromtimestamp(
            end, CLUSTER_TZ).replace(tzinfo=None)
        chunks = _helsinki_chunks(start_local, end_local)
        # GPU partitions only, from the cached scontrol snapshot: CPU
        # jobs never enter the dump (plan §3). An empty list omits -r.
        partitions = sorted(gpu_groups.partition_gpu_types(scontrol_nodes()))
        total = len(chunks)

        if progress_key is not None:
            cache.progress_store[progress_key] = {
                "done": 0, "total": total, "failed_batches": 0}

        def report(done, failed):
            state = {"done": done, "total": total, "failed_batches": failed}
            if progress_key is not None:
                cache.progress_store[progress_key] = state
            if progress is not None:
                progress(state)

        # Seed both publishers with the initial batched-progress state
        # (done=0), exactly like slurm.completed_jobs reports today.
        report(0, 0)

        def fetch_fresh(chunk_start_iso, chunk_end_iso):
            # Mirror the per-chunk retry of slurm.completed_jobs: one
            # retry on SlurmError, then the chunk counts as failed.
            for attempt in range(2):
                try:
                    return deps.sacct_allocations(
                        chunk_start_iso, chunk_end_iso, partitions)
                except SlurmError:
                    if attempt:
                        raise

        def load_chunk(index):
            chunk_start, chunk_end = chunks[index]
            chunk_start_iso = chunk_start.isoformat(timespec="seconds")
            chunk_end_iso = chunk_end.isoformat(timespec="seconds")
            # Only the two edge chunks (first and last — the ones touching
            # the moving window bounds) are fetched fresh; full past days
            # are immutable and shared through the per-day cache.
            is_edge = index == 0 or index == total - 1

            def fresh():
                return fetch_fresh(chunk_start_iso, chunk_end_iso)

            if is_edge:
                return fresh()
            return deps.route_cache.get_or_set(
                cache.day_chunk_key(chunk_start_iso), 3600, fresh)

        try:
            results = {}
            failed_batches = successful_batches = 0
            if chunks:
                with ThreadPoolExecutor(
                        max_workers=min(4, total)) as pool:
                    futures = {pool.submit(load_chunk, i): i
                               for i in range(total)}
                    done = 0
                    for future in as_completed(futures):
                        index = futures[future]
                        try:
                            records = future.result()
                        except SlurmError:
                            records = None
                        done += 1
                        if records is None:
                            failed_batches += 1
                        else:
                            successful_batches += 1
                            results[index] = records
                        report(done, failed_batches)
            # Newest chunk wins: a midnight-spanning job's final State and
            # Elapsed live in the later chunk that contains its end.
            records, seen = [], set()
            for index in sorted(results, reverse=True):
                for record in results[index]:
                    if record["jobid"] not in seen:
                        seen.add(record["jobid"])
                        records.append(record)
            coverage = {
                "failed_batches": failed_batches,
                "successful_batches": successful_batches,
                "complete": failed_batches == 0,
            }
            return records, coverage, start, end
        finally:
            # Failed fetches clear too: a stale in-flight entry would
            # otherwise read as live progress on every later poll.
            if progress_key is not None:
                cache.progress_store.pop(progress_key, None)

    return deps.route_cache.get_or_set(
        cache.sacct_window_key(since_hours), 300, fetch)


_NON_TERMINAL_STATES = frozenset({
    "PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "REQUEUED",
    "RESIZING", "STAGE_OUT", "CONFIGURING",
})

_sacct_row_cache = cache.KeyedBatchCache()


def _is_terminal(state):
    """True when a sacct state's base token (before ``+`` or space) is
    not one of the still-moving states; a terminal row's data cannot
    change, so its per-ID cache entry lives far longer."""
    base = (state or "").replace("+", " ").split(" ", 1)[0]
    return base not in _NON_TERMINAL_STATES


def _rows_for_id(id, rows_by_ref):
    """The deduplicated enriched rows belonging to one requested ID.

    A row belongs when its sacct notation jobid equals the ID, its raw
    ID does (the Prometheus ``slurmjobid`` spelling), or it is one of
    the ID's array tasks (``parent_task``). The same row is indexed
    under both its jobid and jobid_raw by the -j lookup, so dedupe on
    that pair.
    """
    seen, out = set(), []
    for row in rows_by_ref:
        key = (row.get("jobid", ""), row.get("jobid_raw", ""))
        if key in seen:
            continue
        jobid = key[0]
        if (jobid == id or key[1] == id
                or (id and jobid.startswith(id + "_"))):
            seen.add(key)
            out.append(row)
    out.sort(key=lambda r: (r["jobid"], r.get("jobid_raw", "")))
    return out


def _sacct_rows_ttl(id, rows):
    if not rows:
        return 300
    if all(_is_terminal(r.get("state")) for r in rows):
        return 3600
    return 300


def sacct_rows(ids, workers=2, progress=None):
    """Per-ID sacct row cache (plan §1): ``{id: [rows] or None}``.

    sacct results were cached per whole ID set, so the Jobs list, job
    detail, the VRAM enrichment and the node job-start lookup never
    shared rows; here each requested ID is its own cache entry with its
    own TTL — 1 h when every row of that ID is in a terminal state, 300 s
    otherwise. IDs another caller is fetching are joined in flight; the
    remaining ones go to ``deps.sacct_jobs_resilient`` as ONE whole
    missing set (it batches by 100 internally and reports progress per
    batch, preserving that contract).

    Rows come back as the enriched dicts belonging to the ID (its own
    row, its JobIDRaw spelling, and its array tasks), deduplicated.
    An ID whose rows could not be fetched — a failed batch omits them —
    resolves to ``None`` and stays UNCACHED, so the next request retries
    it; a successful call's genuinely row-less IDs are cached as ``[]``
    for 300 s.
    """

    def fetch_missing(missing):
        enriched, failed_batches = deps.sacct_jobs_resilient(
            list(missing), workers=workers, progress=progress)
        out = {}
        for id in missing:
            rows = _rows_for_id(id, enriched.values())
            if rows or failed_batches == 0:
                # Never negative-cache a failure: with failed batches the
                # row-less IDs stay uncached so the next request retries.
                out[id] = rows
        return out

    return _sacct_row_cache.get_batch(
        [str(i) for i in ids], fetch_missing, _sacct_rows_ttl)
