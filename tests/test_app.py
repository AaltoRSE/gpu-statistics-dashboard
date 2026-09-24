"""Endpoint tests with a fake Prometheus client.

Run: .venv/bin/python -m pytest tests/ -q
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import cache  # noqa: E402
import deps  # noqa: E402
import domain.common  # noqa: E402
import domain.jobs as domain_jobs  # noqa: E402
import domain.metadata as domain_metadata  # noqa: E402
import domain.partitions  # noqa: E402
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
    # V100 memory pools: scontrol reports plain typed GRES v100 for both;
    # only the partitions (and min-vram GRES) distinguish the pools.
    {"name": "dgx1a", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "gpu-v100-16g", "cpus": 96, "gpus": 4,
     "gpu_type": "v100_16gb", "gres": [("v100_16gb", 4)],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 900, "real_mem": 5000},
    {"name": "dgx1b", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "gpu-v100-32g", "cpus": 96, "gpus": 4,
     "gpu_type": "v100_32gb", "gres": [("v100_32gb", 4)],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 900, "real_mem": 5000},
    # CPU-only node: its partition must never appear as a GPU queue
    {"name": "csl1", "state": "IDLE", "state_full": "IDLE", "reason": "",
     "partitions": "batch", "cpus": 40, "gpus": 0, "gpu_type": "",
     "gres": [],
     "gpus_alloc": 0, "cpus_alloc": 0, "free_mem": 100, "real_mem": 200},
]


COMPLETED_HISTORY = [{**record, "state": "COMPLETED"}
                     for record in SACCT.values()]

# The Groups tab's NSS directory (deps.user_groups): each canned user's
# group list; users outside this map are unknown to the directory (the
# Unresolved row). dave holds no laitos-*/osasto-* group at all — the
# Unaffiliated row. /api/users never reads this; /api/groups does.
USER_GROUPS = {
    "alice": ["laitos-t40106", "osasto-t410"],
    "bob": ["laitos-t30010", "osasto-t300"],
    "carol": ["osasto-t313"],   # department only, no unit
    "dave": ["triton-users"],   # no org groups at all: unaffiliated
}


class FakeProm:
    """Canned responses shaped like the real Prometheus API.

    Window queries are answered with the per-GPU raw series the shared
    source layer fetches (plan §2): one 6-label ``max by`` series per
    allocated GPU, from which every tab view (job aggregates, partition
    summary, trend, occupancy) is derived in process. The canned series
    are chosen so those derived aggregates exactly equal the old
    hand-aggregated fixtures: job 1's second GPU reports 0 for the whole
    window (an allocated-but-idle GPU), so the per-job max stays [40, 60]
    and the group sums/counts match the old ``sum``/``count by`` fixtures.
    """

    api_base = "http://fake/api/v1"

    def __init__(self):
        self.calls = []
        self.nodes_calls = 0
        # Live per-GPU instant series: job 1 holds 2 GPUs on gpu1, job 2
        # 1 GPU on gpu1, job 4 1 MIG slice on gpu49. Job 3 is not running.
        # The series count per node IS the allocation count.
        self.live_ids = {"1", "2", "4"}
        self.clear_cache_calls = 0
        # Extra window series appended to the jobs list by tests that
        # need more candidates than the canned four.
        self.extra_jobs = []

    # -- canned range data -------------------------------------------------
    _JOB_DETAIL_UTIL = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0"},
         "values": [[1000, "40"], [1120, "60"]]},
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1"},
         "values": [[1000, "20"], [1120, "80"]]},
    ]

    _NODE_DETAIL_UTIL = [
        {"metric": {"slurmjobid": "1", "gpu": "0"},
         "values": [[1000, "50"], [1120, "50"]]},
    ]
    # The per-GPU raw window series (plan §2): every job/GPU instance of
    # the exporter's utilization metric. Job 4 runs on mixed node gpu49;
    # its MIG-shaped label must split it into the profile group.
    _GPU_UTIL = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0",
                    "job": "gpu-h200", "user": "alice",
                    "gpu_type": "NVIDIA H200"},
         "values": [[1000, "40"], [1120, "60"]]},
        # job 1's second GPU: allocated but never utilized in this window
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1",
                    "job": "gpu-h200", "user": "alice",
                    "gpu_type": "NVIDIA H200"},
         "values": [[1000, "0"], [1120, "0"]]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "gpu": "0",
                    "job": "gpu-h200", "user": "bob", "gpu_type": "h200"},
         "values": [[1000, "10"]]},
        {"metric": {"slurmjobid": "3", "instance": "gpu2", "gpu": "0",
                    "job": "gpu-h100", "user": "carol", "gpu_type": "h100"},
         "values": [[1000, "90"], [1120, "95"]]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "gpu": "0",
                    "job": "gpu-h200-mig", "user": "dave",
                    "gpu_type": "h200_3g.71gb"},
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
    # Live instant series (the exporter publishes one per allocated GPU):
    # job 1's two GPUs on gpu1 at 77/70 (node max 77), job 2's GPU at 10,
    # job 4's slice on gpu49 at 60. Job 3 is absent (not running).
    _GPU_INSTANT = [
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "0",
                    "job": "gpu-h200", "user": "alice",
                    "gpu_type": "NVIDIA H200"},
         "value": [1, "77.0"]},
        {"metric": {"slurmjobid": "1", "instance": "gpu1", "gpu": "1",
                    "job": "gpu-h200", "user": "alice",
                    "gpu_type": "NVIDIA H200"},
         "value": [1, "70.0"]},
        {"metric": {"slurmjobid": "2", "instance": "gpu1", "gpu": "0",
                    "job": "gpu-h200", "user": "bob", "gpu_type": "h200"},
         "value": [1, "10.0"]},
        {"metric": {"slurmjobid": "4", "instance": "gpu49", "gpu": "0",
                    "job": "gpu-h200-mig", "user": "dave",
                    "gpu_type": "h200_3g.71gb"},
         "value": [1, "60.0"]},
    ]

    def query_range(self, query, start, end, step):
        self.calls.append(("range", query))
        if "slurm_job_utilization_gpu" in query:
            if 'slurmjobid="' in query:  # job detail: per-device series
                return self._JOB_DETAIL_UTIL
            if 'instance="' in query:  # node detail
                return self._NODE_DETAIL_UTIL
            # The shared per-GPU raw window series (the only utilization
            # range query the app issues now); extra_jobs ride along.
            return self._GPU_UTIL + self.extra_jobs
        if "memory" in query:  # vram
            if "max by (slurmjobid, instance, gpu)" in query:  # vram_gb
                return self._VRAM_GB
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
        if "slurmjobid" in query and "max by" in query:
            # The per-GPU live snapshot: the shared source of live IDs,
            # per-node utilization, active jobs, and allocation counts.
            # Tests trim live_ids to simulate an idle cluster.
            return [s for s in self._GPU_INSTANT
                    if s["metric"]["slurmjobid"] in self.live_ids]
        if "memory_usage_gpu /" in query:
            return [
                {"metric": {"instance": "gpu1"}, "value": [1, "41.2"]},
                {"metric": {"instance": "gpu2"}, "value": [1, "8.0"]},
                {"metric": {"instance": "gpu49"}, "value": [1, "40.0"]},
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
    # The shared window-wide sacct dump (plan §3): every chunk of the
    # window returns the completed history; the row dicts carry the raw
    # ID spelling the dump's index keys on. Each chunk is dated inside
    # the fetched window, so a job spanning midnight dedupes to its
    # newest chunk's row.
    monkeypatch.setattr(
        deps, "sacct_allocations",
        lambda start_iso, end_iso, partitions: [
            dict(record, jobid_raw=record["jobid"])
            for record in COMPLETED_HISTORY])
    # Per-ID sacct rows (the row-cache fallback for jobs the dump cannot
    # enrich): the same records, resolved for exactly the requested IDs.
    monkeypatch.setattr(
        deps, "sacct_jobs_resilient",
        lambda ids, start_iso=None, **kw: (
            {j: SACCT[j] for j in ids if j in SACCT}, 0))
    # No active controller jobs by default; tests opt in to a snapshot.
    monkeypatch.setattr(deps, "show_jobs", lambda: {})
    # The Groups tab's NSS boundary (deps.user_groups): the canned
    # directory above; an unknown user reads as None (unresolved).
    monkeypatch.setattr(deps, "user_groups",
                        lambda username: USER_GROUPS.get(username))
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
    # api_users computes its rows from the shared job view; the view
    # (already imported into the route module) is patched here so the
    # synthetic equal-duration jobs drive the weighted mean.
    monkeypatch.setattr(api_users, "job_views",
                        lambda win, raw, types, vram=(): jobs)
    data = client.get("/api/users", params={"since_hours": 24}).json()
    assert data["users"][0]["mean_util"] == 50.0


def test_users_window_validation(client):
    assert client.get("/api/users", params={"since_hours": 0}).status_code == 422


def test_jobs_user_filter_is_in_process(client, fake_prom):
    # The user filter runs in process over the SHARED unfiltered window
    # fetch (plan §2) — no per-user Prometheus matcher, so a single-user
    # request reuses the same fetch every tab reads.
    r = client.get("/api/jobs", params={"since_hours": 24, "user": "alice"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["jobs"][0]["jobid"] == "1"
    ranges = [q for t, q in fake_prom.calls if t == "range"]
    assert ranges, "the window fetch must still happen"
    assert all('user="' not in q for q in ranges), ranges


def test_jobs_user_filter_accepts_case_drift(client):
    # PromQL's user matcher was case-sensitive; the in-process filter
    # casefolds, so typed capitalization drift still matches the label.
    r = client.get("/api/jobs", params={"since_hours": 24, "user": "Alice"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["jobs"][0]["user"] == "alice"


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
    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, **kw: ({}, 0))
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
    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, **kw: ({}, 0))
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
    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, **kw: ({}, 0))
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
    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, **kw: ({
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
    }, 0))
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
    monkeypatch.setattr(deps, "sacct_jobs_resilient",
                        lambda ids, start_iso=None, **kw: ({
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
    }, 0))
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
    assert set(by_name) == {"gh200", "h200", "h100", "h200_3g.71gb",
                            "v100_16gb", "v100_32gb"}
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
    assert {"gh200", "h100", "h200", "h200_3g.71gb",
            "v100_16gb", "v100_32gb"} <= set(data["trend"])
    assert data["trend"]["gh200"] == []


def test_partitions_gpu_capacity(client):
    by_name = {p["name"]: p for p in
               client.get("/api/partitions",
                          params={"since_hours": 24}).json()["partitions"]}
    # Capacity is summed per exact GRES type across ALL scontrol nodes
    # (idle gpu3 included, independent of partition membership):
    # h200 = gpu1's 8 + gpu3's 8 + gpu49's 4 whole = 20.
    assert by_name["h200"]["gpus_total"] == 20
    # allocation is the exact per-group live count from the live
    # snapshot's per-GPU series: job 1 holds 2 GPUs + job 2 holds 1 on
    # gpu1 -> 3 h200 series. (The old canned count-by fixture said 2;
    # the per-GPU snapshot is the exporter's actual accounting.)
    assert by_name["h200"]["gpus_alloc"] == 3
    assert by_name["h100"]["gpus_total"] == 8
    # job 3 is not running, so the h100 group has no live allocation.
    assert by_name["h100"]["gpus_alloc"] == 0
    # the MIG profile group carries only gpu49's slices.
    assert by_name["h200_3g.71gb"]["gpus_total"] == 8
    assert by_name["h200_3g.71gb"]["gpus_alloc"] == 1




def test_partitions_running_only_filters_series_in_process(client, fake_prom):
    r = client.get("/api/partitions",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    # The live-ID filter is in process over the shared raw fetch: the only
    # utilization range query carries no matcher, and no sum/count
    # aggregation queries exist any more (plan §2).
    util_ranges = [q for t, q in fake_prom.calls if t == "range"
                   and "slurm_job_utilization_gpu" in q]
    assert util_ranges, util_ranges
    assert all(
        "max by (slurmjobid, instance, gpu, job, user, gpu_type)" in q
        and "=~" not in q for q in util_ranges), util_ranges
    # non-running job 3 (h100) is gone; running jobs 1+2 stay h200 and
    # running MIG job 4 is its profile group
    data = r.json()
    by_name = {p["name"]: p for p in data["partitions"]}
    assert set(by_name) == {"h200", "h200_3g.71gb"}
    # the trend is derived from the same filtered series
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
    # GPU-hour weighted: (3600s+3600s) waits / (2×3600 + 1×7200 GPU-s)
    # = 7200/14400 = 0.5 wait-hours per GPU-hour
    assert q["h200"]["wait_per_gpu_hour_weighted"] == 0.5
    # job 11's untyped request is also eligible for the MIG profile
    # (flexible there); job 4 (the in-window MIG start) waited 7200s:
    # submitted 22:00 the day before its 00:00 start
    assert q["h200_3g.71gb"]["eligible_gpus"] == 1
    assert q["h200_3g.71gb"]["wait_p50_s"] == 7200
    assert q["h200_3g.71gb"]["wait_p90_s"] == 7200
    assert q["h200_3g.71gb"]["wait_samples"] == 1
    # job 4: 7200s wait, 1 GPU × 3600s elapsed -> 7200/3600 = 2.0
    assert q["h200_3g.71gb"]["wait_per_gpu_hour_weighted"] == 2.0
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


def test_pending_v100_request_maps_to_eligible_memory_pools(
        client, fake_prom, monkeypatch):
    # scontrol reports plain v100 for both memory pools; a typed v100
    # request whose partitions could land in either pool must count toward
    # both real pool rows instead of a capacity-less synthetic v100 row.
    monkeypatch.setattr(deps, "queue_pending", lambda: [
        {"jobid": "30", "user": "eve",
         "partition": "gpu-v100-16g,gpu-v100-32g", "state": "PENDING",
         "submit": "2026-08-30T12:26:40", "start": "",
         "reason": "(Resources)", "nodes": 1, "gpus": 2,
         "gpu_type": "v100"},
    ])
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    q = data["queue"]
    assert "v100" not in q
    assert q["v100_16gb"]["eligible_jobs"] == 1
    assert q["v100_32gb"]["eligible_jobs"] == 1
    assert q["v100_16gb"]["eligible_gpus"] == 2
    assert q["v100_32gb"]["eligible_gpus"] == 2


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
                   "wait_per_gpu_hour_weighted": None}
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
                           "wait_per_gpu_hour_weighted": 1.0}
    assert coverage["excluded"]["nonpositive_elapsed"] == 1
    assert coverage["excluded"]["timestamps"] == 2
    assert coverage["excluded"]["negative_wait"] == 1


def test_started_wait_summary_ratio_weighted_and_exclusions(
        client, fake_prom, monkeypatch):
    # The ratio is GPU-hour weighted — wait-sum over GPU-hour-sum
    # (elapsed_s × gpus summed over jobs) — NOT the median of per-job
    # ratios, which short jobs would dominate. Rounded to two decimals.
    import domain.partitions as dp
    # waits: 1800 + 3600 + 0 = 5400; GPU-s: 1800*1 + 3600*4 + 900*2 = 18000
    # -> 5400/18000 = 0.3; the per-job median would be 0.25 instead.
    # A zero wait with positive job size is a valid sample.
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
    assert out["h100"]["wait_per_gpu_hour_weighted"] == 0.3
    assert out["h100"]["wait_samples"] == 3
    # Typed allocation is mandatory: an untyped/zero-GPU record is excluded.
    records = [{"state": "COMPLETED", "submit": "2026-08-30T09:00:00",
                "start": "2026-08-30T10:00:00", "elapsed_s": 3600,
                "gpus": 0, "gpu_type": "", "node_list": "gpu2"}]
    out, coverage = dp.completed_wait_summary(
        records, {"gpu2": ["h100"]}, NOW - 72 * 3600, NOW)
    assert out[dp.WAIT_TOTAL_KEY] == dp.wait_empty()
    assert coverage["excluded"]["missing_typed_gpu_allocation"] == 1
    # All-zero waits with positive GPU-hours stay a valid 0.0, not None.
    records = [{"state": "COMPLETED", "submit": "2026-08-30T12:00:00",
                "start": "2026-08-30T12:00:00", "elapsed_s": 900,
                "gpus": 2, "gpu_type": "h100", "node_list": "gpu2"}]
    out, coverage = dp.completed_wait_summary(
        records, {"gpu2": ["h100"]}, NOW - 72 * 3600, NOW)
    assert out["h100"]["wait_per_gpu_hour_weighted"] == 0.0
    assert coverage["valid_samples"] == {"h100": 1}


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
    def _boom(start_iso, end_iso, partitions):
        raise slurm.SlurmError("sacct is not available")

    monkeypatch.setattr(deps, "sacct_allocations", _boom)
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    assert data["wait_history_available"] is False
    assert data["queue_available"] is True
    assert data["totals"]["unique_pending_jobs"] == 0
    # wait statistics read as unknown — all null — not as zero samples

    row = data["queue"]["h200"]
    assert row["wait_p50_s"] is None
    assert row["wait_p90_s"] is None
    assert row["wait_avg_s"] is None
    assert row["wait_samples"] is None
    assert row["wait_per_gpu_hour_weighted"] is None

def test_partitions_partial_wait_history_keeps_successful_metrics(
        client, fake_prom, monkeypatch):
    # One of the window's day chunks failing (after its retry) keeps the
    # other chunks' records: the wait history stays available and the
    # coverage discloses the failure.
    def flaky(start_iso, end_iso, partitions):
        if start_iso.startswith("2026-08-29"):
            raise slurm.SlurmError("chunk failed")
        return COMPLETED_HISTORY

    monkeypatch.setattr(deps, "sacct_allocations", flaky)
    data = client.get("/api/partitions/queue", params={"since_hours": 72}).json()
    assert data["wait_history_available"] is True
    assert data["wait_history_coverage"]["complete"] is False
    assert data["wait_history_coverage"]["failed_batches"] == 1
    assert data["queue"]["h200"]["wait_samples"] == 2


def test_partitions_queue_progress_endpoint_serves_batch_state(
        client, fake_prom):
    # The polling contract: /api/partitions/queue/progress must resolve
    # the ONE collapsed key the window's shared sacct dump publishes
    # under (plan §3). Seeding that exact key and GETting the route
    # proves the lookup; an epoch-derived or flag-mismatched key would
    # return None here.
    key = cache.vram_progress_key(24)
    domain.partitions.progress_store[key] = {
        "done": 4, "total": 7, "failed_batches": 1,
    }
    try:
        r = client.get("/api/partitions/queue/progress",
                       params={"since_hours": 24})
        assert r.status_code == 200
        assert r.json() == {"done": 4, "total": 7, "failed_batches": 1}
        # A fetch that never published (cache hit, finished, or failed)
        # reads as null progress rather than a fabricated batch.
        domain.partitions.progress_store.clear()
        assert client.get(
            "/api/partitions/queue/progress", params={"since_hours": 24},
        ).json() is None
    finally:
        domain.partitions.progress_store.pop(key, None)


def test_partitions_vram_progress_endpoint_serves_batch_state(
        client, fake_prom):
    # Same polling contract, and the SAME key: the VRAM enrichment reads
    # the same window-wide dump as the queue's wait history, so both
    # progress polls resolve one identity. The old per-partition/flag
    # key split is gone — the dump's batch work varies only with the
    # window.
    key = cache.vram_progress_key(24)
    domain.partitions.progress_store[key] = {
        "done": 3, "total": 5, "failed_batches": 1,
    }
    try:
        # Same window, any partition/running flag: the same in-flight
        # dump, so the same state.
        for params in ({"since_hours": 24, "partition": "h200"},
                       {"since_hours": 24, "partition": "h100"},
                       {"since_hours": 24, "running_only": "true"}):
            r = client.get("/api/partitions/vram/progress", params=params)
            assert r.status_code == 200
            assert r.json() == {"done": 3, "total": 5, "failed_batches": 1}
        # A different window is a different dump fetch.
        assert client.get("/api/partitions/vram/progress",
                          params={"since_hours": 72,
                                  "partition": "h200"}).json() is None
        # Nothing in flight reads as null, never a fabricated batch.
        domain.partitions.progress_store.clear()
        assert client.get("/api/partitions/vram/progress",
                          params={"since_hours": 24,
                                  "partition": "h200"}).json() is None
    finally:
        domain.partitions.progress_store.pop(key, None)


def test_partitions_vram_progress_clears_after_failed_fetch(
        client, fake_prom, monkeypatch):
    # A failure inside the dump fetch must leave no stale in-flight
    # entry behind: later polls must see null, not a hung batch state.
    def boom(*args, progress=None, **kwargs):
        raise slurm.SlurmError("sacct down")

    monkeypatch.setattr(deps, "sacct_allocations", boom)
    monkeypatch.setattr(deps, "sacct_jobs_resilient", boom)

    r = client.get("/api/partitions/vram",
                   params={"since_hours": 24})
    assert r.status_code == 502
    assert domain.partitions.progress_store == {}
    assert client.get("/api/partitions/vram/progress",
                      params={"since_hours": 24}).json() is None


def test_partitions_vram_follower_never_rewinds_shared_progress(
        client, fake_prom, monkeypatch):
    # A same-window follower joins the dump fetch's Future (plan §3), so
    # it must not touch the shared progress key: a follower-side seed
    # would rewind the leader's live batch count to 0, and a follower-
    # side clear would erase it mid-run. Only the cache-miss leader that
    # actually runs the chunked fetch publishes.
    progress_key = cache.vram_progress_key(24)
    store = domain.partitions.progress_store
    leader_started = threading.Event()
    release_leader = threading.Event()
    results = {}

    def slow_chunks(start_iso, end_iso, partitions):
        leader_started.set()
        # Hold the chunk open so the follower's whole request lifecycle
        # (route entry through finally) races with the leader's fetch.
        release_leader.wait(timeout=10)
        return COMPLETED_HISTORY

    def run(name, params):
        results[name] = client.get("/api/partitions/vram", params=params)

    monkeypatch.setattr(deps, "sacct_allocations", slow_chunks)
    params = {"since_hours": 24}
    leader = threading.Thread(target=run, args=("leader", params))
    follower = threading.Thread(target=run, args=("follower", params))
    leader.start()
    try:
        assert leader_started.wait(timeout=10)
        follower.start()
        # A correct follower joins the leader's Future and stays blocked
        # until the leader is released: still alive, no result yet.
        follower.join(timeout=2)
        assert follower.is_alive(), \
            "follower must block on the leader's Future, not run its own fetch"
        assert "follower" not in results
        # While the follower sat joined on the request, the leader's
        # live state was neither rewound to 0 nor erased (a 24 h window
        # chunks into 2 edge fetches).
        assert store[progress_key] == {"done": 0, "total": 2,
                                       "failed_batches": 0}
        release_leader.set()
        leader.join(timeout=10)
        follower.join(timeout=10)
        assert results["leader"].status_code == 200
        assert results["follower"].status_code == 200
    finally:
        release_leader.set()
        leader.join(timeout=10)
    # After completion the leader's finally cleared the single entry.
    assert store == {}


def test_sacct_window_publishes_and_clears_progress(client, fake_prom,
                                                    monkeypatch):
    # The dump fetch is the only batched part of a window request: the
    # cache-miss leader publishes {"done", "total", "failed_batches"}
    # before the first chunk and once per finished chunk, and the
    # finally clears the key — a poll after completion reads as null,
    # never as the last batch.
    import sources

    states = []

    def flaky(start_iso, end_iso, partitions):
        if start_iso.startswith("2026-08-29"):
            raise slurm.SlurmError("chunk failed")
        return COMPLETED_HISTORY

    monkeypatch.setattr(deps, "sacct_allocations", flaky)
    records, coverage, start, end = sources.sacct_window(
        24, progress=states.append,
        progress_key=cache.vram_progress_key(24))
    # Both window edges ran; the Aug 29 chunk failed after its retry.
    assert coverage["failed_batches"] == 1
    assert coverage["successful_batches"] == 1
    assert states[0] == {"done": 0, "total": 2, "failed_batches": 0}
    assert states[-1] == {"done": 2, "total": 2, "failed_batches": 1}
    # A failed fetch clears too — a stale in-flight entry would
    # otherwise read as live progress on every later poll.
    assert cache.progress_store == {}
    assert client.get("/api/partitions/vram/progress",
                      params={"since_hours": 24}).json() is None


def test_partitions_queue_progress_clears_after_failed_fetch(
        client, fake_prom, monkeypatch):
    # A SlurmError during the dump's chunk fetches must leave no stale
    # in-flight entry behind: later polls must see null, not a hung
    # batch state.
    def _boom(start_iso, end_iso, partitions):
        raise slurm.SlurmError("sacct unavailable")

    monkeypatch.setattr(deps, "sacct_allocations", _boom)
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    assert data["wait_history_available"] is False
    assert domain.partitions.progress_store == {}
    assert client.get("/api/partitions/queue/progress",
                      params={"since_hours": 24}).json() is None


def test_partitions_queue_cache_and_progress_share_window_identity(
        client, fake_prom, monkeypatch):
    # The dump cache keys by since_hours (not the captured epoch window,
    # which changes every second and would never hit the TTL cache), and
    # progress uses the same collapsed identity: two same-window
    # requests with opposite running_only flags must resolve to one dump
    # fetch, not two.
    calls = []

    def fake_chunks(start_iso, end_iso, partitions):
        calls.append(start_iso)
        return COMPLETED_HISTORY

    monkeypatch.setattr(deps, "sacct_allocations", fake_chunks)
    first = client.get("/api/partitions/queue",
                       params={"since_hours": 24}).json()
    second = client.get("/api/partitions/queue",
                        params={"since_hours": 24,
                                "running_only": True}).json()
    # Both requests join the leader's cached fetch: one chunk set (the
    # 24 h window's two edge fetches), not a second fetch.
    assert len(calls) == 2
    assert first["wait_history_coverage"]["records_examined"] == \
        second["wait_history_coverage"]["records_examined"]
    # The progress identity: one collapsed key, window-scoped only —
    # running_only and partition are response-shape parameters that
    # change no fetch (plan §3).
    assert cache.vram_progress_key(24) == ("vram_progress", 24)


def test_partitions_queue_progress_shares_fetch_across_running_flag(
        client, fake_prom, monkeypatch):
    # The dump joins same-window requests regardless of running_only, so
    # both flags' polls must read the ONE shared fetch's state: the
    # collapsed progress key omits the flag. If it leaked into the key,
    # the opposite-flag poll would miss the leader's entry entirely.
    key = cache.vram_progress_key(24)
    domain.partitions.progress_store[key] = {
        "done": 2, "total": 7, "failed_batches": 0}
    try:
        for flag in (False, True):
            r = client.get("/api/partitions/queue/progress",
                           params={"since_hours": 24, "running_only": flag})
            assert r.status_code == 200
            assert r.json() == {"done": 2, "total": 7, "failed_batches": 0}
    finally:
        domain.partitions.progress_store.pop(key, None)


def test_partitions_queue_empty_when_nothing_pending(client, fake_prom):
    # default fixture stub: reachable squeue, zero pending jobs
    data = client.get("/api/partitions/queue",
                      params={"since_hours": 24}).json()
    assert data["queue_available"] is True
    assert data["totals"]["unique_pending_jobs"] == 0
    assert data["totals"]["unique_gpus_requested"] == 0
    assert data["queue"]["h200"]["wait_samples"] == 0
    assert data["queue"]["h200"]["wait_p50_s"] is None


def test_partitions_queue_cache_hit_filters_on_fetched_bounds(
        client, fake_prom, monkeypatch):
    # The 300s dump TTL outlives any single request's epoch window:
    # a later cache hit must filter records against the bounds the dump
    # was actually fetched for, not bounds recomputed from an advanced
    # clock (which would silently drop every recent record).
    import cache as cache_module

    calls = []

    def fake_chunks(start_iso, end_iso, partitions):
        calls.append((start_iso, end_iso))
        return COMPLETED_HISTORY

    monkeypatch.setattr(deps, "sacct_allocations", fake_chunks)
    # A 168-hour window contains the newest fixture records: with the
    # cached bounds dropped, the recomputed (advanced-clock) window would
    # start a day later and drop them, so the pre-fix route fails here.
    first = client.get("/api/partitions/queue",
                       params={"since_hours": 168}).json()
    first_samples = first["queue"]["h200"]["wait_samples"]
    assert first_samples > 0
    first_examined = first["wait_history_coverage"]["records_examined"]

    # Advance the live clock past the pinned-window TTL by advancing the
    # cache's monotonic clock 120s (< the 300s dump TTL): the second
    # request re-pins its window at the advanced time while the dump
    # entry stays a hit serving its fetched bounds.
    now_marker = _epoch("2026-09-06T00:00:00")
    monkeypatch.setattr(deps, "now", lambda: now_marker)
    base_mono = cache_module.time.monotonic()
    monkeypatch.setattr(cache_module.time, "monotonic",
                        lambda: base_mono + 120)
    second = client.get("/api/partitions/queue",
                        params={"since_hours": 168}).json()
    # The second request is served entirely from the dump cache: its
    # records and bounds are the first fetch's, so only the first
    # request's 8 day chunks (168 h) ever ran.
    assert len(calls) == 8
    assert second["queue"]["h200"]["wait_samples"] == first_samples
    assert second["wait_history_coverage"]["records_examined"] == \
        first_examined


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


def test_partitions_vram_returns_every_candidate(client, fake_prom,
                                                 monkeypatch):
    # No cap: the VRAM chart must see every VRAM-bearing job in the
    # window. An empty dump sends all four candidates to the per-ID row
    # cache in ONE batched call, and all of them reach the payload with
    # total == len(jobs).
    seen_ids = []
    monkeypatch.setattr(
        deps, "sacct_allocations",
        lambda start_iso, end_iso, partitions: [])
    monkeypatch.setattr(
        deps, "sacct_jobs_resilient",
        lambda ids, start_iso=None, **kw: (
            seen_ids.extend(ids) or ({j: SACCT[j] for j in ids}, 0)))
    data = client.get("/api/partitions/vram", params={"since_hours": 24}).json()
    assert sorted(seen_ids) == ["1", "2", "3", "4"]
    assert len(data["jobs"]) == 4
    assert data["total"] == 4 == len(data["jobs"])


def test_partitions_vram_running_only_filters_live(client, fake_prom):
    r = client.get("/api/partitions/vram",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    ids = {j["jobid"] for j in r.json()["jobs"]}
    assert ids == {"1", "2", "4"}  # job 3 has no live GPU series
    # The VRAM series are the shared unscoped fetches: the live-ID filter
    # is applied in process to the job view, never as a matcher.
    assert all("=~" not in q
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


def test_partitions_vram_discloses_partial_enrichment(
        client, fake_prom, monkeypatch):
    # A failed dump (every chunk failing after its retry) must not 502
    # the whole VRAM endpoint: the failed chunks surface as the dump's
    # failed-batch count, the per-ID row cache fills what it can, and the
    # unresolved records keep null gpu_hours.
    def failing_chunks(start_iso, end_iso, partitions):
        raise slurm.SlurmError("chunk failed")

    def partial_rows(ids, start_iso=None, **kw):
        return ({j: SACCT[j] for j in ids if j == "1"}, 1)

    monkeypatch.setattr(deps, "sacct_allocations", failing_chunks)
    monkeypatch.setattr(deps, "sacct_jobs_resilient", partial_rows)
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24}).json()
    assert 0 < data["enriched_frac"] < 1
    # both window chunks failed and are counted, not swallowed
    assert data["failed_batches"] == 2
    by_job = {j["jobid"]: j for j in data["jobs"]}
    assert by_job["1"]["gpu_hours"] is not None
    assert by_job["2"]["gpu_hours"] is None


def test_slurm_error_maps_to_502(client, fake_prom, monkeypatch):
    def boom():
        raise appmod.SlurmError("scontrol is not available")

    # The VRAM pipeline's scontrol snapshot (node types) raises through
    # _pinned; the dump's chunk failures are counted, not raised, so the
    # 502 mapping is exercised on the scontrol call.
    monkeypatch.setattr(deps, "show_nodes", boom)
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "slurm_unreachable"


def test_partitions_vram_counts_zero_hour_rows_as_enriched(
        client, fake_prom, monkeypatch):
    # A sacct row with a valid allocation and elapsed 00:00:00 (Slurm's
    # just-started report) is resolved accounting, not a gap: it must
    # count as coverage and emit 0.0 GPU-hours, not null. The dump here
    # carries nothing for the window, so the rows arrive through the
    # per-ID fallback.
    def zero_row(ids, start_iso=None, **kw):
        meta = {}
        if "1" in ids:
            row = dict(SACCT["1"])
            row["elapsed_s"] = 0
            meta["1"] = row
        return meta, 0

    monkeypatch.setattr(
        deps, "sacct_allocations",
        lambda start_iso, end_iso, partitions: [])
    monkeypatch.setattr(deps, "sacct_jobs_resilient", zero_row)
    data = client.get("/api/partitions/vram",
                      params={"since_hours": 24}).json()
    by_job = {j["jobid"]: j for j in data["jobs"]}
    assert by_job["1"]["gpu_hours"] == 0.0
    # Only the records the stub resolved count; others stay null gaps.
    assert 0 < data["enriched_frac"] < 1
    assert data["failed_batches"] == 0


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
    assert data["count"] == 7  # gpu_only: gpu1, gpu2, gpu3, gpu49, gpu50,
    #                              dgx1a, dgx1b
    by_name = {n["name"]: n for n in data["nodes"]}
    # current_util is the per-node max over the live per-GPU series:
    # gpu1's job 1 reports 77.0 on its first GPU. gpu2 has no live
    # series (job 3 is not running), so its current_util is null — the
    # old canned "max by (instance)" fixture said 55.5/10.0.
    assert by_name["gpu1"]["current_util"] == 77.0
    assert by_name["gpu1"]["current_vram"] == 41.2
    assert by_name["gpu1"]["active_jobs"][0]["jobid"] == "1"
    assert by_name["gpu2"]["current_util"] is None
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
    # snapshot has nothing for this instance, yet gpus_alloc must still
    # read scontrol's true allocation, not silently drop to 0.
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
    # No live GPU series anywhere: the snapshot's jobs_by_node is empty
    # for this node and the six-hour fallback window applies.
    fake_prom.live_ids = set()
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
    # refresh also purges the Prometheus node cache: the second fresh
    # read re-queries the live snapshot's two instant metrics.
    instants = [q for t, q in fake_prom.calls if t == "instant"]
    assert len(instants) == 4  # 2 reads x 2 snapshot queries
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
    # Non-running h100 and idle gh200 are absent. The filtered running
    # whole-H200 series have counts 3 and 2, so occupancy is 12.5%.
    assert set(by_name) == {"h200", "h200_3g.71gb"}
    assert by_name["h200"]["mean_occupancy"] == 12.5
    assert by_name["h200_3g.71gb"]["mean_occupancy"] == 12.5


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
    assert out["h200"]["wait_per_gpu_hour_weighted"] == 1.0
    assert out["h200_3g.71gb"]["wait_samples"] == 1
    assert out["h200_3g.71gb"]["wait_per_gpu_hour_weighted"] == 4.0
    assert coverage["valid_samples"] == {"h200": 2, "h200_3g.71gb": 1}
    assert coverage["excluded"] == {"state": 2}


def test_step_for_range_long_windows():
    # Long-window policy: through 31 days the Prometheus step coarsens to
    # 1,800 s; beyond that the legacy fallback holds. 336 h (14 days) and
    # 720 h (30 days) both land in the 1,800 s tier.
    assert domain.common.step_for_range(7 * 86400) == 600
    assert domain.common.step_for_range(336 * 3600) == 1800
    assert domain.common.step_for_range(720 * 3600) == 1800
    assert domain.common.step_for_range(32 * 86400) == 900


@pytest.mark.parametrize("route", [
    "/api/jobs",
    "/api/jobs/1",
    "/api/users",
    "/api/groups",
    "/api/groups/unit:T40106/users",
    "/api/partitions",
    "/api/partitions/queue",
    "/api/partitions/queue/progress",
    "/api/partitions/vram",
    "/api/partitions/vram/progress",
])
def test_window_routes_accept_30_days_reject_beyond(client, route):
    # Every windowed route validates the shared dashboard window selector:
    # the largest frontend choice (720 h) is accepted, anything beyond
    # (721 h) is a 422 validation error.
    assert client.get(route, params={"since_hours": 720}).status_code == 200
    assert client.get(route, params={"since_hours": 721}).status_code == 422


@pytest.mark.parametrize("route", [
    "/api/jobs",
    "/api/jobs/1",
    "/api/users",
    "/api/groups",
    "/api/partitions",
])
def test_window_routes_span_720_hours_with_1800s_step(client, route):
    data = client.get(route, params={"since_hours": 720}).json()
    assert data["window"]["end"] - data["window"]["start"] == 720 * 3600
    # Routes exposing the query step must carry the 1,800 s long-window
    # value; list endpoints without a step field are skipped.
    if "step" in data:
        assert data["step"] == 1800


def test_vram_progress_route_resolves_exact_identity(client):
    # The VRAM poll resolves the collapsed dump identity: the same
    # window's state is visible to every partition/flag combination
    # (plan §3 — one dump serves all shapes), while a different window
    # is a different fetch and an unknown identity reads as null.
    key = cache.vram_progress_key(24)
    domain_partitions.progress_store[key] = {
        "done": 2, "total": 5, "failed_batches": 0}
    try:
        for params in ({"since_hours": 24, "partition": "h200"},
                       {"since_hours": 24, "partition": "h100",
                        "running_only": "true"}):
            hit = client.get("/api/partitions/vram/progress",
                             params=params)
            assert hit.status_code == 200
            assert hit.json() == {"done": 2, "total": 5,
                                  "failed_batches": 0}
        # A different window is a different dump fetch.
        other = client.get("/api/partitions/vram/progress",
                           params={"since_hours": 72, "partition": "h200"})
        assert other.json() is None
    finally:
        domain_partitions.progress_store.pop(key, None)
