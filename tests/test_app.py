"""Endpoint tests with a fake Prometheus client. Run: .venv/bin/python -m pytest tests/ -q"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402


class FakeProm:
    """Canned responses shaped like the real Prometheus API."""

    api_base = "http://fake/api/v1"

    def __init__(self):
        self.calls = []

    def query_range(self, query, start, end, step):
        self.calls.append(("range", query))
        if "slurm_job_utilization_gpu" in query:
            if "slurmjobid=" in query:  # job detail: per-device series
                return [
                    {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
                     "values": [[1000, "40"], [1120, "60"]]},
                    {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1"},
                     "values": [[1000, "20"], [1120, "80"]]},
                ]
            if "instance=" in query:  # node detail
                return [
                    {"metric": {"slurmjobid": "1", "gpu": "0"},
                     "values": [[1000, "50"], [1120, "50"]]},
                ]
            label = "gpu_type" if "gpu_type" in query else "job"
            return [
                {"metric": {"slurmjobid": "1", "instance": "gpu1", label: "p1"},
                 "values": [[1000, "40"], [1120, "60"]]},
                {"metric": {"slurmjobid": "2", "instance": "gpu1", label: "p1"},
                 "values": [[1000, "10"]]},
                {"metric": {"slurmjobid": "3", "instance": "gpu2", label: "p2"},
                 "values": [[1000, "90"], [1120, "95"]]},
            ]
        if "memory" in query:  # vram
            if "slurmjobid=" in query:
                return [
                    {"metric": {"instance": "gpu1", "gpu": "0"},
                     "values": [[1000, "12.5"], [1120, "13.5"]]},
                ]
            if "instance=" in query:
                return [
                    {"metric": {"gpu": "0"}, "values": [[1000, "30"], [1120, "32"]]},
                ]
            return [
                {"metric": {"slurmjobid": "1", "instance": "gpu1", "job": "p1"},
                 "values": [[1000, "10"], [1120, "12"]]},
            ]
        return []

    def query_instant(self, query, time=None):
        self.calls.append(("instant", query))
        if "instance) (slurm_job_utilization_gpu)" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "55.5"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "10.0"]},
            ]
        if "memory_usage_gpu /" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "41.2"]},
            ]
        if "slurmjobid, job, user" in query:
            return [
                {"metric": {"instance": "gpu1", "slurmjobid": "9", "job": "p1",
                            "user": "alice"}, "value": [1, "77.0"]},
            ]
        return []


@pytest.fixture()
def client(monkeypatch):
    fake = FakeProm()
    monkeypatch.setattr(appmod, "get_prom", lambda: fake)
    monkeypatch.setattr(appmod, "_cache", {})
    monkeypatch.setattr(appmod, "sacct_jobs",
                        lambda ids, start_iso, **kw: {
                            "1": {"jobid": "1", "name": "train.sh", "user": "alice",
                                  "account": "acc", "partition": "p1",
                                  "state": "RUNNING", "start": "2026-08-28T00:00:00",
                                  "end": "", "elapsed_s": 3600, "gpus": 2,
                                  "gpu_type": "h100", "node_list": "gpu1",
                                  "ncpus": 8},
                        })
    monkeypatch.setattr(appmod, "show_nodes", lambda: [
        {"name": "gpu1", "state": "MIXED", "state_full": "MIXED",
         "partitions": "p1", "cpus": 64, "gpus": 8, "gpu_type": "h100",
         "cpus_alloc": 16, "free_mem": 1000, "real_mem": 5000},
        {"name": "csl1", "state": "IDLE", "state_full": "IDLE",
         "partitions": "batch", "cpus": 40, "gpus": 0, "gpu_type": "",
         "cpus_alloc": 0, "free_mem": 100, "real_mem": 200},
    ])
    monkeypatch.setattr(appmod, "show_partitions", lambda: [
        {"name": "p1", "state": "UP", "nodes": "gpu[1-2]"},
    ])
    return TestClient(appmod.app)


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


def test_job_detail_200(client):
    r = client.get("/api/jobs/1", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["jobid"] == "1"
    assert data["series"]["utilization"], "no utilization series"
    assert data["series"]["utilization"][0]["values"]
    assert data["metadata"] and data["metadata"]["name"] == "train.sh"


def test_partitions_endpoint(client):
    r = client.get("/api/partitions", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert len(data["partitions"]) >= 2
    by_name = {p["name"]: p for p in data["partitions"]}
    assert by_name["p1"]["job_count"] == 2
    for p in data["partitions"]:
        assert 0 <= p["mean_util"] <= 100
    # p1: samples 40, 60 (job1) and 10 (job2) -> time-weighted mean 36.67
    assert by_name["p1"]["mean_util"] == pytest.approx(36.67, abs=0.01)
    assert "trend" in data and "p1" in data["trend"]


def test_partitions_group_by_gputype(client):
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "group_by": "gpu_type"})
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["partitions"]}
    assert names, "no gpu types returned"


def test_partitions_rejects_bad_group_by(client):
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "group_by": "user"})
    assert r.status_code == 422


def test_nodes_endpoint(client):
    r = client.get("/api/nodes")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1  # gpu_only=True default
    node = data["nodes"][0]
    assert node["name"] == "gpu1"
    assert node["current_util"] == 55.5
    assert node["current_vram"] == 41.2
    assert node["active_jobs"][0]["jobid"] == "9"


def test_nodes_detail_200(client):
    r = client.get("/api/nodes/gpu1", params={"window_hours": 1})
    assert r.status_code == 200
    data = r.json()
    assert data["series"]["utilization"]
    assert data["series"]["vram"]


def test_match_scontrol_partition():
    sparts = [
        {"name": "gpu-b300-288g-ellis", "nodes": "gpu[64-67]", "state": "UP"},
        {"name": "gpu-b300-288g-short", "nodes": "gpu[60-63]", "state": "UP"},
        {"name": "gpu-h200-71g-ia", "nodes": "gpu[12-15]", "state": "UP"},
        {"name": "gpu-h200-71g-ia-ellis", "nodes": "gpu[8-11]", "state": "DOWN"},
        {"name": "batch-csl", "nodes": "csl[1-48]", "state": "UP"},
    ]
    m = appmod._match_scontrol_partition("b300", sparts)
    assert m["nodes"] == "gpu[64-67],gpu[60-63]"
    assert m["state"] == "UP"
    assert len(m["slurm_partitions"]) == 2
    h200 = appmod._match_scontrol_partition("h200", sparts)
    assert len(h200["slurm_partitions"]) == 2
    assert h200["state"] != "UP"
    assert appmod._match_scontrol_partition("v100_32g", sparts) is None


def test_aggregate_partition_stats_fixture():
    stats = [
        {"metric": {"slurmjobid": "1", "job": "p1"},
         "values": [[1, "40"], [2, "60"]]},
    ]
    out = appmod.aggregate_partition_stats(stats, "job")
    assert out[0]["name"] == "p1"
    assert out[0]["mean_util"] == pytest.approx(50.0)
    assert out[0]["max_util"] == 60.0
    assert out[0]["job_count"] == 1
