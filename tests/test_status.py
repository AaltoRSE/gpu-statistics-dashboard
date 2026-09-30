"""Status tab: the cluster-wide live snapshot and the seven-day history,
every job figure split into CPU jobs and GPU jobs.

Domain tests drive domain.status directly with epoch-built sacct rows;
endpoint tests use the conftest-shared client/fake_prom fixtures (fake
scontrol/squeue/sacct and the fixed clock NOW, 2026-08-30T17:26:40
Helsinki).
"""

from test_app import NODES, NOW

import cache
import deps
from domain import status
from slurm import SlurmError, _cluster_iso

H0 = 1790150400  # an hour boundary (2026-09-23T11:00 Helsinki)
M = 60
HR = 3600


def row(jobid, state, submit=None, eligible=None, start=None, end=None,
        ncpus=4, gpus=0, raw=None):
    """One parsed sacct status row from epoch timestamps."""
    return {"jobid": jobid, "jobid_raw": raw or jobid.split("_")[0],
            "state": state, "submit": _cluster_iso(submit),
            "eligible": _cluster_iso(eligible), "start": _cluster_iso(start),
            "end": _cluster_iso(end), "partition": "p", "ncpus": ncpus,
            "gpus": gpus}


# ---- classification -------------------------------------------------

def test_outcome_groups():
    assert status.outcome("COMPLETED") == "completed"
    assert status.outcome("CANCELLED by 1234") == "cancelled"
    assert status.outcome("CANCELLED+") == "cancelled"
    for failed in ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL",
                   "BOOT_FAIL", "DEADLINE"):
        assert status.outcome(failed) == "failed"
    assert status.outcome("PREEMPTED") == "other"


def test_array_tasks_and_job_class():
    assert status.array_tasks("1234_[0-11%3]") == 12
    assert status.array_tasks("1234_[1,3,5-7]") == 5
    assert status.array_tasks("1234_47") == 1   # one started task
    assert status.array_tasks("1234") == 1
    assert status.job_class(0) == "cpu"
    assert status.job_class(None) == "cpu"
    assert status.job_class(2) == "gpu"


# ---- history: one chunk ------------------------------------------------

def test_reduce_chunk_attributes_each_event_to_its_hour():
    lo, hi = H0, H0 + 4 * HR
    rows = [
        # CPU job: submitted and started in hour 0, ended in hour 2
        row("1", "COMPLETED", H0 + 10 * M, H0 + 10 * M, H0 + 40 * M,
            H0 + 2 * HR + 10 * M, ncpus=4),
        # a pending 4-task GPU array: four submissions, nothing held
        row("7_[0-3]", "PENDING", H0 + 5 * M, H0 + 5 * M, gpus=2),
        # a 2-task GPU array cancelled while pending: two cancellations
        row("8_[0-1]", "CANCELLED by 5", H0 - 2 * HR, end=H0 + 3 * HR + 30 * M,
            gpus=1),
        # events outside the chunk: submitted before lo, ends after hi
        row("11", "COMPLETED", H0 - HR, H0 - HR, H0 - 30 * M, hi + HR,
            ncpus=1),
    ]
    out = status.reduce_chunk(rows, lo, hi, now=hi, live_ids=set())
    h = out["hours"]
    assert h[H0]["cpu"]["submitted"] == 1
    assert h[H0]["gpu"]["submitted"] == 4
    assert h[H0]["cpu"]["started"] == 1
    assert list(out["waits"][H0]["cpu"]) == [30 * M]
    assert h[H0 + 2 * HR]["cpu"]["ended"] == {"COMPLETED": 1}
    assert h[H0 + 3 * HR]["gpu"]["ended"] == {"CANCELLED": 2}
    # job 1 holds 4 cores for 20 min, 60 min, 10 min; job 11 one core
    # for the whole chunk (its own events are outside it)
    assert h[H0]["cpu"]["cpu_s"] == 20 * M * 4 + HR
    assert h[H0 + HR]["cpu"]["cpu_s"] == HR * 4 + HR
    assert h[H0 + 2 * HR]["cpu"]["cpu_s"] == 10 * M * 4 + HR
    assert h[H0 + 3 * HR]["cpu"]["cpu_s"] == HR
    # nothing submitted or started outside [lo, hi) leaks in
    assert sum(c["submitted"] for per in h.values() for c in per.values()) == 5
    assert out["records"] == 4


def test_reduce_chunk_running_rows_need_the_live_job_set():
    lo, hi = H0, H0 + 2 * HR
    now = H0 + HR + 30 * M
    rows = [
        # still running and known to the controller: held until now
        row("20", "RUNNING", H0 - 5 * HR, H0 - 5 * HR, H0 - HR, ncpus=8,
            gpus=1),
        # a stale row: sacct says running, the controller does not know it
        row("21", "RUNNING", H0 - 400 * 24 * HR, H0 - 400 * 24 * HR,
            H0 - 400 * 24 * HR, ncpus=20),
    ]
    out = status.reduce_chunk(rows, lo, hi, now, live_ids={"20"})
    h = out["hours"]
    assert h[H0]["gpu"]["cpu_s"] == HR * 8
    assert h[H0]["gpu"]["gpu_s"] == HR
    assert h[H0 + HR]["gpu"]["gpu_s"] == 30 * M   # clipped to now
    assert h[H0]["cpu"]["cpu_s"] == 0              # the stale row holds nothing
    assert out["phantoms"] == {"21"}
    assert out["unverified"] == set()

    # scontrol unavailable: the age cut-off stands in, and the rest is
    # counted but disclosed as unverified
    out = status.reduce_chunk(rows, lo, hi, now, live_ids=None)
    assert out["phantoms"] == {"21"}
    assert out["unverified"] == {"20"}
    assert out["hours"][H0]["gpu"]["gpu_s"] == HR


def test_reduce_chunk_waits_are_eligible_to_start():
    lo, hi = H0, H0 + HR
    rows = [
        # held by a dependency for 3 h, then queued 10 min
        row("30", "COMPLETED", H0 - 3 * HR, H0 + 5 * M, H0 + 15 * M,
            H0 + 20 * M),
        # eligible time unknown: started, but no wait sample
        row("31", "COMPLETED", H0, None, H0 + 5 * M, H0 + 6 * M),
        # eligible after start (clock skew): no sample either
        row("32", "COMPLETED", H0, H0 + 9 * M, H0 + 8 * M, H0 + 9 * M),
    ]
    out = status.reduce_chunk(rows, lo, hi, now=hi, live_ids=set())
    assert out["hours"][H0]["cpu"]["started"] == 3
    assert list(out["waits"][H0]["cpu"]) == [10 * M]


def test_midnight_spanning_job_counts_once_across_chunks():
    # sacct returns the job for both chunks (it was active in both); each
    # event still lands in exactly one.
    job = row("40", "FAILED", H0 + HR, H0 + HR, H0 + 2 * HR, H0 + 5 * HR,
              ncpus=2)
    first = status.reduce_chunk([job], H0, H0 + 3 * HR, H0 + 6 * HR, set())
    second = status.reduce_chunk([job], H0 + 3 * HR, H0 + 6 * HR,
                                 H0 + 6 * HR, set())
    merged = status.merge_history([first, second], H0, H0 + 6 * HR,
                                  total_chunks=2, running_verified=True)
    week = merged["summary"]["week"]["cpu"]
    assert (week["submitted"], week["started"], week["failed"]) == (1, 1, 1)
    held = merged["series"]["cpu"]["cpus_alloc"]
    # 2 cores from hour 2 through hour 4, in both chunks' halves
    assert held[:6] == [0.0, 0.0, 2.0, 2.0, 2.0, 0.0]


# ---- history: merge ----------------------------------------------------

def _week():
    now = H0 + 168 * HR + 30 * M
    start = H0
    rows = [
        # a week ago: CPU completed, 1 h wait
        row("50", "COMPLETED", H0 + HR, H0 + HR, H0 + 2 * HR, H0 + 3 * HR),
        # today: GPU timed out after a 20 min wait
        row("51", "TIMEOUT", now - 5 * HR, now - 5 * HR, now - 5 * HR + 20 * M,
            now - HR, gpus=1),
        # today: CPU cancelled while pending
        row("52", "CANCELLED by 9", now - 3 * HR, end=now - 2 * HR),
    ]
    chunk = status.reduce_chunk(rows, start, now, now, set())
    return status.merge_history([chunk], start, now, total_chunks=1,
                                running_verified=True), start, now


def test_merge_history_day_and_week_summaries():
    merged, start, now = _week()
    assert merged["available"] is True
    assert merged["windows"]["week"] == {"start": start, "end": now}
    assert merged["windows"]["day"]["start"] == H0 + 168 * HR - 24 * HR
    day, week = merged["summary"]["day"], merged["summary"]["week"]
    assert day["gpu"]["failed"] == 1 and day["gpu"]["states"] == {"TIMEOUT": 1}
    assert day["cpu"]["cancelled"] == 1 and day["cpu"]["completed"] == 0
    assert day["all"]["submitted"] == 2 and week["all"]["submitted"] == 3
    assert week["cpu"]["completed"] == 1
    assert week["cpu"]["wait_avg_s"] == HR
    assert day["cpu"]["wait_samples"] == 0 and day["cpu"]["wait_avg_s"] is None
    assert day["gpu"]["wait_p50_s"] == 20 * M
    # "all" pools both classes' samples, not an average of averages
    assert week["all"]["wait_samples"] == 2
    assert week["all"]["wait_avg_s"] == (HR + 20 * M) // 2
    # one axis point per hour, the current partial hour included
    assert len(merged["series"]["t"]) == 169
    assert merged["coverage"]["complete"] is True


def test_merge_history_uncovered_hours_are_gaps_not_zeros():
    now = H0 + 4 * HR
    only = status.reduce_chunk([], H0 + 2 * HR, now, now, set())
    merged = status.merge_history([only], H0, now, total_chunks=2,
                                  running_verified=True)
    s = merged["series"]["cpu"]
    assert s["submitted"][:2] == [None, None]
    assert s["cpus_alloc"][:2] == [None, None]
    assert s["submitted"][2:4] == [0, 0]
    assert merged["coverage"]["failed_chunks"] == 1
    assert merged["coverage"]["complete"] is False


# ---- live ---------------------------------------------------------------

def test_live_occupancy_counts_unavailable_capacity():
    out = status.live_status(NODES, {}, [], NOW)
    # gpu2 is ALLOCATED* (not responding): its unallocated capacity is
    # unavailable, the rest of the cluster's is idle
    assert out["cpu"] == {"alloc": 80, "idle": 568, "unavailable": 32,
                          "total": 680, "occupancy_pct": 11.8}
    assert out["gpu"] == {"alloc": 6, "idle": 37, "unavailable": 5,
                          "total": 48, "occupancy_pct": 12.5}
    assert out["nodes"]["unavailable"] == 1
    assert out["nodes"]["total"] == len(NODES)


def test_live_jobs_split_by_class_and_partition():
    jobs = {
        "1": {"state": "RUNNING", "partition": "gpu-h200", "ncpus": 8,
              "gpus": 2},
        "2": {"state": "RUNNING", "partition": "batch", "ncpus": 4,
              "gpus": 0},
        "3": {"state": "PENDING", "partition": "batch", "ncpus": 1,
              "gpus": 0},
    }
    pending = [
        {"jobid": "10", "partition": "batch", "reason": "Priority",
         "gpus": 0, "req_gpus": 0, "eligible": "2026-08-30T16:26:40",
         "array_tasks": 12},
        {"jobid": "11", "partition": "gpu-h200,gpu-h100", "reason":
         "Dependency", "gpus": 0, "req_gpus": 1, "eligible": "",
         "array_tasks": 1},
    ]
    out = status.live_status(NODES, jobs, pending, NOW)
    assert out["running"] == {"cpu": {"jobs": 1, "cpus": 4, "gpus": 0},
                              "gpu": {"jobs": 1, "cpus": 8, "gpus": 2}}
    cpu, gpu = out["pending"]["cpu"], out["pending"]["gpu"]
    assert (cpu["jobs"], cpu["tasks"], cpu["eligible_jobs"]) == (1, 12, 1)
    assert cpu["eligible_age_avg_s"] == 3600
    # req_gpus alone makes a GPU job; no eligible time means blocked
    assert (gpu["jobs"], gpu["blocked_jobs"], gpu["eligible_jobs"]) == (1, 1, 0)
    assert gpu["eligible_age_avg_s"] is None
    assert out["pending_reasons"] == [
        {"reason": "Dependency", "cpu_jobs": 0, "gpu_jobs": 1},
        {"reason": "Priority", "cpu_jobs": 1, "gpu_jobs": 0}]
    parts = {p["name"]: p for p in out["partitions"]}
    assert parts["batch"]["kind"] == "cpu"
    assert (parts["batch"]["running_jobs"], parts["batch"]["pending_jobs"]) \
        == (1, 1)
    # a job listing several partitions is pending in each
    assert parts["gpu-h200"]["pending_jobs"] == 1
    assert parts["gpu-h100"]["pending_jobs"] == 1
    assert parts["gpu-h200"]["kind"] == "gpu"
    assert parts["gpu-h200"]["gpus_total"] == 8


def test_live_unavailable_sources_are_null_not_zero():
    out = status.live_status(NODES, None, None, NOW)
    assert out["jobs_available"] is False and out["queue_available"] is False
    assert out["running"] is None and out["pending"] is None
    assert out["pending_reasons"] is None
    assert all(p["running_jobs"] is None and p["pending_jobs"] is None
               for p in out["partitions"])
    out = status.live_status(None, {}, [], NOW)
    assert out["cpu"] is None and out["gpu"] is None
    assert out["partitions"] is None and out["nodes"] is None
    assert out["running"]["cpu"]["jobs"] == 0


# ---- endpoints -----------------------------------------------------------

def test_status_live_endpoint(client, monkeypatch):
    monkeypatch.setattr(deps, "show_jobs", lambda: {
        "1": {"state": "RUNNING", "partition": "gpu-h200", "ncpus": 8,
              "gpus": 2}})
    monkeypatch.setattr(deps, "queue_pending", lambda: [
        {"jobid": "10", "partition": "batch", "reason": "Priority",
         "gpus": 0, "req_gpus": 0, "eligible": "", "array_tasks": 1}])
    body = client.get("/api/status/live").json()
    assert body["time"] == NOW
    assert body["cpu"]["occupancy_pct"] == 11.8
    assert body["running"]["gpu"]["jobs"] == 1
    assert body["pending"]["cpu"]["blocked_jobs"] == 1


def test_status_live_squeue_down_is_not_an_empty_queue(client, monkeypatch):
    def boom():
        raise SlurmError("squeue: slurm_load_jobs error")
    monkeypatch.setattr(deps, "queue_pending", boom)
    body = client.get("/api/status/live").json()
    assert body["queue_available"] is False
    assert body["pending"] is None
    assert body["cpu"]["total"] == 680  # the other sources still render
    assert body["running"] is not None


def test_status_live_refresh_rereads_snapshots(client, fake_prom, monkeypatch):
    calls = []
    monkeypatch.setattr(deps, "queue_pending",
                        lambda: calls.append(1) or [])
    client.get("/api/status/live")
    client.get("/api/status/live")
    assert len(calls) == 1                       # 30 s cache
    before = fake_prom.nodes_calls
    client.get("/api/status/live", params={"refresh": "true"})
    assert len(calls) == 2
    assert fake_prom.nodes_calls == before + 1


def test_status_history_endpoint(client):
    body = client.get("/api/status/history").json()
    assert body["available"] is True
    assert body["capacity"] == {"cpus": 680, "gpus": 48}
    day, week = body["summary"]["day"], body["summary"]["week"]
    # today: 501 (CPU, completed), 502 (GPU, OOM), the 4-task array, and
    # 507 (submitted 23:30 last night — inside the 17:00 day start)
    assert day["cpu"]["submitted"] == 2
    assert day["gpu"]["submitted"] == 5
    assert day["gpu"]["failed"] == 1
    assert day["gpu"]["states"] == {"OUT_OF_MEMORY": 1}
    assert day["cpu"]["completed"] == 2
    # 507 waited 5 min from eligibility, not 45 min from submission
    assert sorted([day["cpu"]["wait_p50_s"], day["cpu"]["wait_p90_s"]]) \
        == [300, 300]
    assert week["cpu"]["cancelled"] == 1
    assert week["cpu"]["failed"] == 1        # 506's TIMEOUT
    assert week["all"]["ended"] == 5
    # the stale running row is excluded and disclosed
    assert body["coverage"]["stale_running_rows"] == 1
    assert body["coverage"]["complete"] is True
    assert body["coverage"]["chunks"] == 8
    # 506 held 16 cores across midnight in both day chunks
    t = body["series"]["t"]
    held = dict(zip(t, body["series"]["cpu"]["cpus_alloc"]))
    assert held[1787896800] == 16.0   # 2026-08-28T09:00 local
    assert held[1787979600] == 16.0   # 2026-08-29T08:00, the next day
    assert held[1787983200] == 0.0    # 2026-08-29T09:00, timed out


def test_status_history_warm_and_refresh_fetch_counts(client, monkeypatch):
    calls = []

    def sacct(start_iso, end_iso):
        calls.append(start_iso)
        return []

    monkeypatch.setattr(deps, "sacct_status", sacct)
    client.get("/api/status/history")
    assert len(calls) == 8                  # 2 edges + 6 full days
    client.get("/api/status/history")
    assert len(calls) == 8                  # warm: no sacct at all
    client.get("/api/status/history", params={"refresh": "true"})
    assert len(calls) == 10                 # only the two edges again


def test_status_history_partial_failure_is_disclosed(client, monkeypatch):
    def sacct(start_iso, end_iso):
        if start_iso.startswith("2026-08-25"):
            raise SlurmError("slurmdbd timeout")
        return []

    monkeypatch.setattr(deps, "sacct_status", sacct)
    body = client.get("/api/status/history").json()
    assert body["available"] is True
    assert body["coverage"]["failed_chunks"] == 1
    assert body["coverage"]["complete"] is False
    t = body["series"]["t"]
    lost = [v for ts, v in zip(t, body["series"]["cpu"]["submitted"])
            if v is None]
    assert len(lost) == 24                  # the whole failed day, as gaps


def test_status_history_total_failure_is_unavailable_and_retried(
        client, monkeypatch):
    calls = []

    def sacct(start_iso, end_iso):
        calls.append(start_iso)
        raise SlurmError("sacct down")

    monkeypatch.setattr(deps, "sacct_status", sacct)
    resp = client.get("/api/status/history")
    assert resp.status_code == 200
    assert resp.json()["available"] is False
    assert resp.json()["summary"] is None
    first = len(calls)
    client.get("/api/status/history")
    assert len(calls) == 2 * first          # never cached as an empty week
    assert client.get("/api/status/history/progress").json() is None


def test_status_history_progress_is_published_and_cleared(client, monkeypatch):
    seen = []

    def sacct(start_iso, end_iso):
        seen.append(cache.progress_store.get(cache.status_progress_key()))
        return []

    monkeypatch.setattr(deps, "sacct_status", sacct)
    client.get("/api/status/history")
    assert all(s and s["total"] == 8 for s in seen)
    assert client.get("/api/status/history/progress").json() is None


def test_status_history_without_scontrol_flags_unverified(client, monkeypatch):
    def boom():
        raise SlurmError("scontrol down")

    monkeypatch.setattr(deps, "show_jobs", boom)
    body = client.get("/api/status/history").json()
    assert body["coverage"]["running_verified"] is False
    # the 2025 row is still caught by the age cut-off
    assert body["coverage"]["stale_running_rows"] == 1


def test_status_deep_link_serves_the_shell(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    assert 'id="tab-status"' in resp.text
