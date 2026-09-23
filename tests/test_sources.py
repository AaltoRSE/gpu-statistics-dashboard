"""Unit tests for the shared-source layer (sources.py) and the batch
aggregator in domain/common.py. Run: .venv/bin/python -m pytest tests/ -q

Light coverage here — the call-count and equivalence guarantees get their
deep pass when domain/ and api/ are rewired onto this layer.
"""

import datetime
import os
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import cache  # noqa: E402
import deps  # noqa: E402
import sources  # noqa: E402
from domain.common import aggregate_by  # noqa: E402
from slurm import SlurmError  # noqa: E402

HELSINKI = ZoneInfo("Europe/Helsinki")
# 2026-09-23T15:30:00 Helsinki (a Wednesday) — fixed clock for window tests.
NOW = datetime.datetime(2026, 9, 23, 15, 30, tzinfo=HELSINKI).timestamp()


class StubProm:
    """Records queries, answers from canned results by fragment match."""

    def __init__(self, range_results=None, instant_results=None):
        self.range_calls = []
        self.instant_calls = []
        self.range_results = range_results or {}
        self.instant_results = instant_results or {}

    def query_range(self, query, start, end, step):
        self.range_calls.append((query, start, end, step))
        for fragment, result in self.range_results.items():
            if fragment in query:
                return result
        return []

    def query_instant(self, query, time=None):
        self.instant_calls.append(query)
        for fragment, result in self.instant_results.items():
            if fragment in query:
                return result
        return []


def test_gather_preserves_thunk_order_despite_completion_order():
    order = []

    def slow(tag, delay):
        time.sleep(delay)
        order.append(tag)
        return tag

    results = sources.gather(lambda: slow("slow", 0.05),
                             lambda: slow("fast", 0.0))
    assert results == ["slow", "fast"]  # thunk order, not completion order
    assert order == ["fast", "slow"]


def test_gather_propagates_exception():
    def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sources.gather(lambda: "ok", boom, lambda: "never")


def test_gather_empty_and_single():
    assert sources.gather() == []
    marker = []
    result = sources.gather(lambda: (marker.append(1) or "inline"))
    assert result == ["inline"]
    assert marker == [1]


def test_pinned_window_caches_triple_and_skips_second_clock_read(
        monkeypatch):
    reads = []
    monkeypatch.setattr(deps, "now", lambda: reads.append(1) or NOW)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    win = sources.pinned_window(24)
    assert win == (int(NOW) - 24 * 3600, int(NOW), 120)  # 24h -> 120s step
    again = sources.pinned_window(24)
    assert again == win
    assert len(reads) == 1  # served from the cache, no new clock read
    assert cache.pinned_window_key(24) in deps.route_cache._store


def test_window_sources_parse_and_cache_per_window(monkeypatch):
    prom = StubProm(range_results={
        "slurm_job_utilization_gpu": [
            {"metric": {"slurmjobid": "7", "instance": "gpu1", "gpu": "0"},
             "values": [["1000", "40"], ["1100", "NaN"], ["1200", "x"]]},
        ],
    })
    monkeypatch.setattr(deps, "get_prom", lambda: prom)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    win = (1000, 1300, 100)
    series = sources.gpu_util(win)
    assert prom.range_calls == [
        ("max by (slurmjobid, instance, gpu, job, user, gpu_type) "
         "(slurm_job_utilization_gpu)", 1000, 1300, 100)]
    # Parsed to floats at the source; NaN and unparseable samples skipped.
    assert series == [{"metric": {"slurmjobid": "7", "instance": "gpu1",
                                  "gpu": "0"},
                       "values": [(1000.0, 40.0)]}]
    assert sources.gpu_util(win) is series  # cache hit, no second query
    assert len(prom.range_calls) == 1
    # A re-pinned window addresses a different key and re-queries.
    sources.gpu_util((1000, 1400, 100))
    assert len(prom.range_calls) == 2


def test_vram_sources_queries(monkeypatch):
    prom = StubProm()
    monkeypatch.setattr(deps, "get_prom", lambda: prom)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    win = (1000, 1300, 100)
    sources.vram_pct(win)
    sources.vram_gb(win)
    queries = [c[0] for c in prom.range_calls]
    assert ("avg by (slurmjobid, instance, gpu) "
            "(slurm_job_memory_usage_gpu / slurm_job_memory_total_gpu * 100)"
            ) in queries
    assert ("max by (slurmjobid, instance, gpu) "
            "(slurm_job_memory_usage_gpu / 1073741824)") in queries


def test_live_snapshot_derives_everything_from_two_queries(monkeypatch):
    prom = StubProm(instant_results={
        "slurm_job_utilization_gpu": [
            {"metric": {"slurmjobid": "100", "instance": "gpu1", "gpu": "0",
                        "job": "p1", "user": "alice", "gpu_type": "h200"},
             "value": [NOW, "90"]},
            {"metric": {"slurmjobid": "100", "instance": "gpu1", "gpu": "1",
                        "job": "p1", "user": "alice", "gpu_type": "h200"},
             "value": [NOW, "50"]},
            {"metric": {"slurmjobid": "101", "instance": "gpu2", "gpu": "0",
                        "job": "p2", "user": "bob", "gpu_type": "a100"},
             "value": [NOW, "10"]},
        ],
        "avg by (instance)": [
            {"metric": {"instance": "gpu1"}, "value": [NOW, "42.5"]},
            {"metric": {"instance": "gpu2"}, "value": [NOW, "13.0"]},
        ],
    })
    monkeypatch.setattr(deps, "get_prom", lambda: prom)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    types = {"gpu1": ["h200"], "gpu2": ["a100"]}
    snap = sources.live_snapshot(types)
    assert set(snap) == {"live_ids", "node_util", "node_vram", "jobs_by_node",
                         "allocs_by_node", "allocs_by_group"}
    assert len(prom.instant_calls) == 2  # two queries, not five
    assert snap["live_ids"] == {"100", "101"}
    assert snap["node_util"] == {"gpu1": 90.0, "gpu2": 10.0}  # max per node
    assert snap["node_vram"] == {"gpu1": 42.5, "gpu2": 13.0}
    # Grouped per (instance, slurmjobid, job, user); util is the max of
    # the group's per-GPU series.
    assert snap["jobs_by_node"] == {
        "gpu1": [{"jobid": "100", "job": "p1", "user": "alice",
                  "util": 90.0}],
        "gpu2": [{"jobid": "101", "job": "p2", "user": "bob", "util": 10.0}],
    }
    # One exporter series per allocated GPU.
    assert snap["allocs_by_node"] == {"gpu1": 2, "gpu2": 1}
    assert snap["allocs_by_group"] == {"h200": 2, "a100": 1}
    again = sources.live_snapshot(types)
    assert again is snap  # 30 s snapshot cache
    assert len(prom.instant_calls) == 2


# ---- aggregate_by ---------------------------------------------------

RAW = [
    {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
     "values": [["1000", "40"], ["1100", "60"]]},
    {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1"},
     "values": [["1000", "10"], ["1100", "0"]]},
    {"metric": {"slurmjobid": "2", "instance": "gpu2", "gpu": "0"},
     "values": [["1000", "80"], ["1100", "bad"]]},
]


def test_aggregate_by_max():
    out = aggregate_by(RAW, ["slurmjobid", "instance"], "max")
    assert out == [
        {"metric": {"slurmjobid": "1", "instance": "gpu1"},
         "values": [(1000.0, 40.0), (1100.0, 60.0)]},
        {"metric": {"slurmjobid": "2", "instance": "gpu2"},
         "values": [(1000.0, 80.0)]},
    ]


def test_aggregate_by_sum_and_count():
    sums = aggregate_by(RAW, ["instance"], "sum")
    assert sums == [
        {"metric": {"instance": "gpu1"},
         "values": [(1000.0, 50.0), (1100.0, 60.0)]},
        {"metric": {"instance": "gpu2"}, "values": [(1000.0, 80.0)]},
    ]
    counts = aggregate_by(RAW, ["instance"], "count")
    assert counts == [
        {"metric": {"instance": "gpu1"},
         "values": [(1000.0, 2.0), (1100.0, 2.0)]},
        {"metric": {"instance": "gpu2"}, "values": [(1000.0, 1.0)]},
    ]


def test_aggregate_by_live_filter_and_parsed_input():
    live = aggregate_by(RAW, ["instance"], "sum", live={"2"})
    assert live == [{"metric": {"instance": "gpu2"},
                     "values": [(1000.0, 80.0)]}]
    parsed = [{"metric": {"instance": "gpu1"},
               "values": [(1000.0, 40.0), (1100.0, 60.0)]}]
    assert aggregate_by(parsed, ["instance"], "sum") == [
        {"metric": {"instance": "gpu1"},
         "values": [(1000.0, 40.0), (1100.0, 60.0)]}]


def test_aggregate_by_drops_groups_without_values():
    empty = [{"metric": {"instance": "gpu9"}, "values": []}]
    assert aggregate_by(RAW + empty, ["instance"], "max") == \
        aggregate_by(RAW, ["instance"], "max")


# ---- sacct_window ---------------------------------------------------

NODES = [
    {"name": "gpu1", "gres": [("h200", 4)],
     "partitions": "gpu-h200,gpu-debug"},
    {"name": "csl1", "gres": [], "partitions": "batch"},
]


def _row(jobid, raw, state, **over):
    row = {"jobid": jobid, "jobid_raw": raw, "state": state, "name": "j",
           "user": "alice", "gpus": 1, "gpu_type": "h200", "elapsed_s": 60,
           "start": "2026-09-22T10:00:00", "end": "2026-09-22T10:01:00"}
    row.update(over)
    return row


def _setup_sacct_window(monkeypatch, allocations):
    """Patch the external deps sacct_window reaches; returns the recorder."""
    calls = []

    def alloc(start_iso, end_iso, partitions):
        calls.append((start_iso, end_iso, partitions))
        return allocations.get(start_iso, [])

    monkeypatch.setattr(deps, "now", lambda: NOW)
    monkeypatch.setattr(deps, "show_nodes", lambda: list(NODES))
    monkeypatch.setattr(deps, "sacct_allocations", alloc)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    return calls


def test_sacct_window_chunks_dedupe_and_progress(monkeypatch):
    # 36 h back from NOW lands on 2026-09-22T03:30 Helsinki: two edge
    # chunks around 09-23's midnight, no full days.
    calls = _setup_sacct_window(monkeypatch, {
        "2026-09-22T03:30:00": [_row("100", "100", "RUNNING")],
        "2026-09-23T00:00:00": [_row("100", "100", "COMPLETED",
                                     elapsed_s=7200)],
    })
    states = []
    records, coverage, start, end = sources.sacct_window(
        36, progress=states.append, progress_key=("k", 36))
    assert calls == [
        ("2026-09-22T03:30:00", "2026-09-23T00:00:00",
         ["gpu-debug", "gpu-h200"]),
        ("2026-09-23T00:00:00", "2026-09-23T15:30:00",
         ["gpu-debug", "gpu-h200"]),
    ]
    assert start == int(NOW) - 36 * 3600 and end == NOW
    # A midnight-spanning job's final state lives in the later chunk.
    assert [r["state"] for r in records] == ["COMPLETED"]
    assert coverage == {"failed_batches": 0, "successful_batches": 2,
                        "complete": True}
    assert states[0] == {"done": 0, "total": 2, "failed_batches": 0}
    assert states[-1] == {"done": 2, "total": 2, "failed_batches": 0}
    assert [s["done"] for s in states] == sorted(s["done"] for s in states)
    # The leader seeded and then popped its progress-store entry.
    assert ("k", 36) not in cache.progress_store


def test_sacct_window_day_chunk_shared_across_windows(monkeypatch):
    calls = _setup_sacct_window(monkeypatch, {
        "2026-09-21T03:30:00": [_row("1", "1", "COMPLETED")],
        "2026-09-22T00:00:00": [_row("2", "2", "COMPLETED")],
        "2026-09-21T23:30:00": [_row("3", "3", "COMPLETED")],
    })
    sources.sacct_window(60)  # chunks: edge1, full day 09-22, edge2
    day_calls = [c for c in calls if c[0] == "2026-09-22T00:00:00"]
    assert len(day_calls) == 1
    sources.sacct_window(40)  # start 09-21T23:30: same full day 09-22
    day_calls = [c for c in calls if c[0] == "2026-09-22T00:00:00"]
    assert len(day_calls) == 1  # served from the per-day cache, 1 h TTL


def test_sacct_window_failed_chunk_keeps_the_others(monkeypatch):
    calls = []
    monkeypatch.setattr(deps, "now", lambda: NOW)
    monkeypatch.setattr(deps, "show_nodes", lambda: list(NODES))
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())

    def alloc(start_iso, end_iso, partitions):
        calls.append((start_iso, end_iso, partitions))
        if start_iso == "2026-09-22T03:30:00":
            raise SlurmError("sacct down")  # both attempts fail
        return [_row("100", "100", "COMPLETED")]

    monkeypatch.setattr(deps, "sacct_allocations", alloc)
    states = []
    records, coverage, start, end = sources.sacct_window(
        36, progress=states.append)
    assert coverage == {"failed_batches": 1, "successful_batches": 1,
                        "complete": False}
    assert [r["jobid"] for r in records] == ["100"]  # survivor kept
    assert states[0] == {"done": 0, "total": 2, "failed_batches": 0}
    assert states[-1] == {"done": 2, "total": 2, "failed_batches": 1}


# ---- sacct_rows ------------------------------------------------------

def test_sacct_rows_maps_jobid_raw_and_array_tasks(monkeypatch):
    task = _row("20001465_47", "20008872", "COMPLETED")
    calls = []

    def resilient(ids, start_iso=None, workers=8, progress=None):
        calls.append(list(ids))
        rows = {}
        for jid in ids:
            if jid in ("20008872", "20001465"):
                rows["20001465_47"] = task
                rows["20008872"] = dict(task)
        return rows, 0

    monkeypatch.setattr(deps, "sacct_jobs_resilient", resilient)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    out = sources.sacct_rows(["20008872"])
    # One row: the task is indexed under both spellings and deduped.
    assert out == {"20008872": [task]}
    out = sources.sacct_rows(["20001465"])
    # The array parent resolves to its task rows via the parent_task form.
    assert [r["jobid"] for r in out["20001465"]] == ["20001465_47"]
    assert calls == [["20008872"], ["20001465"]]


def test_sacct_rows_no_negative_cache_on_failed_batches(monkeypatch):
    calls = []

    def resilient(ids, start_iso=None, workers=8, progress=None):
        calls.append(list(ids))
        return {}, 1  # whole batch failed

    monkeypatch.setattr(deps, "sacct_jobs_resilient", resilient)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    assert sources.sacct_rows(["7", "8"]) == {"7": None, "8": None}
    assert sources.sacct_rows(["7"]) == {"7": None}
    assert len(calls) == 2  # retried: nothing was cached while failing

    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, workers=8, progress=None:
                        ({}, 0))
    assert sources.sacct_rows(["7"]) == {"7": []}  # success caches the miss


def test_sacct_rows_terminal_state_gets_long_ttl(monkeypatch):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])

    def resilient(ids, start_iso=None, workers=8, progress=None):
        calls.append(list(ids))
        rows = {}
        for jid in ids:
            state = "COMPLETED" if jid == "9" else "RUNNING"
            rows[jid] = _row(jid, jid, state)
        return rows, 0

    monkeypatch.setattr(deps, "sacct_jobs_resilient", resilient)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    sources.sacct_rows(["9", "10"])
    assert calls == [["9", "10"]]
    now[0] += 400  # past the 300 s TTL, within the terminal 1 h one
    out = sources.sacct_rows(["9", "10"])
    assert out["9"] == [_row("9", "9", "COMPLETED")]  # still cached
    assert out["10"][0]["state"] == "RUNNING"  # refetched
    assert calls == [["9", "10"], ["10"]]
