"""Endpoint tests with a fake Prometheus client. Run: .venv/bin/python -m pytest tests/ -q"""

import os
import re
import sys
from datetime import datetime, timezone

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
        self.live_ids = {"1", "2"}   # count by (slurmjobid)
        self.job_start_ids = {"1", "2"}  # count by (slurmjobid){instance="gpu1"}

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
    # Partition summary keeps slurmjobid + instance so job identity and the
    # capacity join survive aggregation.
    _PART_SUMMARY = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu_type": "h100"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "gpu_type": "h100"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "gpu_type": "h200"},
         "values": [[1000, "90"], [1120, "95"]]},
    ]
    _PART_TREND = [
        {"metric": {"gpu_type": "h100"}, "values": [[1000, "25.0"], [1120, "35.0"]]},
        {"metric": {"gpu_type": "h200"}, "values": [[1000, "92.5"]]},
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
    ]

    def query_range(self, query, start, end, step):
        self.calls.append(("range", query))
        if "slurm_job_utilization_gpu" in query:
            if 'slurmjobid="' in query:  # job detail: per-device series
                return self._JOB_DETAIL_UTIL
            if 'instance="' in query:  # node detail
                return self._NODE_DETAIL_UTIL
            if "avg by (gpu_type)" in query:  # partition trend
                # a matched selector only yields types with matching jobs
                ids = self._matchers(query)
                if ids is None:
                    return self._PART_TREND
                allowed = {s["metric"]["gpu_type"]
                           for s in self._PART_SUMMARY
                           if s["metric"]["slurmjobid"] in ids}
                return [t for t in self._PART_TREND
                        if t["metric"]["gpu_type"] in allowed]
            if "max by (slurmjobid, instance, gpu_type)" in query:
                return self._filter(self._PART_SUMMARY, self._matchers(query))
            return self._filter(self._JOBS_UTIL, self._matchers(query))
        if "memory" in query:  # vram
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
        if "count by (instance)" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "5"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "3"]},
            ]
        if "max by (instance) (slurm_job_utilization_gpu)" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "55.5"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "10.0"]},
            ]
        if "memory_usage_gpu /" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "41.2"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "8.0"]},
            ]
        if "slurmjobid, job, user" in query:
            return [
                {"metric": {"instance": "gpu1", "slurmjobid": "1", "job": "gpu-h100",
                            "user": "alice"}, "value": [1, "77.0"]},
                {"metric": {"instance": "gpu2", "slurmjobid": "3", "job": "gpu-h200",
                            "user": "carol"}, "value": [1, "90.0"]},
            ]
        return []


@pytest.fixture()
def fake_prom(monkeypatch):
    fake = FakeProm()
    monkeypatch.setattr(appmod, "get_prom", lambda: fake)
    monkeypatch.setattr(appmod, "_cache", {})
    monkeypatch.setattr(appmod, "sacct_jobs",
                        lambda ids, start_iso, **kw: {j: SACCT[j] for j in ids
                                                      if j in SACCT})

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
    assert ids == {"1", "2"}  # job 3 has no live GPU series


def test_jobs_running_only_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/jobs",
                   params={"since_hours": 24, "running_only": "true"})
    data = r.json()
    assert data["count"] == 0 and data["jobs"] == []
    # no broad window range query may be issued when nothing is running
    assert not [q for t, q in fake_prom.calls if t == "range"]


def test_job_detail_200_with_epochs(client):
    r = client.get("/api/jobs/1", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["jobid"] == "1"
    assert data["series"]["utilization"], "no utilization series"
    assert data["series"]["utilization"][0]["values"]
    meta = data["metadata"]
    assert meta and meta["name"] == "train.sh"
    assert meta["start_epoch"] == pytest.approx(_epoch(JOB1_START))
    assert meta["end_epoch"] is None  # running job: no end


def test_job_detail_end_epoch(client):
    meta = client.get("/api/jobs/2", params={"since_hours": 24}).json()["metadata"]
    assert meta["end_epoch"] == pytest.approx(_epoch(JOB2_END))


def test_partitions_gpu_type_groups(client):
    r = client.get("/api/partitions", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    assert set(by_name) == {"h100", "h200"}
    # h100: samples 40, 60 (job1) and 10 (job2) -> time-weighted mean 36.67
    assert by_name["h100"]["job_count"] == 2
    assert by_name["h100"]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert by_name["h200"]["mean_util"] == pytest.approx(92.5)
    for p in data["partitions"]:
        assert 0 <= p["mean_util"] <= 100
    assert "h100" in data["trend"] and "h200" in data["trend"]


def test_partitions_gpu_capacity(client):
    by_name = {p["name"]: p for p in
               client.get("/api/partitions",
                          params={"since_hours": 24}).json()["partitions"]}
    # h100 resolves to one scontrol type: both h100 nodes count (idle gpu3
    # included); allocation comes from live Prometheus counts.
    assert by_name["h100"]["gpus_total"] == 16
    assert by_name["h100"]["gpus_alloc"] == 5
    assert by_name["h200"]["gpus_total"] == 8
    assert by_name["h200"]["gpus_alloc"] == 3


def test_partitions_running_only_injects_matcher(client, fake_prom):
    client.get("/api/partitions", params={"since_hours": 24})
    fake_prom.calls.clear()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    matcher = 'slurmjobid=~"^(?:1|2)$"'
    assert any(matcher in q and "max by (slurmjobid, instance, gpu_type)" in q
               for q in ranges), ranges
    assert any(matcher in q and "avg by (gpu_type)" in q
               for q in ranges), ranges
    # non-running job 3 (h200 only) is gone; running jobs are h100-only
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    assert set(by_name) == {"h100"}
    # the trend has no slurmjobid label: the matched selector must have
    # excluded h200 upstream
    assert set(data["trend"]) == {"h100"}


def test_partitions_running_only_empty_when_no_live_ids(client, fake_prom):
    fake_prom.live_ids = set()
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    data = r.json()
    assert data["partitions"] == [] and data["trend"] == {}


def test_nodes_endpoint(client):
    r = client.get("/api/nodes")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 3  # gpu_only=True default
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["gpu1"]["current_util"] == 55.5
    assert by_name["gpu1"]["current_vram"] == 41.2
    assert by_name["gpu1"]["active_jobs"][0]["jobid"] == "1"
    assert by_name["gpu2"]["gpus_alloc"] == 3


def test_nodes_gpus_alloc(client):
    by_name = {n["name"]: n for n in client.get("/api/nodes").json()["nodes"]}
    assert by_name["gpu1"]["gpus_alloc"] == 5  # from count by (instance)
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


def test_deep_link_routes_serve_spa(client):
    for path in ["/jobs", "/partitions", "/nodes", "/job/19807768",
                 "/node/dgx1"]:
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Triton GPU Efficiency Dashboard" in r.text


def test_aggregate_partition_stats_fixture():
    stats = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu_type": "h100"},
         "values": [[1, "40"], [2, "60"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "gpu_type": "h100"},
         "values": [[1, "10"]]},
    ]
    out = appmod.aggregate_partition_stats(stats)
    assert out[0]["name"] == "h100"
    assert out[0]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert out[0]["max_util"] == 60.0
    assert out[0]["job_count"] == 2
    assert set(out[0]) == {"name", "mean_util", "max_util", "job_count"}


def test_jobid_matcher_escapes():
    assert appmod._jobid_matcher({"9", "10"}) == 'slurmjobid=~"^(?:10|9)$"'
    assert appmod._jobid_matcher({"a.b", "c"}) == 'slurmjobid=~"^(?:a\\.b|c)$"'
