"""Endpoint tests with a fake Prometheus client.

Run: .venv/bin/python -m pytest tests/ -q
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import cache  # noqa: E402
import deps  # noqa: E402
import domain.jobs as domain_jobs  # noqa: E402
import domain.metadata as domain_metadata  # noqa: E402
import domain.partitions as domain_partitions  # noqa: E402
import slurm  # noqa: E402
from api import users as api_users  # noqa: E402

# Deterministic "now" (2026-08-30T18:26:40Z): job 1's sacct start
# (2026-08-28T00:00:00) is ~2.2 days back, inside the seven-day clamp.
NOW = 1788100000
JOB1_START = "2026-08-28T00:00:00"
JOB1_END = ""
JOB2_START = "2026-08-29T00:00:00"
JOB2_END = "2026-08-29T02:00:00"
JOB3_START = "2026-08-27T00:00:00"
JOB3_END = "2026-08-28T00:00:00"

SACCT = {
    "1": {"jobid": "1", "name": "train.sh", "user": "alice", "account": "acc",
          "partition": "gpu-h200-ellis", "state": "RUNNING",
          "submit": "2026-08-27T23:00:00", "start": JOB1_START,
          "end": JOB1_END, "elapsed_s": 3600, "gpus": 2, "gpu_type": "h200",
          "node_list": "gpu1", "ncpus": 8},
    "2": {"jobid": "2", "name": "fin.sh", "user": "bob", "account": "acc",
          "partition": "gpu-h200", "state": "COMPLETED",
          "submit": "2026-08-28T23:00:00", "start": JOB2_START,
          "end": JOB2_END, "elapsed_s": 7200, "gpus": 1, "gpu_type": "h200",
          "node_list": "gpu1", "ncpus": 4},
    "3": {"jobid": "3", "name": "infer.sh", "user": "carol", "account": "acc",
          "partition": "gpu-h100", "state": "COMPLETED",
          "submit": "2026-08-26T23:00:00", "start": JOB3_START,
          "end": JOB3_END, "elapsed_s": 86400, "gpus": 4, "gpu_type": "h100",
          "node_list": "gpu2", "ncpus": 8},
    # MIG slice on gpu49: distinct user; its node's GRES profile splits
    # it from the whole-GPU h200 group (capacity belongs only to the
    # profile group).
    "4": {"jobid": "4", "name": "mig.sh", "user": "dave", "account": "acc",
          "partition": "gpu-h200-mig", "state": "RUNNING",
          "submit": "2026-08-28T22:00:00",
          "start": JOB2_START,
          "end": JOB1_END, "elapsed_s": 3600, "gpus": 1,
          "gpu_type": "h200_3g.71gb", "node_list": "gpu49", "ncpus": 32},
}

NODES = [
    {"name": "gpu1", "state": "MIXED", "state_full": "MIXED", "reason": "",
     "partitions": "gpu-h200,gpu-h200-ellis", "cpus": 64, "gpus": 8,
     "gpu_type": "h200",
     "gres": [("h200", 8)],
     "gpus_alloc": 2, "cpus_alloc": 16, "free_mem": 1000, "real_mem": 5000},
    # priority variant over the same hardware: must collapse into the
    # same h200 queue type
    {"name": "gpu3", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "gpu-h200-ellis", "cpus": 64, "gpus": 8,
     "gpu_type": "h200",
     "gres": [("h200", 8)],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 4000, "real_mem": 5000},
    # another type entirely
    {"name": "gpu2", "state": "ALLOCATED", "state_full": "ALLOCATED*",
     "reason": "",
     "partitions": "gpu-h100", "cpus": 64, "gpus": 8, "gpu_type": "h100",
     "gres": [("h100", 8)],
     "gpus_alloc": 3, "cpus_alloc": 32, "free_mem": 900, "real_mem": 5000},
    # gpu49 in reality: 4 whole H200 GPUs + 8 MIG h200_3g.71gb slices on
    # one node — each group must see only its own type's count
    {"name": "gpu49", "state": "ALLOCATED", "state_full": "ALLOCATED",
     "reason": "",
     "partitions": "gpu-h200-mig", "cpus": 128, "gpus": 12,
     "gpu_type": "h200",
     "gres": [("h200", 4), ("h200_3g.71gb", 8)],
     "gpus_alloc": 1, "cpus_alloc": 32, "free_mem": 900, "real_mem": 5000},
    # Configured GH200 pool with no utilization samples in this window.
    # It must remain visible as no-data instead of being shown as 0%.
    {"name": "gpu50", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "gpu-gh200", "cpus": 128, "gpus": 4,
     "gpu_type": "gh200", "gres": [("gh200", 4)],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 900, "real_mem": 5000},
    # CPU-only node: its partition must never appear as a GPU queue
    {"name": "csl1", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "batch", "cpus": 40, "gpus": 0, "gpu_type": "",
     "gres": [],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 100, "real_mem": 200},
]


COMPLETED_HISTORY = [{**record, "state": "COMPLETED"}
                     for record in SACCT.values()]


class FakeProm:
    """Canned responses shaped like the real Prometheus API."""

    api_base = "http://fake/api/v1"

    def __init__(self):
        self.calls = []
        self.nodes_calls = 0
        self.live_ids = {"1", "2", "4"}   # count by (slurmjobid)
        self.job_start_ids = {"1", "2"}  # count by (slurmjobid){instance="gpu1"}
        self.clear_cache_calls = 0
        # Extra window series appended to the jobs list by tests that
        # need more candidates than the canned three.
        self.extra_jobs = []

    # -- canned range data -------------------------------------------------
    _JOB_DETAIL_UTIL = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1"},
         "values": [[1000, "20"], [1120, "80"]]},
    ]

    @staticmethod
    def _matchers(query):
        m = re.search(r'slurmjobid=~"\^\(\?:([^)]*)\)\$"', query)
        return set(m.group(1).split("|")) if m else None

    @staticmethod
    def _filter(series, ids):
        if ids is None:
            return series
        return [s for s in series if s["metric"].get("slurmjobid") in ids]
    _NODE_DETAIL_UTIL = [
        {"metric": {"slurmjobid": "1", "gpu": "0"},
         "values": [[1000, "50"], [1120, "50"]]},
    ]
    # Partition summary keeps slurmjobid + instance + gpu_type so job
    # identity, the capacity join, and the GPU-type grouping survive
    # aggregation. The exporter's gpu_type label aliases the short
    # scontrol type (gpu1 reports "NVIDIA H200"); grouping must
    # canonicalize against the instance's configured types. Job 4 runs
    # on mixed node gpu49; its MIG-shaped label must split it into the
    # profile group, and the whole-GPU label must not.
    _PART_SUMMARY = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "gpu-h200",
                    "gpu_type": "NVIDIA H200"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "job": "gpu-h200",
                    "gpu_type": "h200"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "job": "gpu-h100",
                    "gpu_type": "h100"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "job": "gpu-h200-mig",
                    "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "80"], [1120, "90"]]},
    ]
    # Trend/occupancy preserve instance + raw label so aggregation can
    # resolve aliases against each node's configured GRES types.
    _PART_UTIL_SUMS = [
        {"metric": {"instance": "gpu1", "gpu_type": "NVIDIA H200"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"instance": "gpu1", "gpu_type": "h200"},
         "values": [[1000, "10"]]},
        {"metric": {"instance": "gpu2", "gpu_type": "h100"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"instance": "gpu49", "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "80"], [1120, "90"]]},
    ]
    _PART_GPU_COUNTS = [
        {"metric": {"instance": "gpu1", "gpu_type": "NVIDIA H200"},
         "values": [[1000, "2"], [1120, "2"]]},
        {"metric": {"instance": "gpu1", "gpu_type": "h200"},
         "values": [[1000, "1"]]},
        {"metric": {"instance": "gpu2", "gpu_type": "h100"},
         "values": [[1000, "1"], [1120, "1"]]},
        {"metric": {"instance": "gpu49", "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "1"], [1120, "1"]]},
    ]
    _JOBS_UTIL = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "gpu-h200",
                    "user": "alice", "gpu_type": "NVIDIA H200"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "job": "gpu-h200",
                    "user": "bob", "gpu_type": "h200"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "job": "gpu-h100",
                    "user": "carol", "gpu_type": "h100"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "job": "gpu-h200-mig",
                    "user": "dave", "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "80"], [1120, "90"]]},
    ]
    # Per-GPU VRAM (GB) over the window, matching the /partitions/vram
    # query shape. Job 1 peaks 12/18 on its two GPUs, job 2 peaks 30,
    # job 3 peaks 8 (the 0 sample is a never-reported GPU, not a low).
    _VRAM_GB = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
         "values": [[1000, "10"], [1120, "12"]]},
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1"},
         "values": [[1000, "16"], [1120, "18"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "gpu": "0"},
         "values": [[1000, "30"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "gpu": "0"},
         "values": [[1000, "8"], [1120, "0"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "gpu": "0"},
         "values": [[1000, "20"], [1120, "22"]]},
    ]

    def query_range(self, query, start, end, step):
        self.calls.append(("range", query))
        if "slurm_job_utilization_gpu" in query:
            if 'slurmjobid="' in query:  # job detail: per-device series
                return self._JOB_DETAIL_UTIL
            if 'instance="' in query:  # node detail
                return self._NODE_DETAIL_UTIL
            if "count by (instance, gpu_type)" in query:
                ids = self._matchers(query)
                if ids is None:
                    return self._PART_GPU_COUNTS
                allowed = {(s["metric"]["instance"], s["metric"]["gpu_type"])
                           for s in self._PART_SUMMARY
                           if s["metric"]["slurmjobid"] in ids}
                return [s for s in self._PART_GPU_COUNTS
                        if (s["metric"]["instance"], s["metric"]["gpu_type"])
                        in allowed]
            if "sum by (instance, gpu_type)" in query:
                ids = self._matchers(query)
                if ids is None:
                    return self._PART_UTIL_SUMS
                allowed = {(s["metric"]["instance"], s["metric"]["gpu_type"])
                           for s in self._PART_SUMMARY
                           if s["metric"]["slurmjobid"] in ids}
                return [s for s in self._PART_UTIL_SUMS
                        if (s["metric"]["instance"], s["metric"]["gpu_type"])
                        in allowed]
            if "max by (slurmjobid, instance, gpu_type)" in query:
                return self._filter(self._PART_SUMMARY, self._matchers(query))
            jobs_util = self._JOBS_UTIL + self.extra_jobs
            return self._filter(jobs_util, self._matchers(query))
        if "memory" in query:  # vram
            if "max by (slurmjobid, instance, gpu)" in query:  # job records
                return self._filter(self._VRAM_GB, self._matchers(query))
            if 'slurmjobid="' in query:  # job detail
                return [
                    {"metric": {"instance": "gpu1", "gpu": "0"},
                     "values": [[1000, "12.5"], [1120, "13.5"]]},
                ]
            if 'instance="' in query:
                # Two co-located 1-GPU jobs both report gpu="0" (job-local
                # label) — they must stay separate series.
                return [
                    {"metric": {"slurmjobid": "1", "gpu": "0"},
                     "values": [[1000, "30"], [1120, "32"]]},
                    {"metric": {"slurmjobid": "2", "gpu": "0"},
                     "values": [[1000, "45"], [1120, "47"]]},
                ]
            return [
                {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
                 "values": [[1000, "10"], [1120, "12"]]},
            ]
        return []

    def query_instant(self, query, time=None):
        self.calls.append(("instant", query))
        if "count by (slurmjobid)" in query:
            if 'instance="' in query:  # node-detail job-start lookup
                return [{"metric": {"slurmjobid": j}, "value": [1, "1"]}
                        for j in sorted(self.job_start_ids)]
            return [{"metric": {"slurmjobid": j}, "value": [1, "1"]}
                    for j in sorted(self.live_ids)]
        if "count by (instance, job, gpu_type)" in query:
            return [
                {"metric": {"instance": "gpu1", "job": "gpu-h200",
                            "gpu_type": "NVIDIA H200"},
                 "value": [1, "2"]},
                {"metric": {"instance": "gpu2", "job": "gpu-h100",
                            "gpu_type": "h100"},
                 "value": [1, "3"]},
                {"metric": {"instance": "gpu49", "job": "gpu-h200-mig",
                            "gpu_type": "h200_3g.71gb"},
                 "value": [1, "1"]},
            ]
        if "max by (instance) (slurm_job_utilization_gpu)" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "55.5"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "10.0"]},
                {"metric": {"instance": "gpu49"}, "value": [1, "60.0"]},
            ]
        if "memory_usage_gpu /" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "41.2"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "8.0"]},
                {"metric": {"instance": "gpu49"}, "value": [1, "40.0"]},
            ]
        if "slurmjobid, job, user" in query:
            return [
                {"metric": {"instance": "gpu1", "slurmjobid": "1", "job": "gpu-h100",
                            "user": "alice"}, "value": [1, "77.0"]},
                {"metric": {"instance": "gpu2", "slurmjobid": "3", "job": "gpu-h200",
                            "user": "carol"}, "value": [1, "90.0"]},
                {"metric": {"instance": "gpu49", "slurmjobid": "4", "job": "gpu-h200",
                            "user": "dave"}, "value": [1, "85.0"]},
            ]
        return []

    def clear_cache(self):
        self.clear_cache_calls += 1


@pytest.fixture()
def fake_prom(monkeypatch):
    # Every external dependency (Prometheus, sacct, scontrol, the clock,
    # the route cache) is patched here in one place, on deps — not on
    # appmod — since deps is the one module every call site (regardless
    # of which file it lives in) reaches these through. Patching appmod
    # instead would only affect code that still lives in app.py today;
    # code that later moves to a different module would silently start
    # hitting the real cluster instead of failing loudly.
    fake = FakeProm()
    monkeypatch.setattr(deps, "get_prom", lambda: fake)
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    monkeypatch.setattr(deps, "sacct_jobs",
                        lambda ids, start_iso=None, **kw: {j: SACCT[j] for j in ids
                                                           if j in SACCT})
    monkeypatch.setattr(
        deps, "completed_jobs",
        lambda start_iso, end_iso: (COMPLETED_HISTORY, {
            "failed_batches": 0, "successful_batches": 1, "complete": True,
        }))
    # No active controller jobs by default; tests opt in to a snapshot.
    monkeypatch.setattr(deps, "show_jobs", lambda: {})
    # Empty pending queue by default; tests opt in via deps.queue_pending.
    monkeypatch.setattr(deps, "queue_pending", lambda: [])

    def _show_nodes():
        fake.nodes_calls += 1
        return list(NODES)

    monkeypatch.setattr(deps, "show_nodes", _show_nodes)
    monkeypatch.setattr(deps, "now", lambda: NOW)
    return fake


@pytest.fixture()
def client(fake_prom):
    return TestClient(appmod.app)


def _epoch(s):
    # sacct/squeue strings are Europe/Helsinki-naive (the cluster wall
    # clock); interpret them the way domain._sacct_epoch does, not on
    # the process TZ.
    from domain.partitions import _sacct_epoch
    return _sacct_epoch(s)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_jobs_endpoint_nonempty(client):
    r = client.get("/api/jobs", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    job = data["jobs"][0]
    assert job["jobid"]
    assert 0 <= job["mean_util"] <= 100
    assert job["gpu_hours_eff"] is not None


def test_jobs_mean_util_not_trivially_100(client):
    r = client.get("/api/jobs", params={"since_hours": 24})
    utils = [j["mean_util"] for j in r.json()["jobs"]]
    assert utils, "no jobs returned"
    assert not all(u == 100.0 for u in utils), (
        "every job reports exactly 100% — aggregation bug"
    )


def test_jobs_user_filter(client):
    client.get("/api/jobs", params={"since_hours": 24})
    filtered = client.get("/api/jobs",
                          params={"since_hours": 24, "user": "nobody"}).json()
    assert filtered["count"] == 0


def test_jobs_name_search(client):
    r = client.get("/api/jobs", params={"since_hours": 24, "search": "train.sh"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["jobs"][0]["name"] == "train.sh"


def test_jobs_running_only_keeps_live_ids(client, fake_prom):
    r = client.get("/api/jobs",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    ids = {j["jobid"] for j in r.json()["jobs"]}
    assert ids == {"1", "2", "4"}  # job 3 has no live GPU series


def test_jobs_running_only_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/jobs",
                   params={"since_hours": 24, "running_only": "true"})
    data = r.json()
    assert data["count"] == 0 and data["jobs"] == []
    assert data["total_candidates"] == 0
    # no broad window range query may be issued when nothing is running
    assert not [q for t, q in fake_prom.calls if t == "range"]


def test_users_aggregates_per_user(client):
    # step=120 s; per-job util-gpu-hours = sum(values) * step / 3600 / 100:
    # job 1 (alice): (40+60)*120/3600/100 = 0.0333, mean util 50
    # job 2 (bob):   10*120/3600/100 = 0.0033, mean util 10
    # job 3 (carol): (90+95)*120/3600/100 = 0.0617, mean util 92.5
    # job 4 (dave):  (80+90)*120/3600/100 = 0.0567, mean util 85
    # Live set is {1, 2, 4}. The list is Prometheus-only (no sacct keys).
    r = client.get("/api/users", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 4
    by_user = {u["user"]: u for u in data["users"]}
    alice = by_user["alice"]
    assert alice["jobs"] == 1 and alice["running_jobs"] == 1
    assert alice["util_gpu_hours"] == pytest.approx(0.0333, abs=0.005)
    assert alice["mean_util"] == pytest.approx(50.0)
    assert alice["vram_avg"] == pytest.approx(11.0)  # mean of 10 and 12
    # the Users table shows the raw exporter gpu_type label (not the
    # canonical Partitions-tab group)
    assert alice["gpu_types"] == ["NVIDIA H200"]
    assert "name" not in alice and "gpu_hours_alloc" not in alice
    dave = by_user["dave"]
    assert dave["jobs"] == 1 and dave["running_jobs"] == 1
    assert dave["util_gpu_hours"] == pytest.approx(0.0567, abs=0.005)
    assert dave["mean_util"] == pytest.approx(85.0)
    assert dave["gpu_types"] == ["h200_3g.71gb"]
    carol = by_user["carol"]
    assert carol["running_jobs"] == 0
    assert carol["util_gpu_hours"] == pytest.approx(0.0617, abs=0.005)
    assert carol["mean_util"] == pytest.approx(92.5)
    assert carol["vram_avg"] is None
    # util-gpu-hours descending: carol > dave > alice > bob.
    assert [u["user"] for u in data["users"]] == ["carol", "dave", "alice", "bob"]


def test_users_mean_util_weights_samples_not_effective_gpu_hours(client, monkeypatch):
    # Equal-duration 10% and 90% jobs must aggregate to 50%, not 82% from
    # weighting a utilization value by gpu_hours_eff (which already includes it).
    jobs = [
        {"jobid": "low", "user": "alice", "mean_util": 10.0,
         "gpu_hours_eff": 0.1, "_util_sum": 10.0, "_util_samples": 1,
         "gpu_type": "h100", "vram_avg": None},
        {"jobid": "high", "user": "alice", "mean_util": 90.0,
         "gpu_hours_eff": 0.9, "_util_sum": 90.0, "_util_samples": 1,
         "gpu_type": "h100", "vram_avg": None},
    ]
    # api_users looks these names up in its own module (api.users), since
    # it imported them with `from domain.X import Y` — patching the domain
    # module itself would not affect this already-bound reference.
    monkeypatch.setattr(api_users, "fetch_job_window",
                        lambda since_hours: (jobs, 1, 2, 120))
    monkeypatch.setattr(api_users, "running_gpu_job_ids", lambda: set())
    data = client.get("/api/users", params={"since_hours": 24}).json()
    assert data["users"][0]["mean_util"] == 50.0


def test_users_window_validation(client):
    assert client.get("/api/users", params={"since_hours": 0}).status_code == 422


def test_jobs_user_filter_is_query_scoped(client, fake_prom):
    # The user must reach the Prometheus selector, not just a post-fetch
    # filter (a single-user request must not pull every user's window).
    r = client.get("/api/jobs", params={"since_hours": 24, "user": "alice"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["jobs"][0]["jobid"] == "1"
    users = [q for t, q in fake_prom.calls if t == "range"]
    assert any('user="alice"' in q for q in users)


def test_jobs_user_filter_preserves_prometheus_label_case(client, fake_prom):
    # PromQL exact label matchers are case-sensitive. The endpoint must send
    # the selected label unchanged, then use casefold only after the query.
    r = client.get("/api/jobs", params={"since_hours": 24, "user": "Alice"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    users = [q for t, q in fake_prom.calls if t == "range"]
    assert any('user="Alice"' in q for q in users)


def test_job_detail_200_with_human_readable_meta(client):
    r = client.get("/api/jobs/1", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["jobid"] == "1"
    assert data["series"]["utilization"], "no utilization series"
    assert data["series"]["utilization"][0]["values"]
    meta = data["metadata"]
    assert meta and meta["name"] == "train.sh"
    # Human-readable sacct strings are preserved; no epoch fields.
    assert meta["start"] == JOB1_START
    assert meta["end"] == ""  # running job: no end
    assert "start_epoch" not in meta
    assert "end_epoch" not in meta


def test_job_detail_summary_row_fields(client):
    # PLAN-2: the summary row above the trend chart needs mean utilization,
    # GPU-hours, and elapsed time computed for the single job in view, not
    # taken from the (separate) Jobs-list fetch.
    data = client.get("/api/jobs/1", params={"since_hours": 24}).json()
    assert data["mean_util"] == 50.0  # mean of job 1's util series (40, 60)
    assert data["gpu_hours_alloc"] == 2.0  # 2 gpus x 1h elapsed
    assert data["gpu_hours_eff"] == 1.0  # 2.0 alloc x 50% mean_util
    assert data["elapsed_s"] == 3600


def test_job_detail_summary_fields_null_without_metadata(
        client, fake_prom, monkeypatch):
    # No sacct/scontrol record resolves for this ID: mean_util still comes
    # from the series alone, but the allocation-based fields have nothing
    # to compute from.
    monkeypatch.setattr(deps, "sacct_jobs", lambda ids, start_iso=None, **kw: {})
    data = client.get("/api/jobs/1", params={"since_hours": 24}).json()
    assert data["metadata"] is None
    assert data["mean_util"] == 50.0
    assert data["gpu_hours_alloc"] is None
    assert data["elapsed_s"] is None


def test_job_detail_end_human_readable(client):
    meta = client.get("/api/jobs/2", params={"since_hours": 24}).json()["metadata"]
    assert meta["end"] == JOB2_END
    assert "start_epoch" not in meta and "end_epoch" not in meta


def _scontrol_row(jobid, parent, task, node, state="RUNNING",
                  start=JOB1_START, gpus=1, elapsed=3600, ncpus=4):
    return {"jobid": jobid, "array_jobid": parent, "array_task_id": task,
            "name": "arr.sh", "user": "alice", "account": "acc",
            "partition": "gpu-h100", "state": state, "start": start,
            "end": "", "elapsed_s": elapsed, "gpus": gpus, "gpu_type": "h100",
            "node_list": node, "ncpus": ncpus}


def test_enrich_merges_active_array_tasks(client, fake_prom, monkeypatch):
    # Bare array parent "42": Prometheus sees it on two nodes; scontrol
    # holds the physical tasks (suffix-tolerant IDs). Both must merge into
    # one row instead of being left blank or misattributed.
    monkeypatch.setattr(deps, "sacct_jobs", lambda ids, start_iso=None, **kw: {})
    monkeypatch.setattr(deps, "show_jobs", lambda: {
        "42_1": _scontrol_row("42_1", "42", "1", "gpu2",
                              start=JOB2_START, elapsed=7200),
        "42_0": _scontrol_row("42_0", "42", "0", "gpu1",
                              start=JOB1_START, elapsed=3600),
    })
    jobs = [{"jobid": "42", "nodes": ["gpu1", "gpu2"], "mean_util": 50.0,
             "gpu_hours_eff": 0.5}]
    domain_metadata.enrich(jobs)
    job = jobs[0]
    assert job["name"] == "arr.sh"
    assert job["state"] == "RUNNING"
    assert job["start"] == JOB1_START  # earliest task start
    assert job["node_list"] == "gpu1,gpu2"
    assert job["gpus"] == 2 and job["ncpus"] == 8
    # allocation = 1 GPU x 1 h + 1 GPU x 2 h
    assert job["gpu_hours_alloc"] == 3.0
    assert job["gpu_hours_eff"] == 1.5


def test_enrich_array_no_node_match_falls_back_without_misattribution(
        client, fake_prom, monkeypatch):
    # 44's only active task ran on a node that did not observe the job.
    monkeypatch.setattr(deps, "sacct_jobs",
                        lambda ids, start_iso=None, **kw: {})
    monkeypatch.setattr(deps, "show_jobs", lambda: {
        "44_0": _scontrol_row("44_0", "44", "0", "gpu2"),
    })
    jobs = [{"jobid": "44", "nodes": ["gpu1"], "mean_util": 10.0,
             "gpu_hours_eff": 0.1}]
    domain_metadata.enrich(jobs)
    for key in ("name", "state", "start", "gpus", "node_list"):
        assert key not in jobs[0]


def test_enrich_scontrol_failure_falls_back_to_sacct(client, fake_prom,
                                                      monkeypatch):
    from slurm import SlurmError

    monkeypatch.setattr(deps, "show_jobs",
                        lambda: (_ for _ in ()).throw(SlurmError("boom")))
    jobs = [{"jobid": "1", "nodes": ["gpu1"], "mean_util": 40.0,
             "gpu_hours_eff": 0.4}]
    domain_metadata.enrich(jobs)
    job = jobs[0]
    assert job["name"] == "train.sh"
    assert job["state"] == "RUNNING"
    assert job["gpus"] == 2
    assert job["gpu_hours_alloc"] == pytest.approx(2.0)  # 2 GPUs x 1 h


def test_enrich_merges_historical_array_tasks_in_sacct(
        client, fake_prom, monkeypatch):
    # Historical parent "45": already finished, so scontrol no longer
    # knows it. Three of its sacct tasks ran on the observed node and must
    # merge into one metadata row (the reported array-table gap).
    monkeypatch.setattr(deps, "show_jobs", lambda: {})
    monkeypatch.setattr(deps, "sacct_jobs", lambda ids, start_iso=None, **kw: {
        "45_0": {"jobid": "45_0", "name": "hist.sh", "user": "alice",
                 "account": "acc", "partition": "gpu-h100",
                 "state": "COMPLETED", "start": JOB3_START,
                 "end": JOB3_END, "elapsed_s": 3600, "gpus": 1,
                 "gpu_type": "h100", "node_list": "gpu1", "ncpus": 4},
        "45_1": {"jobid": "45_1", "name": "hist.sh", "user": "alice",
                 "account": "acc", "partition": "gpu-h100",
                 "state": "COMPLETED", "start": JOB2_START,
                 "end": JOB2_END, "elapsed_s": 1800, "gpus": 1,
                 "gpu_type": "h100", "node_list": "gpu1", "ncpus": 4},
        "45_9": {"jobid": "45_9", "name": "hist.sh", "user": "alice",
                 "account": "acc", "partition": "gpu-h100",
                 "state": "COMPLETED", "start": JOB1_START,
                 "end": JOB1_END, "elapsed_s": 7200, "gpus": 2,
                 "gpu_type": "h100", "node_list": "gpu1,gpu2", "ncpus": 8},
    })
    jobs = [{"jobid": "45", "nodes": ["gpu1"], "mean_util": 50.0,
             "gpu_hours_eff": 0.5}]
    domain_metadata.enrich(jobs)
    job = jobs[0]
    assert job["name"] == "hist.sh"
    assert job["state"] == "COMPLETED"
    assert job["start"] == JOB3_START  # earliest of the matching tasks
    assert job["node_list"] == "gpu1,gpu2"
    assert job["gpus"] == 4 and job["ncpus"] == 16
    # allocation = 1x1h + 1x0.5h + 2x2h
    assert job["gpu_hours_alloc"] == pytest.approx(5.5)


def test_enrich_array_task_without_node_match_is_not_merged(
        client, fake_prom, monkeypatch):
    # Parent "46" has sacct tasks, but none ran on the observed node;
    # the metadata must stay blank rather than be misattributed.
    monkeypatch.setattr(deps, "show_jobs", lambda: {})
    monkeypatch.setattr(deps, "sacct_jobs", lambda ids, start_iso=None, **kw: {
        "46_0": {"jobid": "46_0", "name": "other.sh", "user": "bob",
                 "account": "acc", "partition": "gpu-h200",
                 "state": "COMPLETED", "start": JOB3_START,
                 "end": JOB3_END, "elapsed_s": 1800, "gpus": 1,
                 "gpu_type": "h200", "node_list": "gpu2", "ncpus": 4},
        "46_1": {"jobid": "46_1", "name": "other.sh", "user": "bob",
                 "account": "acc", "partition": "gpu-h200",
                 "state": "COMPLETED", "start": JOB2_START,
                 "end": JOB2_END, "elapsed_s": 3600, "gpus": 1,
                 "gpu_type": "h200", "node_list": "gpu2", "ncpus": 4},
    })
    jobs = [{"jobid": "46", "nodes": ["gpu1"], "mean_util": 10.0,
             "gpu_hours_eff": 0.1}]
    domain_metadata.enrich(jobs)
    for key in ("name", "state", "start", "gpus", "node_list"):
        assert key not in jobs[0]

def _extra_job(i):
    return {"metric": {"slurmjobid": str(100 + i), "instance": "gpu1",
                       "job": "gpu-h100", "user": "alice",
                       "gpu_type": "h100"},
            "values": [[1000, "10"]]}


def test_jobs_default_limit_100(client, fake_prom):
    # 4 canned + 98 synthetic = 102 candidates.
    fake_prom.extra_jobs = [_extra_job(i) for i in range(98)]
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    assert data["count"] == 100


def test_jobs_limit_bounds(client, fake_prom):
    fake_prom.extra_jobs = [_extra_job(i) for i in range(98)]
    assert client.get("/api/jobs",
                      params={"since_hours": 24, "limit": 1}).json()["count"] == 1
    assert client.get("/api/jobs",
                      params={"since_hours": 24,
                              "limit": 1000}).json()["count"] == 102


def test_jobs_limit_rejects_out_of_range(client, fake_prom):
    for bad in ("0", "1001", "1.5"):
        r = client.get("/api/jobs", params={"since_hours": 24, "limit": bad})
        assert r.status_code == 422, bad


def test_jobs_running_only_ignores_limit(client, fake_prom):
    # Three live GPU jobs; limit=1 must not trim the running set.
    r = client.get("/api/jobs",
                   params={"since_hours": 24, "running_only": "true",
                           "limit": 1})
    assert r.status_code == 200
    assert {j["jobid"] for j in r.json()["jobs"]} == {"1", "2", "4"}

def test_partitions_group_by_gpu_type(client):
    r = client.get("/api/partitions", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # Two priority partitions (gpu-h200, gpu-h200-ellis) over the same
    # h200 hardware collapse into ONE h200 queue row; jobs 1+2 both land
    # there (2+2+1 samples of 40/60/10 -> mean 36.67). Job 3's h100 and
    # job 4's MIG profile stay their own groups. A configured GH200 pool
    # with no samples remains visible as explicit no-data across the table
    # and graphs rather than disappearing from the API response.
    assert set(by_name) == {"gh200", "h200", "h100", "h200_3g.71gb"}
    assert by_name["h200"]["job_count"] == 2
    assert by_name["h200"]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert by_name["h100"]["mean_util"] == pytest.approx(92.5)
    assert by_name["h200_3g.71gb"]["job_count"] == 1
    assert by_name["h200_3g.71gb"]["mean_util"] == pytest.approx(85.0)
    assert by_name["gh200"] == {
        "name": "gh200", "mean_util": None, "max_util": None,
        "job_count": 0, "gpus_alloc": 0, "gpus_total": 4,
        "mean_occupancy": None,
    }
    for p in data["partitions"]:
        assert p["mean_util"] is None or 0 <= p["mean_util"] <= 100
    assert {"gh200", "h100", "h200", "h200_3g.71gb"} <= set(data["trend"])
    assert data["trend"]["gh200"] == []


def test_partitions_gpu_capacity(client):
    by_name = {p["name"]: p for p in
               client.get("/api/partitions",
                          params={"since_hours": 24}).json()["partitions"]}
    # Capacity is summed per exact GRES type across ALL scontrol nodes
    # (idle gpu3 included, independent of partition membership):
    # h200 = gpu1's 8 + gpu3's 8 + gpu49's 4 whole = 20.
    assert by_name["h200"]["gpus_total"] == 20
    # allocation is the exact per-group live count (jobs 1+2 on gpu1 = 2).
    assert by_name["h200"]["gpus_alloc"] == 2
    assert by_name["h100"]["gpus_total"] == 8
    assert by_name["h100"]["gpus_alloc"] == 3
    # the MIG profile group carries only gpu49's slices.
    assert by_name["h200_3g.71gb"]["gpus_total"] == 8
    assert by_name["h200_3g.71gb"]["gpus_alloc"] == 1




def test_partitions_running_only_injects_matcher(client, fake_prom):
    client.get("/api/partitions", params={"since_hours": 24})
    fake_prom.calls.clear()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    matcher = 'slurmjobid=~"^(?:1|2|4)$"'
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    assert any(matcher in q and "sum by (instance, gpu_type)" in q
               for q in ranges), ranges
    assert any(matcher in q and "count by (instance, gpu_type)" in q
               for q in ranges), ranges
    # non-running job 3 (h100) is gone; running jobs 1+2 stay h200 and
    # running MIG job 4 is its profile group
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    assert set(by_name) == {"h200", "h200_3g.71gb"}
    # the trend has no slurmjobid label: the matched selector must have
    # excluded h100 upstream
    assert set(data["trend"]) == {"h200", "h200_3g.71gb"}


def test_partitions_running_only_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    data = r.json()
    assert data["partitions"] == [] and data["trend"] == {}


def test_partitions_queue_summary(client, fake_prom, monkeypatch):
    # Queue eligibility by GPU type: same-type priority partitions
    # (gpu-h200 / gpu-h200-ellis both map to h200), the mixed whole+MIG
    # partition (maps to h200 + h200_3g.71gb), a typed %b request, an
    # untyped request eligible for several types, a constraints-only
    # row, a CPU-only job (omitted), and an explicit untyped GPU request
    # with no resolvable partition type (-> unknown). Wait history comes
    # from the same window's sacct-enriched jobs (jobs 1 and 2 start
    # 2026-08-28/29 under h200; job 4 under the MIG profile). Submit
    # times are naive-UTC so wait_s math is TZ-independent.
    monkeypatch.setattr(deps, "queue_pending", lambda: [
        # typed %b request: belongs ONLY to that type, despite %P
        {"jobid": "10", "user": "eve", "partition": "gpu-h200,gpu-h200-ellis",
         "state": "PENDING", "submit": "2026-08-30T12:26:40", "start": "",
         "reason": "(Resources)", "nodes": 2, "gpus": 4, "gpu_type": "h200"},
        # untyped GPU request on the mixed whole+MIG partition: counts
        # once per eligible union type (h200 AND h200_3g.71gb)
        {"jobid": "11", "user": "eve", "partition": "gpu-h200-mig",
         "state": "PENDING", "submit": "", "start": "",
         "reason": "(Resources)", "nodes": 4, "gpus": 1, "gpu_type": ""},
        # constraints-only row on a GPU-backed partition: eligible via %P
        {"jobid": "12", "user": "eve", "partition": "gpu-h100",
         "state": "PENDING", "submit": "2026-08-30T15:26:40", "start": "N/A",
         "reason": "(Priority)", "nodes": 2, "gpus": 0, "gpu_type": ""},
        # CPU-only job on a CPU partition: omitted entirely
        {"jobid": "13", "user": "eve", "partition": "batch",
         "state": "PENDING", "submit": "", "start": "",
         "reason": "(Dependency)", "nodes": 0, "gpus": 0, "gpu_type": ""},
        # explicit untyped GPU count on an unmapped partition: unknown
        {"jobid": "14", "user": "eve", "partition": "ghost",
         "state": "PENDING", "submit": "2026-08-30T14:26:40", "start": "",
         "reason": "(Resources)", "nodes": 1, "gpus": 1, "gpu_type": ""},
    ])
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 72}).json()
    assert data["queue_available"] is True
    assert data["wait_history_available"] is True
    coverage = data["wait_history_coverage"]
    assert coverage["complete"] is True
    assert coverage["failed_batches"] == 0
    assert coverage["records_examined"] == len(COMPLETED_HISTORY)
    q = data["queue"]
    # h200: job 10 typed exclusive (4 GPUs, the per-job request) + job 11
    # untyped flexible (1 GPU in both its rows); jobs 1+2 started
    # in-window with 3600s waits each -> P50/P90/avg 3600, 2 samples.
    # Job 3's 2026-08-27T00:00 start is pre-window (72h back is
    # 18:26:40) and must not contribute.
    assert q["h200"]["exclusive_jobs"] == 1
    assert q["h200"]["flexible_jobs"] == 1
    assert q["h200"]["eligible_jobs"] == 2
    assert q["h200"]["exclusive_gpus"] == 4
    assert q["h200"]["flexible_gpus"] == 1
    assert q["h200"]["eligible_gpus"] == 5
    assert q["h200"]["wait_p50_s"] == 3600
    assert q["h200"]["wait_p90_s"] == 3600
    assert q["h200"]["wait_avg_s"] == 3600
    assert q["h200"]["wait_samples"] == 2
    # ratio from the shared SACCT fixture: job 1 = 3600s wait for
    # 2 GPUs × 3600s = ratio 0.5; job 2 = 3600s wait for 1 GPU ×
    # 7200s = 0.5 -> median 0.5 wait-hours per GPU-hour
    assert q["h200"]["wait_per_gpu_hour_p50"] == 0.5
    # job 11's untyped request is also eligible for the MIG profile
    # (flexible there); job 4 (the in-window MIG start) waited 7200s:
    # submitted 22:00 the day before its 00:00 start
    assert q["h200_3g.71gb"]["eligible_gpus"] == 1
    assert q["h200_3g.71gb"]["wait_p50_s"] == 7200
    assert q["h200_3g.71gb"]["wait_p90_s"] == 7200
    assert q["h200_3g.71gb"]["wait_samples"] == 1
    # job 4: 7200s wait, 1 GPU × 3600s elapsed -> ratio 2.0
    assert q["h200_3g.71gb"]["wait_per_gpu_hour_p50"] == 2.0
    # constraints-only row on the h100 partition: 1 job, 0 GPUs, no waits
    assert q["h100"]["exclusive_jobs"] == 1
    assert q["h100"]["eligible_jobs"] == 1
    assert q["h100"]["eligible_gpus"] == 0
    assert q["h100"]["wait_samples"] == 0
    assert q["h100"]["wait_p50_s"] is None
    # explicit untyped GPU with no resolvable partition type
    assert q["unknown"]["exclusive_jobs"] == 1
    assert q["unknown"]["exclusive_gpus"] == 1
    assert q["unknown"]["wait_samples"] == 0
    # no pseudo-total row in the public queue map
    assert "__total__" not in q
    # unique view: 4 GPU-eligible physical jobs (13 excluded); exact GPU
    # demand 4 + 1 + 0 + 1 = 6 (single-node per-job requests)
    assert data["totals"] == {"unique_pending_jobs": 4,
                              "unique_gpus_requested": 6}
    # rows overlap (flexible job 11) and must never sum to the totals
    assert sum(r["eligible_jobs"] for r in q.values()) > \
        data["totals"]["unique_pending_jobs"]
    waiting = {j["jobid"]: j for j in data["waiting_jobs"]}
    assert set(waiting) == {"10", "11", "12", "14"}  # CPU-only 13 absent
    assert waiting["10"]["groups"] == ["h200"]       # typed beats %P union
    assert waiting["11"]["groups"] == ["h200", "h200_3g.71gb"]
    assert waiting["12"]["groups"] == ["h100"]
    assert waiting["14"]["groups"] == ["unknown"]
    assert waiting["10"]["wait_s"] == NOW - int(_epoch("2026-08-30T12:26:40"))
    assert waiting["11"]["wait_s"] is None          # unparsable submit
    assert waiting["10"]["gpu_total"] == 4          # the per-job request
    assert waiting["12"]["gpu_total"] == 0          # no GPU request
    assert waiting["14"]["gpu_total"] == 1
    # the raw partition string survives as metadata
    assert waiting["10"]["partition"] == "gpu-h200,gpu-h200-ellis"


def test_wait_statistics_percentiles():
    # Unit contract of the statistics helper: P50 is the ordinary median
    # (mean of the two middle values for an even count, integer-rounded),
    # P90 is nearest-rank ceil(0.9*n), and no samples reads as null
    # percentiles with a zero sample count. No bucket output remains.
    import domain.partitions as dp
    out = dp._wait_statistics([3600, 3600])
    assert out["wait_p50_s"] == 3600
    assert out["wait_p90_s"] == 3600
    # even count with distinct middles: median is their mean
    out = dp._wait_statistics([100, 200, 300, 400])
    assert out["wait_p50_s"] == 250
    # odd sum across an even sample count: integer-ROUNDED, not truncated
    # ([1, 2] -> 1.5 -> 2, not 1)
    out = dp._wait_statistics([1, 2])
    assert out["wait_p50_s"] == 2
    # nearest-rank P90 of 10 samples is the 9th ordered value
    out = dp._wait_statistics(list(range(1, 11)))
    assert out["wait_p90_s"] == 9
    # no valid samples: null percentiles and a zero sample count
    out = dp._wait_statistics([])
    assert out == {"wait_p50_s": None, "wait_p90_s": None,
                   "wait_avg_s": None, "wait_samples": 0,
                   "wait_per_gpu_hour_p50": None}
    # the median helper itself: empty -> None, odd -> middle,
    # even ints -> rounded mean, floats keep precision
    assert dp._median([]) is None
    assert dp._median([10, 20, 30]) == 20
    assert dp._median([1, 2]) == 2
    assert dp._median([0.5, 1.5]) == 1.0


def test_started_wait_summary_excludes_invalid_records(client, fake_prom,
                                                       monkeypatch):
    # started_wait_summary accepts only records whose submit and start
    # both parse, whose start falls inclusively inside the window, and
    # whose start is not before submit. Invalid/missing times are excluded
    # from every statistic, never read as 0; a group with no valid job
    # yields zero samples and null percentiles. The normalized ratio needs
    # a positive allocated GPU-hours figure (elapsed_s × gpus): records
    # without one are excluded from the ratio but still count as waits.
    import domain.partitions as dp
    recs = {
        # valid: 3600s wait, 1 GPU for 1h -> ratio 1.0
        "100": {"submit": "2026-08-30T09:00:00", "start": "2026-08-30T10:00:00",
                "elapsed_s": 3600, "gpus": 1},
        # valid: 7200s wait, 2 GPUs for 1h -> ratio 1.0 as well
        "104": {"submit": "2026-08-27T16:26:40", "start": "2026-08-27T18:26:40",
                "elapsed_s": 3600, "gpus": 2},
        # missing submit
        "101": {"submit": "", "start": "2026-08-30T10:00:00",
                "elapsed_s": 3600, "gpus": 1},
        # missing/unparsable start
        "102": {"submit": "2026-08-30T09:00:00", "start": "Unknown",
                "elapsed_s": 3600, "gpus": 1},
        # start before submit (negative duration)
        "103": {"submit": "2026-08-30T11:00:00", "start": "2026-08-30T10:00:00",
                "elapsed_s": 3600, "gpus": 1},
        # zero elapsed: counts toward waits, excluded from the ratio
        "105": {"submit": "2026-08-30T09:00:00", "start": "2026-08-30T09:30:00",
                "elapsed_s": 0, "gpus": 1},
    }
    records = [{**record, "state": "COMPLETED", "gpu_type": "h100",
                "node_list": "gpu2"} for record in recs.values()]
    out, coverage = dp.completed_wait_summary(
        records, {"gpu2": ["h100"]}, NOW - 72 * 3600, NOW)
    # Only the two positive-runtime records are valid completed samples.
    assert out["h100"] == {"wait_p50_s": 5400, "wait_p90_s": 7200,
                            "wait_avg_s": 5400, "wait_samples": 2,
                            "wait_per_gpu_hour_p50": 1.0}
    assert coverage["excluded"]["timestamps"] == 2
    assert coverage["excluded"]["negative_wait"] == 1
    assert coverage["excluded"]["nonpositive_elapsed"] == 1


def test_started_wait_summary_ratio_median_and_exclusions(client, fake_prom,
                                                          monkeypatch):
    # The ratio is the median of PER-JOB ratios (not wait-sum over
    # GPU-hour-sum, which would weight large jobs), rounded to two
    # decimals. A zero wait with positive job size is a valid 0.0.
    import domain.partitions as dp
    # per-job ratios: 1800/(1800*1)=1.0, 3600/(3600*4)=0.25, 0/(900*2)=0.0
    # -> median 0.25; ratio-of-sums would be 5400/15300h ~= 0.35 instead.
    recs = {
        "200": {"submit": "2026-08-30T09:00:00", "start": "2026-08-30T09:30:00",
                "elapsed_s": 1800, "gpus": 1},
        "201": {"submit": "2026-08-30T10:00:00", "start": "2026-08-30T11:00:00",
                "elapsed_s": 3600, "gpus": 4},
        "202": {"submit": "2026-08-30T12:00:00", "start": "2026-08-30T12:00:00",
                "elapsed_s": 900, "gpus": 2},
    }
    records = [{**record, "state": "COMPLETED", "gpu_type": "h100",
                "node_list": "gpu2"} for record in recs.values()]
    out, _ = dp.completed_wait_summary(
        records, {"gpu2": ["h100"]}, NOW - 72 * 3600, NOW)
    assert out["h100"]["wait_per_gpu_hour_p50"] == 0.25
    assert out["h100"]["wait_samples"] == 3
    # Typed allocation is mandatory: an untyped/zero-GPU record is excluded.
    records = [{"state": "COMPLETED", "submit": "2026-08-30T09:00:00",
                "start": "2026-08-30T10:00:00", "elapsed_s": 3600,
                "gpus": 0, "gpu_type": "", "node_list": "gpu2"}]
    out, coverage = dp.completed_wait_summary(
        records, {"gpu2": ["h100"]}, NOW - 72 * 3600, NOW)
    assert out[dp.WAIT_TOTAL_KEY] == dp.wait_empty()
    assert coverage["excluded"]["missing_typed_gpu_allocation"] == 1


def test_completed_wait_summary_no_records(client, fake_prom):
    import domain.partitions as dp
    out, coverage = dp.completed_wait_summary({}, {}, NOW - 3600, NOW)
    assert out[dp.WAIT_TOTAL_KEY] == dp.wait_empty()
    assert coverage == {"records_examined": 0, "valid_samples": {},
                        "excluded": {}, "failed_batches": 0,
                        "complete": True}


def test_partitions_waiting_zero_pending_partition_visible(client, fake_prom):
    # default fixture stub: reachable squeue, zero pending jobs — but the
    # utilization groups must still appear in the queue summary with
    # genuine zeros (squeue answered: 0 is a real count here).
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    q = data["queue"]
    for name in ("h200", "h100", "h200_3g.71gb"):
        row = q[name]
        assert row["exclusive_jobs"] == 0 and row["flexible_jobs"] == 0
        assert row["eligible_jobs"] == 0
        assert row["exclusive_gpus"] == 0 and row["flexible_gpus"] == 0
        assert row["eligible_gpus"] == 0
        assert row["wait_samples"] == 0
        assert row["wait_p50_s"] is None
    assert data["totals"] == {"unique_pending_jobs": 0,
                              "unique_gpus_requested": 0}
    assert data["waiting_jobs"] == []


def test_partitions_queue_unavailable_is_not_empty(client, fake_prom,
                                                   monkeypatch):
    def _boom():
        raise slurm.SlurmError("squeue is not available")

    monkeypatch.setattr(deps, "queue_pending", _boom)
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 72}).json()
    assert data["queue_available"] is False
    assert data["waiting_jobs"] == []
    # pending demand is unknown, never a fabricated 0
    assert data["totals"] == {"unique_pending_jobs": None,
                              "unique_gpus_requested": None}
    # sacct history is independent: it still works
    assert data["wait_history_available"] is True
    # the utilization group rows keep null pending fields but valid waits
    assert data["queue"]["h200"]["exclusive_jobs"] is None
    assert data["queue"]["h200"]["eligible_jobs"] is None
    assert data["queue"]["h200"]["wait_samples"] == 2
    assert data["queue"]["h200"]["wait_p50_s"] == 3600


def test_partitions_wait_history_unavailable_keeps_queue(client, fake_prom,
                                                         monkeypatch):
    def _boom(start_iso, end_iso):
        raise slurm.SlurmError("sacct is not available")

    monkeypatch.setattr(deps, "completed_jobs", _boom)
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    assert data["wait_history_available"] is False
    assert data["queue_available"] is True
    # current pending figures survive the history failure
    assert data["totals"]["unique_pending_jobs"] == 0
    # wait statistics read as unknown — all null — not as zero samples

    row = data["queue"]["h200"]
    assert row["wait_p50_s"] is None
    assert row["wait_p90_s"] is None
    assert row["wait_avg_s"] is None
    assert row["wait_samples"] is None
    assert row["wait_per_gpu_hour_p50"] is None

def test_partitions_partial_wait_history_keeps_successful_metrics(
        client, fake_prom, monkeypatch):
    monkeypatch.setattr(
        deps, "completed_jobs",
        lambda start_iso, end_iso: (COMPLETED_HISTORY, {
            "failed_batches": 1, "successful_batches": 2, "complete": False,
        }))
    data = client.get("/api/partitions/queue", params={"since_hours": 72}).json()
    assert data["wait_history_available"] is True
    assert data["wait_history_coverage"]["complete"] is False
    assert data["wait_history_coverage"]["failed_batches"] == 1
    assert data["queue"]["h200"]["wait_samples"] == 2


def test_partitions_queue_empty_when_nothing_pending(client, fake_prom):
    # default fixture stub: reachable squeue, zero pending jobs
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    assert data["queue_available"] is True
    assert data["totals"]["unique_pending_jobs"] == 0
    assert data["totals"]["unique_gpus_requested"] == 0
    assert data["queue"]["h200"]["wait_samples"] == 0
    assert data["queue"]["h200"]["wait_p50_s"] is None




def test_partitions_core_response_has_no_queue_fields(client, fake_prom):
    # The split: /api/partitions returns only Prometheus-backed data so
    # it renders without waiting on squeue/sacct; queue fields moved to
    # /api/partitions/queue.
    data = client.get("/api/partitions", params={"since_hours": 24}).json()
    assert set(data) == {"window", "step", "partitions", "trend"}


def test_partitions_queue_refetches_squeue_per_window(client, fake_prom,
                                                      monkeypatch):
    # The live queue is not a windowed metric, but a window change must
    # still perform a FRESH squeue call (no constant-key cache) and the
    # windowed wait columns must follow the requested window. Snapshot A
    # (24h): one typed h200 job; snapshot B (72h): a second job joined.
    snap_a = [{"jobid": "20", "user": "eve",
               "partition": "gpu-h200", "state": "PENDING",
               "submit": "2026-08-30T12:26:40", "start": "",
               "reason": "(Resources)", "nodes": 1, "gpus": 2,
               "gpu_type": "h200"}]
    snap_b = snap_a + [{"jobid": "21", "user": "eve",
                        "partition": "gpu-h200", "state": "PENDING",
                        "submit": "2026-08-30T13:26:40", "start": "",
                        "reason": "(Priority)", "nodes": 1, "gpus": 1,
                        "gpu_type": "h200"}]
    calls = []

    def _queue():
        calls.append(1)
        return snap_b if len(calls) > 1 else snap_a

    monkeypatch.setattr(deps, "queue_pending", _queue)
    first = client.get("/api/partitions/queue",
                       params={"since_hours": 24}).json()
    assert first["queue"]["h200"]["exclusive_jobs"] == 1
    assert {j["jobid"] for j in first["waiting_jobs"]} == {"20"}
    second = client.get("/api/partitions/queue",
                        params={"since_hours": 72}).json()
    # a second, fresh squeue snapshot — not a cached replay
    assert len(calls) == 2
    assert second["queue"]["h200"]["exclusive_jobs"] == 2
    assert {j["jobid"] for j in second["waiting_jobs"]} == {"20", "21"}
    # wait history stays window-scoped: jobs 1+2 start in-window for 72h
    assert second["queue"]["h200"]["wait_samples"] == 2
    assert second["queue"]["h200"]["wait_p50_s"] == 3600


def test_partitions_vram_records(client):
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    by_id = {j["jobid"]: j for j in data["jobs"]}
    # per-GPU peaks averaged: job1 (12+18)/2, job2 30, job3 8, job4 22
    assert by_id["1"]["vram_gb"] == 15.0
    # ``partition`` is the group label (MIG jobs carry their profile group)
    assert by_id["1"]["partition"] == "h200"
    assert by_id["3"]["partition"] == "h100"
    assert by_id["4"]["partition"] == "h200_3g.71gb"
    # mean_util from the utilization window (time-weighted mean)
    assert by_id["1"]["mean_util"] == pytest.approx(50.0)
    assert by_id["2"]["mean_util"] == pytest.approx(10.0)
    assert by_id["3"]["mean_util"] == pytest.approx(92.5)
    assert by_id["4"]["mean_util"] == pytest.approx(85.0)
    # gpu_hours from sacct (gpus x elapsed)
    assert by_id["1"]["gpu_hours"] == pytest.approx(2 * 3600 / 3600)
    assert by_id["2"]["gpu_hours"] == pytest.approx(1 * 7200 / 3600)
    assert by_id["3"]["gpu_hours"] == pytest.approx(4 * 86400 / 3600)
    assert by_id["4"]["gpu_hours"] == pytest.approx(1 * 3600 / 3600)
    # sorted by gpu_hours desc
    hours = [j["gpu_hours"] for j in data["jobs"]]
    assert hours == sorted(hours, reverse=True)
    # total counts all candidates (here 4, under the cap)
    assert data["total"] == 4


def test_partitions_vram_discloses_truncation(client, fake_prom, monkeypatch):
    monkeypatch.setattr(deps, "VRAM_RECORD_CAP", 2)
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    # 4 candidates but the cap of 2 is enforced on the payload…
    assert len(data["jobs"]) == 2
    # …while total still reports the full candidate count
    assert data["total"] == 4


def test_partitions_vram_running_only_filters_live(client, fake_prom):
    r = client.get("/api/partitions/vram",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    ids = {j["jobid"] for j in r.json()["jobs"]}
    assert ids == {"1", "2", "4"}  # job 3 has no live GPU series
    # the VRAM query carries the live-ID matcher
    matcher = 'slurmjobid=~"^(?:1|2|4)$"'
    assert any(matcher in q and "max by (slurmjobid, instance, gpu)" in q
               for t, q in fake_prom.calls if t == "range")


def test_partitions_vram_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/partitions/vram",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    assert r.json()["jobs"] == []
    # no range query may be issued when nothing is running
    assert not [q for t, q in fake_prom.calls if t == "range"]


def test_partitions_vram_partition_filter(client):
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24, "partition": "h200"}).json()
    assert data["total"] == 2
    assert {j["jobid"] for j in data["jobs"]} == {"1", "2"}
    # the h200 type filter selects the whole-GPU group only; the MIG
    # slice is reached through its profile group label
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24,
                              "partition": "h200_3g.71gb"}).json()
    assert data["total"] == 1
    assert [j["jobid"] for j in data["jobs"]] == ["4"]
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24, "partition": "h100"}).json()
    assert data["total"] == 1
    assert [j["jobid"] for j in data["jobs"]] == ["3"]
    # unknown type: no candidates, empty payload
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24, "partition": "b300"}).json()
    assert data["total"] == 0 and data["jobs"] == []


def test_slurm_error_maps_to_502(client, fake_prom, monkeypatch):
    def boom(ids, start_iso=None, **kw):
        raise appmod.SlurmError("sacct timed out")

    monkeypatch.setattr(deps, "sacct_jobs", boom)
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    # the handler must produce the 502 itself; a reversed
    # JSONResponse(status, body) call turns this into a 500.
    assert r.status_code == 502
    assert r.json()["error"] == "slurm_unreachable"


def test_prometheus_error_maps_to_502(client, fake_prom, monkeypatch):
    def boom():
        raise appmod.PrometheusError("prometheus down")

    monkeypatch.setattr(deps, "get_prom", boom)
    # /api/nodes degrades gracefully on Prometheus outages (by design);
    # the vram endpoint propagates, exercising the handler.
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "prometheus_unreachable"


def test_nodes_endpoint(client):
    r = client.get("/api/nodes")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 5  # gpu_only=True: gpu1, gpu2, gpu3, gpu49, gpu50
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["gpu1"]["current_util"] == 55.5
    assert by_name["gpu1"]["current_vram"] == 41.2
    assert by_name["gpu1"]["active_jobs"][0]["jobid"] == "1"
    assert by_name["gpu2"]["gpus_alloc"] == 3


def test_nodes_gpu_group(client):
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    # A node's gpu_group is its own parsed scalar type (the MIG profile
    # for an all-MIG node); partition membership is irrelevant.
    assert by_name["gpu1"]["gpu_group"] == "h200"
    assert by_name["gpu2"]["gpu_group"] == "h100"
    assert by_name["gpu3"]["gpu_group"] == "h200"
    # gpu49 is mixed (4 whole + 8 MIG): its scalar gpu_type is the first
    # GRES entry; per-type capacity accounting splits its MIG profile out.
    assert by_name["gpu49"]["gpu_group"] == "h200"
    assert by_name["gpu49"]["gpu_type"] == "h200"
    assert by_name["gpu49"]["gpus"] == 12


def test_nodes_gpus_alloc(client):
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    assert by_name["gpu1"]["gpus_alloc"] == 2  # from scontrol AllocTRES
    assert by_name["gpu1"]["cpus_alloc"] == 16  # from scontrol CPUAlloc


def test_nodes_gpus_alloc_ignores_silent_exporter(client, fake_prom, monkeypatch):
    # A node scontrol reports as fully allocated, but whose monitoring
    # exporter has stopped publishing any per-GPU series at all (gpu49 in
    # reality, after its today's mixed-GRES reconfiguration): the live
    # Prometheus "count by (instance, job, gpu_type)" query has nothing
    # for this instance, yet gpus_alloc must still read scontrol's true
    # allocation, not silently drop to 0.
    monkeypatch.setattr(deps, "show_nodes", lambda: [
        {"name": "gpu99", "state": "MIXED", "state_full": "MIXED",
         "reason": "", "partitions": "gpu-h200-71g-ia", "cpus": 128,
         "gpus": 12, "gpu_type": "h200", "gpus_alloc": 12,
         "cpus_alloc": 56, "free_mem": 1000, "real_mem": 5000},
    ])
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    assert by_name["gpu99"]["gpus_alloc"] == 12


def test_nodes_detail_vram_keeps_coresident_jobs(client):
    r = client.get("/api/nodes/gpu1", params={"view": "6"})
    assert r.status_code == 200
    data = r.json()
    vram = data["series"]["vram"]
    # Two co-located 1-GPU jobs both label their device gpu="0"; the fix
    # keeps slurmjobid in the grouping so they must remain separate series.
    assert len(vram) == 2
    assert {s["metric"]["slurmjobid"] for s in vram} == {"1", "2"}


def test_node_detail_job_start_view(client, fake_prom):
    r = client.get("/api/nodes/gpu1", params={"view": "job_start"})
    assert r.status_code == 200
    data = r.json()
    assert data["view"] == "job_start"
    # earliest sacct start of live jobs 1 and 2, within the 7-day clamp
    assert data["window"]["start"] == int(_epoch(JOB1_START))
    assert data["window"]["end"] == NOW


def test_node_detail_job_start_fallback_no_jobs(client, fake_prom):
    fake_prom.job_start_ids = set()
    data = client.get("/api/nodes/gpu1",
                      params={"view": "job_start"}).json()
    assert data["window"]["start"] == NOW - 6 * 3600


def test_node_detail_numeric_views(client):
    for view, hours in (("1", 1), ("6", 6), ("24", 24)):
        data = client.get("/api/nodes/gpu1", params={"view": view}).json()
        assert data["view"] == view
        assert data["window"]["start"] == NOW - hours * 3600


def test_node_detail_rejects_bad_view(client):
    r = client.get("/api/nodes/gpu1", params={"view": "12"})
    assert r.status_code == 422
    # the old window_hours parameter is gone and ignored
    data = client.get("/api/nodes/gpu1", params={"window_hours": 6}).json()
    assert data["view"] == "job_start"
    assert data["window"]["start"] == int(_epoch(JOB1_START))


def test_nodes_refresh_bypasses_cache(client, fake_prom):
    client.get("/api/nodes")
    assert fake_prom.nodes_calls == 1
    client.get("/api/nodes")
    assert fake_prom.nodes_calls == 1  # served from the 30 s cache
    client.get("/api/nodes", params={"refresh": "true"})
    assert fake_prom.nodes_calls == 2  # forced a fresh scontrol read
    # refresh also purges the Prometheus node cache: the second fresh read
    # re-queries all four instant node metrics.
    ranges = [q for t, q in fake_prom.calls if t == "instant"]
    assert len(ranges) == 8  # 2 reads x 4 instant queries
    assert fake_prom.clear_cache_calls == 1


def test_deep_link_routes_serve_spa(client):
    for path in ["/jobs", "/partitions", "/nodes", "/job/19807768",
                 "/node/dgx1", "/partition/gpu-h100"]:
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Triton GPU Efficiency Dashboard" in r.text


def test_aggregate_partition_stats_fixture():
    stats = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1",
                    "gpu_type": "h200"},
         "values": [[1, "40"], [2, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1",
                    "gpu_type": "NVIDIA H200"},
         "values": [[1, "10"]]},
    ]
    out = domain_partitions.aggregate_partition_stats(
        stats, {"gpu1": ["h200"]})
    # both series canonicalize to h200 and merge into one group
    assert len(out) == 1
    assert out[0]["name"] == "h200"
    assert out[0]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert out[0]["max_util"] == 60.0
    assert out[0]["job_count"] == 2
    assert set(out[0]) == {"name", "mean_util", "max_util", "job_count"}


def test_jobs_mean_util_field_and_no_duplicate_efficiency_field(client):
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    by_id = {j["jobid"]: j for j in data["jobs"]}
    assert by_id["1"]["mean_util"] == 50.0  # mean of 40, 60
    assert by_id["2"]["mean_util"] == 10.0
    assert by_id["3"]["mean_util"] == 92.5
    assert by_id["4"]["mean_util"] == 85.0
    # "efficiency" was a duplicate of mean_util (T-26 collapsed it); the
    # charts and sort still speak of "efficiency" as a concept, but no job
    # dict carries a field by that name any more.
    assert "efficiency" not in by_id["1"]


def test_efficiency_histogram_all_buckets_zero_filled():
    # No jobs at all: every bucket from 0-100 is still present, at zero —
    # a caller must be able to tell "no data" from "no bar drawn here".
    hist = domain_jobs.efficiency_histogram([])
    assert len(hist) == 10
    assert [b["bucket_start"] for b in hist] == list(range(0, 100, 10))
    assert [b["bucket_end"] for b in hist] == list(range(10, 101, 10))
    assert all(b["gpu_hours"] == 0.0 for b in hist)


def test_efficiency_histogram_bins_and_sums_gpu_hours():
    jobs = [
        {"jobid": "1", "mean_util": 5.0, "gpu_hours_eff": 2.0},
        {"jobid": "2", "mean_util": 8.0, "gpu_hours_eff": 1.0},
        {"jobid": "3", "mean_util": 92.5, "gpu_hours_eff": 4.0},
        # Exactly 100 (the theoretical max) must land in the last bucket,
        # not spill past it into a nonexistent 100-110 bucket.
        {"jobid": "4", "mean_util": 100.0, "gpu_hours_eff": 1.5},
    ]
    hist = domain_jobs.efficiency_histogram(jobs)
    by_bucket = {(b["bucket_start"], b["bucket_end"]): b["gpu_hours"] for b in hist}
    assert by_bucket[(0, 10)] == 3.0  # jobs 1 + 2
    assert by_bucket[(90, 100)] == 5.5  # jobs 3 + 4
    assert by_bucket[(10, 20)] == 0.0


def test_jobs_efficiency_histogram(client):
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    hist = data["efficiency_histogram"]
    by_bucket = {(b["bucket_start"], b["bucket_end"]): b["gpu_hours"] for b in hist}
    assert len(hist) == 10
    # Pre-enrich gpu_hours_eff, matching the fixture's four jobs: job 1
    # (mean_util 50 -> 0.03), job 2 (mean_util 10 -> 0.0, contributes
    # nothing), job 3 (mean_util 92.5 -> 0.06), job 4 (mean_util 85.0 ->
    # 0.06).
    assert by_bucket[(50, 60)] == 0.03
    assert by_bucket[(80, 90)] == 0.06
    assert by_bucket[(90, 100)] == 0.06
    assert sum(v for k, v in by_bucket.items()
               if k not in {(50, 60), (80, 90), (90, 100)}) == 0


def test_jobs_histogram_bounded_by_search(client):
    data = client.get("/api/jobs",
                      params={"since_hours": 24, "search": "train.sh"}).json()
    # search matches only job 1's sacct name (mean_util=50). This second
    # histogram is computed after enrich(), so it uses job 1's
    # allocation-based gpu_hours_eff (1.0), not the pre-enrich estimate the
    # first (unsearched) histogram uses — see the api/jobs.py comment on
    # why the two histograms in one response can use different bases.
    assert [j["jobid"] for j in data["jobs"]] == ["1"]
    hist = data["efficiency_histogram"]
    by_bucket = {(b["bucket_start"], b["bucket_end"]): b["gpu_hours"] for b in hist}
    assert by_bucket[(50, 60)] == 1.0
    assert sum(v for k, v in by_bucket.items() if k != (50, 60)) == 0


def test_jobs_total_candidates_reflects_pre_limit_count(client):
    # No limit in play: every matching job is both a candidate and returned.
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    assert data["total_candidates"] == 4
    assert data["count"] == 4

    # A tight limit cuts "jobs" but total_candidates still reports every job
    # that matched the window/partition/user filters before that cut — the
    # signal the UI needs to tell "no such job" apart from "outside the top
    # N by GPU-hours."
    data = client.get("/api/jobs", params={"since_hours": 24, "limit": 2}).json()
    assert data["total_candidates"] == 4
    assert data["count"] == 2

    # A search for a job excluded by the tight limit finds nothing, but
    # total_candidates still shows it was among the window's candidates.
    data = client.get("/api/jobs",
                      params={"since_hours": 24, "limit": 2,
                              "search": "train.sh"}).json()
    assert data["jobs"] == []
    assert data["total_candidates"] == 4


def test_partitions_mean_occupancy(client):
    data = client.get("/api/partitions", params={"since_hours": 24}).json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # occupancy = window-average concurrent series / capacity:
    # h200: merged counts 3 @t0 and 2 @t1 -> mean 2.5 / 20 = 12.5%;
    # h100: 1 / 8 = 12.5%; h200_3g.71gb: 1 / 8 = 12.5% (gpu49's slices).
    # gh200 has capacity but no samples, so its occupancy is explicitly null.
    assert by_name["h200"]["mean_occupancy"] == 12.5
    assert by_name["h100"]["mean_occupancy"] == 12.5
    assert by_name["h200_3g.71gb"]["mean_occupancy"] == 12.5
    assert by_name["gh200"]["mean_occupancy"] is None


def test_partitions_mean_occupancy_running_only(client, fake_prom):
    data = client.get("/api/partitions",
                      params={"since_hours": 24, "running_only": "true"}).json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # Non-running h100 and idle gh200 are absent. The matched running
    # whole-H200 samples have counts 3 and 2, so occupancy is 12.5%.
    assert set(by_name) == {"h200", "h200_3g.71gb"}
    assert by_name["h200"]["mean_occupancy"] == 12.5
    assert by_name["h200_3g.71gb"]["mean_occupancy"] == 12.5
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    assert any('slurmjobid=~"^(?:1|2|4)$"' in q
               and "count by (instance, gpu_type)" in q
               for q in ranges), ranges


def test_gpu_capacity_mixed_whole_and_mig_node():
    # gpu49 in reality: 4 whole H200 GPUs + 8 MIG h200_3g.71gb slices on
    # one node. Each group must get only its own type's count, not the
    # node's combined total (12) and not the other group's slice/whole
    # count either.
    nodes = [
        {"name": "gpu49", "partitions": "gpu-h200-71g-ia",
         "gres": [("h200", 4), ("h200_3g.71gb", 8)]},
        {"name": "gpu50", "partitions": "gpu-h200-141g",
         "gres": [("h200", 8)]},
    ]
    groups = [{"name": "h200"}, {"name": "h200_3g.71gb"}]
    allocs = {"h200": 5, "h200_3g.71gb": 3}
    out = domain_partitions.gpu_capacity(groups, {}, nodes, allocs)
    by_name = {g["name"]: g for g in out}
    assert by_name["h200"]["gpus_total"] == 12  # gpu49's 4 + gpu50's 8
    assert by_name["h200"]["gpus_alloc"] == 5
    assert by_name["h200_3g.71gb"]["gpus_total"] == 8  # gpu49's MIG slices only
    assert by_name["h200_3g.71gb"]["gpus_alloc"] == 3


def test_completed_wait_summary_separates_mig_and_excludes_noncompleted():
    records = [
        {"state": "COMPLETED", "submit": "2026-08-30T09:00:00",
         "start": "2026-08-30T09:05:00", "elapsed_s": 300, "gpus": 1,
         "gpu_type": "h200", "node_list": "gpu49"},
        {"state": "COMPLETED", "submit": "2026-08-30T08:00:00",
         "start": "2026-08-30T10:00:00", "elapsed_s": 3600, "gpus": 2,
         "gpu_type": "h200", "node_list": "gpu49"},
        {"state": "COMPLETED", "submit": "2026-08-30T08:00:00",
         "start": "2026-08-30T12:00:00", "elapsed_s": 3600, "gpus": 1,
         "gpu_type": "h200_3g.71gb", "node_list": "gpu49"},
        {"state": "RUNNING", "submit": "2026-08-30T08:00:00",
         "start": "2026-08-30T10:00:00", "elapsed_s": 3600, "gpus": 1,
         "gpu_type": "h200", "node_list": "gpu49"},
        {"state": "FAILED", "submit": "2026-08-30T08:00:00",
         "start": "2026-08-30T10:00:00", "elapsed_s": 3600, "gpus": 1,
         "gpu_type": "h200", "node_list": "gpu49"},
    ]
    out, coverage = domain_partitions.completed_wait_summary(
        records, {"gpu49": ["h200", "h200_3g.71gb"]},
        NOW - 72 * 3600, NOW)
    assert out["h200"]["wait_samples"] == 2  # includes the five-minute job
    assert out["h200"]["wait_per_gpu_hour_p50"] == 1.0
    assert out["h200_3g.71gb"]["wait_samples"] == 1
    assert out["h200_3g.71gb"]["wait_per_gpu_hour_p50"] == 4.0
    assert coverage["valid_samples"] == {"h200": 2, "h200_3g.71gb": 1}
    assert coverage["excluded"] == {"state": 2}
