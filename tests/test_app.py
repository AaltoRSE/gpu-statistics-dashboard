"""Endpoint tests with a fake Prometheus client.

Run: .venv/bin/python -m pytest tests/ -q
"""

import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402

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
          "partition": "gpu-h100", "state": "RUNNING", "start": JOB1_START,
          "end": JOB1_END, "elapsed_s": 3600, "gpus": 2, "gpu_type": "h100",
          "node_list": "gpu1", "ncpus": 8},
    "2": {"jobid": "2", "name": "fin.sh", "user": "bob", "account": "acc",
          "partition": "gpu-h100", "state": "COMPLETED", "start": JOB2_START,
          "end": JOB2_END, "elapsed_s": 7200, "gpus": 1, "gpu_type": "h100",
          "node_list": "gpu1", "ncpus": 4},
    "3": {"jobid": "3", "name": "infer.sh", "user": "carol", "account": "acc",
          "partition": "gpu-h200", "state": "COMPLETED", "start": JOB3_START,
          "end": JOB3_END, "elapsed_s": 86400, "gpus": 4, "gpu_type": "h200",
          "node_list": "gpu2", "ncpus": 8},
    # MIG slice on gpu49: distinct user, same Slurm partition as job 3
    # but its node's GRES is a MIG profile, so it must group separately in
    # Partitions (and its capacity belongs only to the profile group).
    "4": {"jobid": "4", "name": "mig.sh", "user": "dave", "account": "acc",
          "partition": "gpu-h200", "state": "RUNNING", "start": JOB2_START,
          "end": JOB1_END, "elapsed_s": 3600, "gpus": 1,
          "gpu_type": "h200_3g.71gb", "node_list": "gpu49", "ncpus": 32},
}

NODES = [
    {"name": "gpu1", "state": "MIXED", "state_full": "MIXED",
     "partitions": "gpu-h100", "cpus": 64, "gpus": 8, "gpu_type": "h100",
     "cpus_alloc": 16, "free_mem": 1000, "real_mem": 5000},
    {"name": "gpu2", "state": "ALLOCATED", "state_full": "ALLOCATED*",
     "partitions": "gpu-h200", "cpus": 64, "gpus": 8, "gpu_type": "h200",
     "cpus_alloc": 32, "free_mem": 900, "real_mem": 5000},
    # idle h100 node: must be included in the h100 group's capacity total
    {"name": "gpu3", "state": "IDLE", "state_full": "IDLE",
     "partitions": "gpu-h100", "cpus": 64, "gpus": 8, "gpu_type": "h100",
     "cpus_alloc": 0, "free_mem": 4000, "real_mem": 5000},
    # MIG node: its GRES is a MIG profile, so its capacity belongs to the
    # profile group, never to the whole-GPU h200 pool.
    {"name": "gpu49", "state": "ALLOCATED", "state_full": "ALLOCATED",
     "partitions": "gpu-h200", "cpus": 128, "gpus": 8,
     "gpu_type": "h200_3g.71gb",
     "cpus_alloc": 32, "free_mem": 900, "real_mem": 5000},
    # drained node: qualifiers and reason must survive parsing
    {"name": "gpu51", "state": "IDLE", "state_full": "IDLE+DRAIN",
     "reason": "maintenance: firmware update scheduled",
     "partitions": "gpu-h200", "cpus": 64, "gpus": 8, "gpu_type": "h200",
     "cpus_alloc": 0, "free_mem": 100, "real_mem": 5000},
    {"name": "csl1", "state": "IDLE", "state_full": "IDLE",
     "partitions": "batch", "cpus": 40, "gpus": 0, "gpu_type": "",
     "cpus_alloc": 0, "free_mem": 100, "real_mem": 200},
]


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
    # Partition summary keeps slurmjobid + instance + job + gpu_type so
    # job identity, the capacity join, and the partition/MIG grouping
    # survive aggregation. ``job`` is the Slurm partition (the metric's
    # partition label); job 4 runs on MIG node gpu49 under the same
    # partition but its gpu_type must split it into the profile group.
    _PART_SUMMARY = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "gpu-h100",
                    "gpu_type": "h100"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "job": "gpu-h100",
                    "gpu_type": "h100"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "job": "gpu-h200",
                    "gpu_type": "h200"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "job": "gpu-h200",
                    "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "80"], [1120, "90"]]},
    ]
    _PART_TREND = [
        {"metric": {"job": "gpu-h100", "gpu_type": "h100"},
         "values": [[1000, "25.0"], [1120, "35.0"]]},
        {"metric": {"job": "gpu-h200", "gpu_type": "h200"},
         "values": [[1000, "92.5"]]},
        {"metric": {"job": "gpu-h200", "gpu_type": "h200_3g.71gb"},
         "values": [[1000, "80.0"], [1120, "90.0"]]},
    ]
    _JOBS_UTIL = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "gpu-h100",
                    "user": "alice", "gpu_type": "h100"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "job": "gpu-h100",
                    "user": "bob", "gpu_type": "h100"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "job": "gpu-h200",
                    "user": "carol", "gpu_type": "h200"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "job": "gpu-h200",
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
            if "count by (job, gpu_type)" in query:  # partition occupancy
                # concurrent allocated series per (partition, gpu_type)
                ids = self._matchers(query)
                n = {}
                for s in self._PART_SUMMARY:
                    if ids is None or s["metric"]["slurmjobid"] in ids:
                        g = (s["metric"]["job"], s["metric"]["gpu_type"])
                        n[g] = n.get(g, 0) + 1
                return [
                    {"metric": {"job": g[0], "gpu_type": g[1]},
                     "values": [[1000, str(c)], [1120, str(c)]]}
                    for g, c in sorted(n.items())
                ]
            if "avg by (job, gpu_type)" in query:  # partition trend
                # a matched selector only yields groups with matching jobs
                ids = self._matchers(query)
                if ids is None:
                    return self._PART_TREND
                allowed = {(s["metric"]["job"], s["metric"]["gpu_type"])
                           for s in self._PART_SUMMARY
                           if s["metric"]["slurmjobid"] in ids}
                return [t for t in self._PART_TREND
                        if (t["metric"]["job"], t["metric"]["gpu_type"])
                        in allowed]
            if "max by (slurmjobid, instance, job, gpu_type)" in query:
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
                {"metric": {"instance": "gpu1", "job": "gpu-h100",
                            "gpu_type": "h100"},
                 "value": [1, "2"]},
                {"metric": {"instance": "gpu2", "job": "gpu-h200",
                            "gpu_type": "h200"},
                 "value": [1, "3"]},
                {"metric": {"instance": "gpu49", "job": "gpu-h200",
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
    fake = FakeProm()
    monkeypatch.setattr(appmod, "get_prom", lambda: fake)
    monkeypatch.setattr(appmod, "_cache", {})
    monkeypatch.setattr(appmod, "sacct_jobs",
                        lambda ids, start_iso=None, **kw: {j: SACCT[j] for j in ids
                                                           if j in SACCT})
    # No active controller jobs by default; tests opt in to a snapshot.
    monkeypatch.setattr(appmod, "show_jobs", lambda: {})

    def _show_nodes():
        fake.nodes_calls += 1
        return list(NODES)

    monkeypatch.setattr(appmod, "show_nodes", _show_nodes)
    monkeypatch.setattr(appmod.time, "time", lambda: NOW)
    return fake


@pytest.fixture()
def client(fake_prom):
    return TestClient(appmod.app)


def _epoch(s):
    return datetime.fromisoformat(s).timestamp()


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
    assert alice["gpu_types"] == ["h100"]
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
    monkeypatch.setattr(appmod, "_fetch_job_window",
                        lambda since_hours: (jobs, 1, 2, 120))
    monkeypatch.setattr(appmod, "_running_gpu_job_ids", lambda: set())
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
    monkeypatch.setattr(appmod, "sacct_jobs", lambda ids, start_iso=None, **kw: {})
    monkeypatch.setattr(appmod, "show_jobs", lambda: {
        "42_1": _scontrol_row("42_1", "42", "1", "gpu2",
                              start=JOB2_START, elapsed=7200),
        "42_0": _scontrol_row("42_0", "42", "0", "gpu1",
                              start=JOB1_START, elapsed=3600),
    })
    jobs = [{"jobid": "42", "nodes": ["gpu1", "gpu2"], "mean_util": 50.0,
             "gpu_hours_eff": 0.5}]
    appmod._enrich(jobs)
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
    monkeypatch.setattr(appmod, "sacct_jobs",
                        lambda ids, start_iso=None, **kw: {})
    monkeypatch.setattr(appmod, "show_jobs", lambda: {
        "44_0": _scontrol_row("44_0", "44", "0", "gpu2"),
    })
    jobs = [{"jobid": "44", "nodes": ["gpu1"], "mean_util": 10.0,
             "gpu_hours_eff": 0.1}]
    appmod._enrich(jobs)
    for key in ("name", "state", "start", "gpus", "node_list"):
        assert key not in jobs[0]


def test_enrich_scontrol_failure_falls_back_to_sacct(client, fake_prom,
                                                      monkeypatch):
    from slurm import SlurmError

    monkeypatch.setattr(appmod, "show_jobs",
                        lambda: (_ for _ in ()).throw(SlurmError("boom")))
    jobs = [{"jobid": "1", "nodes": ["gpu1"], "mean_util": 40.0,
             "gpu_hours_eff": 0.4}]
    appmod._enrich(jobs)
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
    monkeypatch.setattr(appmod, "show_jobs", lambda: {})
    monkeypatch.setattr(appmod, "sacct_jobs", lambda ids, start_iso=None, **kw: {
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
    appmod._enrich(jobs)
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
    monkeypatch.setattr(appmod, "show_jobs", lambda: {})
    monkeypatch.setattr(appmod, "sacct_jobs", lambda ids, start_iso=None, **kw: {
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
    appmod._enrich(jobs)
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

def test_partitions_groups_by_partition(client):
    r = client.get("/api/partitions", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # job 4 runs on MIG node gpu49, so the whole-GPU gpu-h200 group is split
    # out into the MIG profile group.
    assert set(by_name) == {"gpu-h100", "gpu-h200", "h200_3g.71gb"}
    # gpu-h100: samples 40, 60 (job1) and 10 (job2) -> time-weighted mean 36.67
    assert by_name["gpu-h100"]["job_count"] == 2
    assert by_name["gpu-h100"]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert by_name["gpu-h200"]["mean_util"] == pytest.approx(92.5)
    assert by_name["h200_3g.71gb"]["job_count"] == 1
    assert by_name["h200_3g.71gb"]["mean_util"] == pytest.approx(85.0)
    for p in data["partitions"]:
        assert 0 <= p["mean_util"] <= 100
    assert "gpu-h100" in data["trend"] and "gpu-h200" in data["trend"]
    assert "h200_3g.71gb" in data["trend"]


def test_partitions_gpu_capacity(client):
    by_name = {p["name"]: p for p in
               client.get("/api/partitions",
                          params={"since_hours": 24}).json()["partitions"]}
    # gpu-h100 members are all scontrol nodes with that partition (idle gpu3
    # included); allocation is the exact per-partition live GPU count.
    assert by_name["gpu-h100"]["gpus_total"] == 16
    assert by_name["gpu-h100"]["gpus_alloc"] == 2
    # job 4 moved to the MIG profile group, so gpu51 (idle) is the only
    # other gpu-h200 member now.
    assert by_name["gpu-h200"]["gpus_total"] == 16
    assert by_name["gpu-h200"]["gpus_alloc"] == 3
    # the MIG profile group carries only gpu49's capacity and job 4.
    assert by_name["h200_3g.71gb"]["gpus_total"] == 8
    assert by_name["h200_3g.71gb"]["gpus_alloc"] == 1


def test_partitions_running_only_injects_matcher(client, fake_prom):
    client.get("/api/partitions", params={"since_hours": 24})
    fake_prom.calls.clear()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    matcher = 'slurmjobid=~"^(?:1|2|4)$"'
    assert any(matcher in q and "max by (slurmjobid, instance, job, gpu_type)" in q
               for q in ranges), ranges
    assert any(matcher in q and "avg by (job, gpu_type)" in q
               for q in ranges), ranges
    # non-running job 3 (whole-GPU gpu-h200) is gone; running jobs 1, 2 are
    # gpu-h100 and running MIG job 4 is its profile group
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    assert set(by_name) == {"gpu-h100", "h200_3g.71gb"}
    # the trend has no slurmjobid label: the matched selector must have
    # excluded whole-GPU gpu-h200 upstream
    assert set(data["trend"]) == {"gpu-h100", "h200_3g.71gb"}


def test_partitions_running_only_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    data = r.json()
    assert data["partitions"] == [] and data["trend"] == {}

def test_partitions_vram_records(client):
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    by_id = {j["jobid"]: j for j in data["jobs"]}
    # per-GPU peaks averaged: job1 (12+18)/2, job2 30, job3 8, job4 22
    assert by_id["1"]["vram_gb"] == 15.0
    # ``partition`` is the group label (MIG jobs carry their profile group)
    assert by_id["1"]["partition"] == "gpu-h100"
    assert by_id["3"]["partition"] == "gpu-h200"
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
    monkeypatch.setattr(appmod, "_VRAM_RECORD_CAP", 2)
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
                      params={"since_hours": 24, "partition": "gpu-h100"}).json()
    assert data["total"] == 2
    assert {j["jobid"] for j in data["jobs"]} == {"1", "2"}
    # the gpu-h200 partition filter selects the whole-GPU group only; the
    # MIG slice is reached through its profile group label
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24, "partition": "gpu-h200"}).json()
    assert data["total"] == 1
    assert [j["jobid"] for j in data["jobs"]] == ["3"]
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24,
                              "partition": "h200_3g.71gb"}).json()
    assert data["total"] == 1
    assert [j["jobid"] for j in data["jobs"]] == ["4"]
    # unknown partition: no candidates, empty payload
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24, "partition": "b300"}).json()
    assert data["total"] == 0 and data["jobs"] == []


def test_slurm_error_maps_to_502(client, fake_prom, monkeypatch):
    def boom(ids, start_iso=None, **kw):
        raise appmod.SlurmError("sacct timed out")

    monkeypatch.setattr(appmod, "sacct_jobs", boom)
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    # the handler must produce the 502 itself; a reversed
    # JSONResponse(status, body) call turns this into a 500.
    assert r.status_code == 502
    assert r.json()["error"] == "slurm_unreachable"


def test_prometheus_error_maps_to_502(client, fake_prom, monkeypatch):
    def boom():
        raise appmod.PrometheusError("prometheus down")

    monkeypatch.setattr(appmod, "get_prom", boom)
    # /api/nodes degrades gracefully on Prometheus outages (by design);
    # the vram endpoint propagates, exercising the handler.
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "prometheus_unreachable"


def test_nodes_endpoint(client):
    r = client.get("/api/nodes")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 5  # gpu_only=True default (gpu1,2,3,49,51)
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["gpu1"]["current_util"] == 55.5
    assert by_name["gpu1"]["current_vram"] == 41.2
    assert by_name["gpu1"]["active_jobs"][0]["jobid"] == "1"
    assert by_name["gpu2"]["gpus_alloc"] == 3


def test_nodes_gpu_group(client):
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    # non-MIG nodes follow their partition; the MIG node is grouped by its
    # profile name, not by partition.
    assert by_name["gpu1"]["gpu_group"] == "gpu-h100"
    assert by_name["gpu2"]["gpu_group"] == "gpu-h200"
    assert by_name["gpu49"]["gpu_group"] == "h200_3g.71gb"
    assert by_name["gpu51"]["gpu_group"] == "gpu-h200"


def test_nodes_gpus_alloc(client):
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    assert by_name["gpu1"]["gpus_alloc"] == 2  # from count by (instance, job, gpu_type)
    assert by_name["gpu1"]["cpus_alloc"] == 16  # from scontrol CPUAlloc


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
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "gpu-h100"},
         "values": [[1, "40"], [2, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "job": "gpu-h100"},
         "values": [[1, "10"]]},
    ]
    out = appmod.aggregate_partition_stats(stats)
    assert out[0]["name"] == "gpu-h100"
    assert out[0]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert out[0]["max_util"] == 60.0
    assert out[0]["job_count"] == 2
    assert set(out[0]) == {"name", "mean_util", "max_util", "job_count"}


def test_jobs_efficiency_field(client):
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    by_id = {j["jobid"]: j for j in data["jobs"]}
    assert by_id["1"]["efficiency"] == 50.0  # mean of 40, 60
    assert by_id["2"]["efficiency"] == 10.0
    assert by_id["3"]["efficiency"] == 92.5
    assert by_id["4"]["efficiency"] == 85.0


def test_jobs_efficiency_extremes(client):
    data = client.get("/api/jobs", params={"since_hours": 24}).json()
    assert [j["jobid"] for j in data["efficiency_high"]] == ["3", "4", "1", "2"]
    assert [j["jobid"] for j in data["efficiency_low"]] == ["2", "1", "4", "3"]


def test_jobs_extremes_bounded_by_search(client):
    data = client.get("/api/jobs",
                      params={"since_hours": 24, "search": "train.sh"}).json()
    # search matches only job 1's sacct name; charts must show the searched rows
    assert [j["jobid"] for j in data["jobs"]] == ["1"]
    assert [j["jobid"] for j in data["efficiency_high"]] == ["1"]
    assert [j["jobid"] for j in data["efficiency_low"]] == ["1"]


def test_partitions_mean_occupancy(client):
    data = client.get("/api/partitions", params={"since_hours": 24}).json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # occupancy = concurrent series / capacity, rounded to 0.1:
    # gpu-h100 2 / 16 = 12.5%; gpu-h200 1 / 16 = 6.25% -> 6.2 (job 3's
    # whole-GPU group lost job 4's series); h200_3g.71gb 1 / 8 = 12.5%
    # (job 4 on the MIG node)
    assert by_name["gpu-h100"]["mean_occupancy"] == 12.5
    assert by_name["gpu-h200"]["mean_occupancy"] == 6.2
    assert by_name["h200_3g.71gb"]["mean_occupancy"] == 12.5


def test_partitions_mean_occupancy_running_only(client, fake_prom):
    data = client.get("/api/partitions",
                      params={"since_hours": 24, "running_only": "true"}).json()
    by_name = {p["name"]: p for p in data["partitions"]}
    # whole-GPU gpu-h200 (non-running job 3 only) is gone; the matched
    # running series are gpu-h100 jobs plus running MIG job 4
    assert set(by_name) == {"gpu-h100", "h200_3g.71gb"}
    assert by_name["gpu-h100"]["mean_occupancy"] == 12.5
    assert by_name["h200_3g.71gb"]["mean_occupancy"] == 12.5
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    assert any('slurmjobid=~"^(?:1|2|4)$"' in q and "count by (job, gpu_type)" in q
               for q in ranges), ranges
