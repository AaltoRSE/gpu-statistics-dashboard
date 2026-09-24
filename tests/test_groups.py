"""/api/groups endpoint tests: roll-up math, the always-present special
rows, department level, NSS failure semantics, and the shared-fetch
guarantee (a groups request must add no Prometheus query).

Run: .venv/bin/python -m pytest tests/test_groups.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from test_app import USER_GROUPS  # noqa: E402

import deps  # noqa: E402


@pytest.fixture()
def nss(monkeypatch):
    """A mutable in-memory directory over deps.user_groups."""
    accounts = {user: list(groups) for user, groups in USER_GROUPS.items()}
    calls = []

    def user_groups(username):
        calls.append(username)
        if username not in accounts:
            return None
        return list(accounts[username])

    monkeypatch.setattr(deps, "user_groups", user_groups)
    return {"accounts": accounts, "calls": calls}


def by_id(data):
    return {row["group_id"]: row for row in data["groups"]}


def test_groups_rollup_math(client, nss):
    # step=120 s; per-job figures (see test_users_aggregates_per_user):
    # alice job 1: samples (40, 60) -> mean 50, util-gpu-h 0.03, held
    #   2 samples x 120 s / 3600 = 0.07 GPU-hours
    # bob job 2: sample (10) -> mean 10, util-gpu-h 0.0, held 0.03
    # carol job 3: samples (90, 95) -> mean 92.5, util-gpu-h 0.06
    # dave job 4: samples (80, 90) -> mean 85, util-gpu-h 0.06
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["level"] == "unit"
    rows = by_id(data)
    # The always-present rows exist even when empty.
    assert {"unit:T40106", "unit:T30010", "dept:T313",
            "unaffiliated", "unresolved"} <= set(rows)
    kyrki = rows["unit:T40106"]
    assert kyrki["group_name"] == "Kyrki Ville group"
    assert kyrki["dept_code"] == "T410"
    assert kyrki["dept_name"] == "Electrical Engineering and Automation"
    assert kyrki["school_code"] == "ELEC"
    assert kyrki["school_name"] == "School of Electrical Engineering"
    assert kyrki["users"] == 1 and kyrki["jobs"] == 1
    assert kyrki["running_jobs"] == 1
    assert kyrki["mean_util"] == pytest.approx(50.0)
    assert kyrki["util_gpu_hours"] == pytest.approx(0.03)
    assert kyrki["gpu_hours"] == pytest.approx(0.07)
    assert kyrki["vram_avg"] == pytest.approx(11.0)
    assert kyrki["low_eff_jobs"] == 0
    assert kyrki["top_users"] == [{"user": "alice", "util_gpu_hours": 0.03}]
    sci_it = rows["unit:T30010"]
    assert sci_it["group_name"] == "Science IT Technical services"
    assert sci_it["school_code"] == "SCI"
    assert sci_it["mean_util"] == pytest.approx(10.0)
    assert sci_it["low_eff_jobs"] == 1  # job 2's mean is 10 < 30
    # carol has osasto-t313 only: a department row, marked "(no unit)"
    t313 = rows["dept:T313"]
    assert t313["group_name"] == "Computer Science (no unit)"
    assert t313["dept_code"] == "T313" and t313["school_code"] == "SCI"
    # dave has no org groups: the unaffiliated row, never a fake unit
    unaff = rows["unaffiliated"]
    assert unaff["users"] == 1 and unaff["jobs"] == 1
    assert unaff["mean_util"] == pytest.approx(85.0)
    assert unaff["school_code"] is None
    # nobody unknown in this window: the row is a genuine zero row
    assert rows["unresolved"]["users"] == 0
    assert rows["unresolved"]["jobs"] == 0
    assert rows["unresolved"]["mean_util"] == 0.0
    assert rows["unresolved"]["vram_avg"] is None
    assert data["coverage"] == {
        "users": 4, "affiliated": 3, "unaffiliated": 1, "unresolved": 0,
        "failed": 0, "unmapped_codes": [],
    }
    # groups are ordered by util_gpu_hours desc (ties by name)
    ids = [row["group_id"] for row in data["groups"]]
    assert ids == ["dept:T313", "unaffiliated", "unit:T40106",
                   "unit:T30010", "unresolved"]
    # the school filter options ride along
    assert {s["code"] for s in data["schools"]} == {"T1", "T2", "T3",
                                                    "T4", "T5", "T6"}
    assert next(s for s in data["schools"] if s["code"] == "T4") == {
        "code": "T4", "short": "ELEC",
        "full": "School of Electrical Engineering"}


def test_groups_mean_util_reweights_all_member_samples(client, nss,
                                                       monkeypatch):
    # A group of a 10% one-sample job and a 90% one-sample job must be
    # sample-weighted, not averaged per user mean: the 90% job has 10
    # samples, the 10% job 1, so a per-user-mean average would give 50%
    # but the sample-weighted figure is (900 + 10) / 11 = 82.73.
    import sources

    extra = [{"metric": {"slurmjobid": "50", "instance": "gpu1", "gpu": "0",
                         "job": "gpu-h100", "user": "bob",
                         "gpu_type": "h100"},
              "values": [[1000 + i * 120, str(90)] for i in range(10)]}]
    real = sources.gpu_util

    def with_extra(win):
        return real(win) + extra

    monkeypatch.setattr(sources, "gpu_util", with_extra)
    data = client.get("/api/groups", params={"since_hours": 24}).json()
    rows = by_id(data)
    # both of bob's jobs land in his own group regardless of GPU type
    sci_it = rows["unit:T30010"]
    assert sci_it["users"] == 1 and sci_it["jobs"] == 2
    assert sci_it["mean_util"] == pytest.approx(82.73, abs=0.01)


def test_groups_department_level(client, nss):
    data = client.get("/api/groups",
                      params={"since_hours": 24, "level": "department"}).json()
    rows = by_id(data)
    # unit users collapse into their department; no "(no unit)" suffix
    assert "unit:T40106" not in rows
    t410 = rows["dept:T410"]
    assert t410["group_name"] == "Electrical Engineering and Automation"
    t313 = rows["dept:T313"]
    assert t313["group_name"] == "Computer Science"
    assert t313["users"] == 1  # carol's own osasto, no "(no unit)" needed
    # the special rows survive the level change
    assert rows["unaffiliated"]["users"] == 1
    assert rows["unresolved"]["users"] == 0


def test_groups_unaffiliated_and_unresolved_always_render(client, nss):
    # everyone classified: both rows still present — "no row" must never
    # be read as "everyone is classified"
    data = client.get("/api/groups", params={"since_hours": 24}).json()
    rows = by_id(data)
    assert "unaffiliated" in rows and "unresolved" in rows
    assert rows["unaffiliated"]["users"] == 1
    assert rows["unresolved"]["users"] == 0
    assert rows["unresolved"]["mean_util"] == 0.0


def test_groups_unknown_user_is_unresolved(client, nss, monkeypatch):
    # a user the directory does not know is Unresolved — an answer, not
    # a failure — and never lands in Unaffiliated.
    def unknown_dave(username):
        if username == "dave":
            return None
        return USER_GROUPS.get(username)

    monkeypatch.setattr(deps, "user_groups", unknown_dave)
    data = client.get("/api/groups", params={"since_hours": 24}).json()
    rows = by_id(data)
    assert rows["unaffiliated"]["users"] == 0
    assert rows["unresolved"]["users"] == 1
    assert rows["unresolved"]["jobs"] == 1
    assert rows["unresolved"]["mean_util"] == pytest.approx(85.0)
    assert data["coverage"]["unresolved"] == 1


def test_groups_partial_nss_failure_keeps_the_rest(client, nss, monkeypatch):
    real = deps.user_groups

    def flaky(username):
        if username == "alice":
            raise deps.DirectoryError("sssd worker timeout")
        return real(username)

    monkeypatch.setattr(deps, "user_groups", flaky)
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["coverage"]["failed"] == 1
    rows = by_id(data)
    # alice's activity is in NO row (disclosed as failed, never faked)
    assert "unit:T40106" not in rows
    assert rows["unit:T30010"]["users"] == 1  # bob still resolves


def test_groups_total_nss_failure_is_502(client, nss, monkeypatch):
    def boom(username):
        raise deps.DirectoryError("sssd down")

    monkeypatch.setattr(deps, "user_groups", boom)
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "directory_unreachable"


def test_groups_missing_org_file_is_502(client, nss, monkeypatch, tmp_path):
    monkeypatch.setenv("ORG_UNITS_FILE",
                       str(tmp_path / "does-not-exist.conf"))
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "directory_unreachable"
    assert "does-not-exist.conf" in r.json()["detail"]


def test_groups_repeat_request_makes_zero_nss_calls(client, nss):
    client.get("/api/groups", params={"since_hours": 24})
    assert len(nss["calls"]) == 4
    client.get("/api/groups", params={"since_hours": 24})
    client.get("/api/groups", params={"since_hours": 24, "level": "department"})
    # per-user classification cache: same users, same directory answers
    assert len(nss["calls"]) == 4


def test_groups_running_only_filters_in_process(client, nss, fake_prom):
    r = client.get("/api/groups",
                   params={"since_hours": 24, "running_only": "true"})
    assert r.status_code == 200
    rows = by_id(r.json())
    # job 3 (carol) is not running: her department row is gone; the
    # util ranges carry no matcher (in-process filter over the shared fetch)
    assert "dept:T313" not in rows
    assert all("=~" not in q for t, q in fake_prom.calls if t == "range")


def test_groups_drilldown_members(client, nss):
    r = client.get("/api/groups/unit:T40106/users",
                   params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["group_id"] == "unit:T40106"
    assert data["group_name"] == "Kyrki Ville group"
    assert data["count"] == 1
    member = data["users"][0]
    assert member["user"] == "alice"
    assert member["unit_code"] == "T40106"
    assert member["dept_code"] == "T410"
    assert member["extra_units"] == []
    assert member["status"] == "unit"
    # a unit-less user's drill-down carries no unit code
    r = client.get("/api/groups/dept:T313/users", params={"since_hours": 24})
    assert r.status_code == 200
    member = r.json()["users"][0]
    assert member["user"] == "carol"
    assert member["unit_code"] is None and member["dept_code"] == "T313"
    # unknown group: 404, never an empty member list
    r = client.get("/api/groups/unit:T00000/users",
                   params={"since_hours": 24})
    assert r.status_code == 404


def test_groups_drilldown_shows_extra_units(client, nss):
    # hannuse2-style multi-unit membership: the preferred unit owns the
    # row, the other rides along as extra_units in the drill-down.
    nss["accounts"]["carol"] = ["laitos-t40106", "laitos-t40714",
                                "osasto-t412"]
    data = client.get("/api/groups/unit:T40714/users",
                      params={"since_hours": 24}).json()
    member = data["users"][0]
    assert member["user"] == "carol"
    assert member["unit_code"] == "T40714"  # osasto-t412 prefers T40714
    assert member["extra_units"] == ["T40106"]


def test_users_then_groups_add_no_prometheus_query(client, nss, fake_prom):
    client.get("/api/users", params={"since_hours": 24})
    before = list(fake_prom.calls)
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 200
    # the groups pipeline reads the same shared window sources: no new
    # range or instant query, only per-user NSS classification
    assert fake_prom.calls == before
