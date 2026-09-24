"""/api/groups endpoint tests: roll-up math, the always-present special
rows, department level, NSS failure semantics, and the shared-fetch
guarantee (a groups request must add no Prometheus query).

Run: .venv/bin/python -m pytest tests/test_groups.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from test_app import GROUP_MEMBERS, USER_GROUPS  # noqa: E402

import deps  # noqa: E402


@pytest.fixture()
def nss(monkeypatch):
    """Mutable in-memory directories over deps.user_groups and
    deps.group_members."""
    accounts = {user: list(groups) for user, groups in USER_GROUPS.items()}
    members = {name: list(m) for name, m in GROUP_MEMBERS.items()}
    calls, member_calls = [], []

    def user_groups(username):
        calls.append(username)
        if username not in accounts:
            return None
        return list(accounts[username])

    def group_members(name):
        member_calls.append(name)
        if name not in members:
            return None
        return list(members[name])

    monkeypatch.setattr(deps, "user_groups", user_groups)
    monkeypatch.setattr(deps, "group_members", group_members)
    return {"accounts": accounts, "members": members,
            "calls": calls, "member_calls": member_calls}


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
    assert data["level"] == "group"
    rows = by_id(data)
    # The always-present rows exist even when empty.
    assert {"kyrkiv1", "dept:T300", "dept:T313",
            "unaffiliated", "unresolved"} <= set(rows)
    kyrki = rows["kyrkiv1"]
    assert kyrki["group_name"] == "Kyrki Ville"
    assert kyrki["leader"] == "kyrkiv1"
    assert kyrki["leader_name"] == "Kyrki Ville"
    assert kyrki["unit_codes"] == ["T40106"]
    assert kyrki["dept_code"] == "T410"
    assert kyrki["dept_name"] \
        == "Department of Electrical Engineering and Automation"
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
    # bob's laitos-t30010 belongs to no professor in the test conf: an
    # honest department-only row, named after the department
    t300 = rows["dept:T300"]
    assert t300["group_name"] \
        == "Department of Computer Science, no professor group"
    assert t300["leader"] is None and t300["unit_codes"] == []
    assert t300["school_code"] == "SCI"
    assert t300["mean_util"] == pytest.approx(10.0)
    assert t300["low_eff_jobs"] == 1  # job 2's mean is 10 < 30
    # carol has osasto-t313 only: no configured department name, so the
    # code itself names the row
    t313 = rows["dept:T313"]
    assert t313["group_name"] == "T313, no professor group"
    assert t313["dept_code"] == "T313" and t313["school_code"] == "SCI"
    # dave has no relevant groups: the unaffiliated row, never a fake group
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
        "users": 4, "in_prof_group": 1, "dept_only": 2, "unaffiliated": 1,
        "unresolved": 0, "failed": 0,
    }
    # rows are ordered by util_gpu_hours desc (ties by name)
    ids = [row["group_id"] for row in data["groups"]]
    assert ids == ["dept:T313", "unaffiliated", "kyrkiv1", "dept:T300",
                   "unresolved"]
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
    # both of bob's jobs land in his department-only row regardless of
    # GPU type
    t300 = rows["dept:T300"]
    assert t300["users"] == 1 and t300["jobs"] == 2
    assert t300["mean_util"] == pytest.approx(82.73, abs=0.01)


def test_groups_department_level(client, nss):
    data = client.get("/api/groups",
                      params={"since_hours": 24, "level": "department"}).json()
    rows = by_id(data)
    # group members collapse into the professor's department; no
    # "no professor group" suffix
    assert "kyrkiv1" not in rows
    t410 = rows["dept:T410"]
    assert t410["group_name"] \
        == "Department of Electrical Engineering and Automation"
    assert t410["leader"] is None
    t300 = rows["dept:T300"]
    assert t300["group_name"] == "Department of Computer Science"
    assert t300["users"] == 1  # bob's own osasto
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
    assert "kyrkiv1" not in rows
    assert rows["dept:T300"]["users"] == 1  # bob still resolves


def test_groups_total_nss_failure_is_502(client, nss, monkeypatch):
    def boom(username):
        raise deps.DirectoryError("sssd down")

    monkeypatch.setattr(deps, "user_groups", boom)
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "directory_unreachable"


def test_groups_missing_conf_file_is_502(client, nss, monkeypatch, tmp_path):
    monkeypatch.setenv("PROF_GROUPS_FILE",
                       str(tmp_path / "does-not-exist.conf"))
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 502
    assert r.json()["error"] == "directory_unreachable"
    assert "does-not-exist.conf" in r.json()["detail"]


def test_groups_repeat_request_makes_zero_directory_calls(client, nss):
    client.get("/api/groups", params={"since_hours": 24})
    assert len(nss["calls"]) == 4
    assert len(nss["member_calls"]) == 6   # 2 configured groups x 3 lists
    client.get("/api/groups", params={"since_hours": 24})
    client.get("/api/groups", params={"since_hours": 24, "level": "department"})
    # per-user group lists and per-group member lists are both cached:
    # same users, same groups, same directory answers
    assert len(nss["calls"]) == 4
    assert len(nss["member_calls"]) == 6


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
    r = client.get("/api/groups/kyrkiv1/users",
                   params={"since_hours": 24})
    assert r.status_code == 200
    data = r.json()
    assert data["group_id"] == "kyrkiv1"
    assert data["group_name"] == "Kyrki Ville"
    assert data["count"] == 1
    member = data["users"][0]
    assert member["user"] == "alice"
    assert member["group"] == "kyrkiv1"
    assert member["membership"] == "paid"
    assert member["dept_code"] == "T410"
    assert member["own_dept"] == "T410"
    assert member["extra_groups"] == []
    assert member["status"] == "group"
    # a department-only user's drill-down carries no group
    r = client.get("/api/groups/dept:T300/users", params={"since_hours": 24})
    assert r.status_code == 200
    member = r.json()["users"][0]
    assert member["user"] == "bob"
    assert member["group"] is None and member["dept_code"] == "T300"
    # unknown group: 404, never an empty member list
    r = client.get("/api/groups/nobody/users",
                   params={"since_hours": 24})
    assert r.status_code == 404


def test_groups_drilldown_shows_extra_groups(client, nss):
    # a user claimed by two configured groups: the strongest (here tied
    # -> lowest leader name) owns the row, the other rides along as an
    # extra group in the drill-down.
    nss["members"]["laitos-t40106"] = ["alice", "kyrkiv1", "bob"]
    nss["members"]["laitos-t40571"] = ["linc15", "bob"]
    data = client.get("/api/groups/backstt1/users",
                      params={"since_hours": 24}).json()
    member = data["users"][0]
    assert member["user"] == "bob"
    assert member["group"] == "backstt1"
    assert member["extra_groups"] == ["kyrkiv1"]


def test_groups_member_kind_reflects_the_membership_list(client, nss):
    # the same user lands with a different membership kind depending on
    # which of the unit's lists they are in
    nss["members"]["t40106-everyone"] = ["hannuse2", "carol"]
    data = client.get("/api/groups/kyrkiv1/users",
                      params={"since_hours": 24}).json()
    members = {m["user"]: m for m in data["users"]}
    assert members["alice"]["membership"] == "paid"
    assert members["carol"]["membership"] == "everyone"
    # carol's row department is the professor's, her own osasto rides along
    assert members["carol"]["dept_code"] == "T410"
    assert members["carol"]["own_dept"] == "T313"


def test_users_then_groups_add_no_prometheus_query(client, nss, fake_prom):
    client.get("/api/users", params={"since_hours": 24})
    before = list(fake_prom.calls)
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 200
    # the groups pipeline reads the same shared window sources: no new
    # range or instant query, only per-user NSS classification
    assert fake_prom.calls == before
