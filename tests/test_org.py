"""Professor-group classification tests (domain/org.py).

The loader is checked against synthetic conf files in tmp_path (the
committed prof_groups.conf is data, replaced wholesale when the real AD
dump is built — the suite must not depend on its contents). The NSS
boundaries (deps.user_groups, deps.group_members) are exercised with
patched pwd/os/grp — never against the real directory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pwd  # noqa: E402

import pytest  # noqa: E402

import cache  # noqa: E402
import deps  # noqa: E402
import domain.org as org  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_prof_cache():
    org.reset_prof_cache()
    yield
    org.reset_prof_cache()


@pytest.fixture()
def conf_file(tmp_path):
    def write(content, name="prof_groups.conf"):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        return str(f)
    return write


# ---- the loader ---------------------------------------------------------

def test_load_prof_groups_parses_sections(conf_file):
    path = conf_file(
        "[schools]\n"
        "T4 = ELEC | School of Electrical Engineering\n"
        "\n"
        "[departments]\n"
        "T410 = Electrical Engineering and Automation | T4\n"
        "\n"
        "[groups]\n"
        "kyrkiv1 = Kyrki Ville | T410 | T40106 t40107\n")
    m = org.load_prof_groups(path)
    assert m["schools"]["T4"] == {
        "short": "ELEC", "full": "School of Electrical Engineering"}
    assert m["departments"]["T410"] == {
        "name": "Electrical Engineering and Automation", "school": "T4"}
    assert m["groups"]["kyrkiv1"] == {
        "name": "Kyrki Ville", "dept": "T410",
        "unit_codes": ["T40106", "T40107"]}
    assert m["_source"][0] == path


def test_load_prof_groups_keys_are_case_insensitive(conf_file):
    path = conf_file(
        "[groups]\n"
        "Kyrkiv1 = Kyrki Ville | t410 | t40106\n")
    m = org.load_prof_groups(path)
    assert m["groups"]["Kyrkiv1"]["dept"] == "T410"
    assert m["groups"]["Kyrkiv1"]["unit_codes"] == ["T40106"]


def test_load_prof_groups_hand_editing_corners(conf_file):
    path = conf_file(
        "[groups]\n"
        "skaski1 = Kaski Samuel | T313 | \n"     # no units: leader-only
        "pipen1 = A | B | T313 | T31301\n"       # name may contain a pipe
        "nodept1 = No Dept | | T31302\n")        # empty department
    m = org.load_prof_groups(path)
    assert m["groups"]["skaski1"]["unit_codes"] == []
    # the last two pipes delimit: everything before is the name
    assert m["groups"]["pipen1"]["name"] == "A | B"
    assert m["groups"]["pipen1"]["dept"] == "T313"
    assert m["groups"]["nodept1"]["dept"] is None


def test_load_prof_groups_missing_sections_are_empty(conf_file):
    path = conf_file("[schools]\nT3 = SCI | School of Science\n")
    m = org.load_prof_groups(path)
    assert m["departments"] == {}
    assert m["groups"] == {}
    assert m["units"] == {}


def test_load_prof_groups_parses_units_section(conf_file):
    path = conf_file(
        "[units]\n"
        "T21204 = Mechatronics | T212\n"
        "t21403 = Performance in BDC | \n"   # empty dept: None
        "\n"
        "[groups]\n"
        "kyrkiv1 = Kyrki Ville | T410 | T40106\n")
    m = org.load_prof_groups(path)
    assert m["units"]["T21204"] == {"name": "Mechatronics", "dept": "T212"}
    assert m["units"]["T21403"] == {"name": "Performance in BDC",
                                    "dept": None}
    # a lower-case code still resolves upper-cased like every other key
    m2 = org.load_prof_groups(conf_file("[units]\nt21205 = X | t212\n",
                                        name="units_only.conf"))
    assert "T21205" in m2["units"]


def test_load_prof_groups_reloads_when_mtime_changes(conf_file):
    path = conf_file("[departments]\nT410 = Old name | T4\n")
    assert (org.load_prof_groups(path)["departments"]["T410"]["name"]
            == "Old name")
    # mtime granularity can be coarser than the edit; force a fresh stamp.
    conf_file("[departments]\nT410 = New name | T4\n")
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    assert (org.load_prof_groups(path)["departments"]["T410"]["name"]
            == "New name")


def test_load_prof_groups_env_override(conf_file, monkeypatch):
    path = conf_file("[departments]\nT410 = From override | T4\n")
    monkeypatch.setenv("PROF_GROUPS_FILE", path)
    assert org.load_prof_groups()["departments"]["T410"]["name"] \
        == "From override"
    assert org.prof_groups_path() == path


def test_load_prof_groups_missing_file_is_directory_error(conf_file):
    with pytest.raises(org.deps.DirectoryError, match="unavailable"):
        org.load_prof_groups("/no/such/prof_groups.conf")


# ---- conf lookups -------------------------------------------------------

def test_school_prefers_the_department_configured_key():
    conf = {"schools": {"T3": {"short": "SCI", "full": "Science"},
                        "T31": {"short": "SCI2", "full": "Longer match"}},
            "departments": {"T313": {"name": "CS", "school": "T31"}}}
    assert org.school_pair("T313", conf) == ("SCI2", "Longer match")


def test_school_longest_prefix_fallback_and_other():
    conf = {"schools": {"T3": {"short": "SCI", "full": "Science"},
                        "T31": {"short": "SCI2", "full": "Longer match"}},
            "departments": {}}
    # no configured key: the longest matching prefix wins
    assert org.school_pair("T313", conf) == ("SCI2", "Longer match")
    assert org.school_pair("T900", conf) == ("Other", None)
    assert org.school_pair(None, conf) == (None, None)


def test_dept_name_and_leader_name_fall_back():
    c = conf()
    assert org.dept_name("T999", c) == "T999"
    assert org.dept_name("T410", c) \
        == "Department of Electrical Engineering and Automation"
    assert org.leader_name("ghost1", c) == "ghost1"
    assert org.leader_name("kyrkiv1", c) == "Kyrki Ville"
    assert org.group_dept("kyrkiv1", c) == "T410"
    assert org.group_dept("ghost1", c) is None


# ---- the synthetic world (index + classify + rollup) --------------------

def conf():
    """The synthetic world: Kyrki (T40106, dept T410), Bäckström
    (T40571, dept T412), Alku (T40521, dept T412 — Bäckström sits in
    Alku's staff list, not their own unit's), Kaski Sami (T31301,
    dept T313) and one shared unit T21204 (Mechatronics, dept T212)."""
    return {
        "schools": {
            "T2": {"short": "ENG", "full": "School of Engineering"},
            "T3": {"short": "SCI", "full": "School of Science"},
            "T4": {"short": "ELEC",
                   "full": "School of Electrical Engineering"},
        },
        "departments": {
            "T212": {"name": "Department of Mechanical Engineering",
                     "school": "T2"},
            "T410": {"name": "Department of Electrical Engineering and "
                             "Automation", "school": "T4"},
            "T412": {"name": "Department of Information and Communications "
                             "Engineering", "school": "T4"},
            "T313": {"name": "Department of Computer Science",
                     "school": "T3"},
        },
        "groups": {
            "kyrkiv1": {"name": "Kyrki Ville", "dept": "T410",
                        "unit_codes": ["T40106"]},
            "backstt1": {"name": "Bäckström Tom", "dept": "T412",
                         "unit_codes": ["T40571"]},
            "alkut1": {"name": "Alku Alonen", "dept": "T412",
                       "unit_codes": ["T40521"]},
            "skaski1": {"name": "Kaski Sami", "dept": "T313",
                        "unit_codes": ["T31301"]},
        },
        "units": {"T21204": {"name": "Mechatronics", "dept": "T212"}},
    }


# The NSS member lists of the configured units' groups. 'pat' is in two
# units' laitos groups (the strength-tie case); backstt1's own unit's
# lists deliberately omit them (the leader seed must carry them).
# auto-ext- carries the unit's external visitors; duoext sits in BOTH
# the ext and staff lists (external beats staff), mixpaid in a laitos
# and another unit's ext list (paid beats external).
MEMBERS = {
    "laitos-t40106": ["alice", "bothpaid", "kyrkiv1", "mixpaid", "pat",
                     "unitos"],
    "t40106-staff": ["kyrkiv1"],
    "t40106-everyone": ["hannuse2"],
    "auto-ext-t40106": ["extvis"],
    "laitos-t40571": ["linc15", "pat"],
    "t40571-staff": ["backstt1", "duoext"],
    "t40571-everyone": ["linc15"],
    "auto-ext-t40571": ["duoext", "mixpaid"],
    "laitos-t40521": [],
    "t40521-staff": ["backstt1"],
    "t40521-everyone": [],
    "laitos-t31301": ["pete"],
    "t31301-staff": None,          # a unit with no staff group at all
    "laitos-t21204": ["bothpaid", "unitmike", "unitos"],
    "laitos-t99999": None,         # a group the directory does not know
    "t99999-staff": None,
    "t99999-everyone": None,
    "auto-ext-t99999": None,
}

USER_GROUPS_WORLD = {
    "alice": ["laitos-t40106", "osasto-t410"],
    "hannuse2": ["t40106-everyone", "osasto-t411"],
    "linc15": ["laitos-t40571", "t40571-everyone"],
    "pete": ["laitos-t31301", "osasto-t313"],
    "carol": ["osasto-t313"],
    "dave": ["triton-users"],
    "ghost": None,                 # unknown to the directory
    # external visitors carry no osasto group — the unit-code fallback
    # of own_dept_of is all the department they have
    "extvis": ["auto-ext-t40106"],
    "duoext": ["auto-ext-t40571", "t40571-staff"],
    "mixpaid": ["laitos-t40106", "auto-ext-t40571"],
    "unitmike": ["laitos-t21204", "osasto-t212"],
    "bothpaid": ["laitos-t40106", "laitos-t21204", "osasto-t313"],
    "unitos": ["laitos-t40106", "laitos-t21204", "osasto-t212"],
}


@pytest.fixture()
def nss(monkeypatch):
    """The synthetic world's directory + member lists, with the route
    cache fresh so group-membership caching is observable per test."""
    monkeypatch.setattr(deps, "route_cache", cache.TtlCache())
    monkeypatch.setattr(
        deps, "user_groups",
        lambda user: list(USER_GROUPS_WORLD[user])
        if user in USER_GROUPS_WORLD and USER_GROUPS_WORLD[user] is not None
        else None)
    calls = []

    def group_members(name):
        calls.append(name)
        if name not in MEMBERS or MEMBERS[name] is None:
            return None
        return list(MEMBERS[name])

    monkeypatch.setattr(deps, "group_members", group_members)
    return {"member_calls": calls}


@pytest.fixture()
def index(nss):
    return org.membership_index(conf())


def test_index_strengths_and_leader_seed(index):
    # alice: paid (laitos, 4); hannuse2: everyone (1); the leader is
    # seeded into their own group even though no member list has them.
    assert index["alice"] == {"kyrkiv1": 4}
    assert index["hannuse2"] == {"kyrkiv1": 1}
    assert index["backstt1"]["backstt1"] == 5
    # Bäckström sits in Alku's staff list — a second, weaker membership.
    assert index["backstt1"]["alkut1"] == 2


def test_index_external_kind_and_ordering(nss, index):
    # extvis is ONLY in the unit's auto-ext list: external (3), and no
    # osasto group exists to fall back on
    assert index["extvis"] == {"kyrkiv1": 3}
    r = org.classify("extvis", USER_GROUPS_WORLD["extvis"], index, conf())
    assert r == {
        "group": "kyrkiv1", "membership": "external", "dept_code": "T410",
        "school_code": "ELEC", "own_dept": "T401",
        "extra_groups": [], "status": "group"}
    # external (3) beats staff (2) in the same unit
    assert index["duoext"] == {"backstt1": 3}
    r = org.classify("duoext", USER_GROUPS_WORLD["duoext"], index, conf())
    assert r["group"] == "backstt1" and r["membership"] == "external"
    assert r["extra_groups"] == []
    # paid (4) beats another unit's external (3)
    assert index["mixpaid"] == {"kyrkiv1": 4, "backstt1": 3}
    r = org.classify("mixpaid", USER_GROUPS_WORLD["mixpaid"], index, conf())
    assert r["group"] == "kyrkiv1" and r["membership"] == "paid"
    assert r["extra_groups"] == ["backstt1"]


def test_index_strongest_strength_wins_within_one_group(index):
    # linc15 is in BOTH the unit's laitos and everyone lists: one group,
    # the strongest strength kept.
    assert index["linc15"] == {"backstt1": 4}


def test_index_shared_unit_rows_have_no_leader_seed(index):
    # a [units] row's key is unit:<CODE>; members land there with the
    # list kinds, and nothing is seeded (no leader exists)
    assert index["unitmike"] == {"unit:T21204": 4}
    assert "unit:T21204" not in index.get("kyrkiv1", {})


def test_index_reads_each_member_list_once_per_day(nss, index):
    # the loader below re-reads nothing: member lists are cached per
    # group in the route cache for the index's TTL
    before = len(nss["member_calls"])
    org.membership_index(conf())
    assert len(nss["member_calls"]) == before


def test_index_unknown_groups_are_tolerated(index):
    # skaski1's T99999 does not exist in NSS: no members, no error, and
    # the leader is still seeded
    assert index["skaski1"] == {"skaski1": 5}


def test_index_cached_per_conf_version(nss):
    c = conf()
    c["_source"] = ("/fake/prof_groups.conf", 1000.0)
    first = org._index_cached(c)
    second = org._index_cached(c)
    assert first is second
    # a new mtime (a conf edit) addresses a different key: rebuilt
    c2 = conf()
    c2["_source"] = ("/fake/prof_groups.conf", 2000.0)
    assert org._index_cached(c2) is not first


def test_index_hand_built_conf_is_uncached(nss):
    # a conf without _source (hand-built in tests) computes directly
    assert org._index_cached(conf()) == org.membership_index(conf())


# ---- classification -----------------------------------------------------

def test_classify_paid_member(index):
    r = org.classify("alice", USER_GROUPS_WORLD["alice"], index, conf())
    assert r == {
        "group": "kyrkiv1", "membership": "paid", "dept_code": "T410",
        "school_code": "ELEC", "own_dept": "T410", "extra_groups": [],
        "status": "group"}


def test_classify_group_dept_beats_the_members_own_osasto(index):
    # hannuse2 rides in t40106-everyone but is paid under osasto-t411:
    # the row reports the professor's department; their own osasto rides
    # along as own_dept.
    r = org.classify("hannuse2", USER_GROUPS_WORLD["hannuse2"], index,
                     conf())
    assert r["group"] == "kyrkiv1" and r["membership"] == "everyone"
    assert r["dept_code"] == "T410" and r["own_dept"] == "T411"
    assert r["school_code"] == "ELEC"


def test_classify_leader_stays_in_own_group_not_alkus(index):
    # Bäckström's strongest membership is their own group (leader, 4),
    # not Alku's (staff, 2): the extra membership rides along.
    r = org.classify("backstt1", ["osasto-t412"], index, conf())
    assert r["group"] == "backstt1" and r["membership"] == "leader"
    assert r["extra_groups"] == ["alkut1"]
    assert r["dept_code"] == "T412"


def test_classify_strength_tie_prefers_the_users_own_osasto(index):
    # pat is paid in two units (both strength 3): the group whose
    # department matches their osasto wins.
    groups = ["laitos-t40106", "laitos-t40571"]
    assert org.classify("pat", groups + ["osasto-t410"], index,
                        conf())["group"] == "kyrkiv1"
    assert org.classify("pat", groups + ["osasto-t412"], index,
                        conf())["group"] == "backstt1"


def test_classify_strength_tie_without_osasto_is_deterministic(index):
    groups = ["laitos-t40106", "laitos-t40571"]
    r = org.classify("pat", groups, index, conf())
    # no osasto to prefer with (the unit-code fallback gives T401, which
    # matches neither): lowest leader name, deterministically
    assert r["group"] == "backstt1"
    assert r["extra_groups"] == ["kyrkiv1"]
    assert r["dept_code"] == "T412" and r["own_dept"] == "T401"


def test_classify_shared_unit_member(index):
    # a [units] member: the row is the unit: key, the department is the
    # unit's configured one, and the row id carries the unit code
    r = org.classify("unitmike", USER_GROUPS_WORLD["unitmike"], index,
                     conf())
    assert r == {
        "group": "unit:T21204", "membership": "paid", "dept_code": "T212",
        "school_code": "ENG", "own_dept": "T212", "extra_groups": [],
        "status": "group"}


def test_classify_professor_group_beats_shared_unit_on_a_tie(index):
    # bothpaid is paid in kyrkiv1's unit AND the shared unit (strength
    # tie at 4) with an osasto matching neither: professor groups come
    # before unit: keys, so the professor group owns the row and the
    # shared unit rides along.
    r = org.classify("bothpaid", USER_GROUPS_WORLD["bothpaid"], index,
                     conf())
    assert r["group"] == "kyrkiv1" and r["membership"] == "paid"
    assert r["extra_groups"] == ["unit:T21204"]
    # the user's own osasto is T313, matching neither configured dept
    assert r["dept_code"] == "T410" and r["own_dept"] == "T313"
    # the osasto-match term outranks the professor-before-unit term: an
    # osasto matching the shared unit's department keeps the unit row
    r = org.classify("unitos", USER_GROUPS_WORLD["unitos"], index, conf())
    assert r["group"] == "unit:T21204"
    assert r["extra_groups"] == ["kyrkiv1"]


def test_classify_department_only_user(index):
    r = org.classify("carol", USER_GROUPS_WORLD["carol"], index, conf())
    assert r == {
        "group": None, "membership": None, "dept_code": "T313",
        "school_code": "SCI", "own_dept": "T313", "extra_groups": [],
        "status": "dept"}


def test_classify_unaffiliated_and_unresolved(index):
    r = org.classify("dave", USER_GROUPS_WORLD["dave"], index, conf())
    assert r["status"] == "unaffiliated" and r["dept_code"] is None
    r = org.classify("ghost", None, index, conf())
    assert r["status"] == "unresolved" and r["group"] is None


def test_own_dept_osasto_beats_staff_and_is_case_insensitive():
    assert org.own_dept_of(["osasto-t410", "T412-STAFF"]) == "T410"
    assert org.own_dept_of(["T412-STAFF"]) == "T412"
    # a unit's tNNNXX-staff (six chars) is NOT a department group — it
    # feeds the unit-code fallback instead
    assert org.own_dept_of(["t40106-staff"]) == "T401"
    assert org.own_dept_of(["laitos-t40106", "triton-users"]) == "T401"


def test_own_dept_unit_code_fallback():
    # no osasto / legacy staff group: the first four characters of the
    # LOWEST unit-shaped code are the department
    assert org.own_dept_of(["laitos-t31354"]) == "T313"
    assert org.own_dept_of(["auto-ext-t40571"]) == "T405"
    assert org.own_dept_of(["t21204-everyone"]) == "T212"
    assert org.own_dept_of(["laitos-t31354", "laitos-t30417"]) == "T304"
    # osasto and the legacy staff shape still win over the fallback
    assert org.own_dept_of(["osasto-t410", "laitos-t31354"]) == "T410"
    assert org.own_dept_of(["T412-STAFF", "laitos-t31354"]) == "T412"
    # acronym-style and service groups are not clues: none matches
    assert org.own_dept_of(["triton-users", "auto-student-users",
                            "csm"]) is None


# ---- resolve_users ------------------------------------------------------

def test_resolve_users_coverage(nss, index):
    mapping, coverage = org.resolve_users(
        USER_GROUPS_WORLD, conf(), index=index)
    assert coverage == {
        "users": 13, "in_prof_group": 10, "dept_only": 1,
        "unaffiliated": 1, "unresolved": 1, "failed": 0}
    assert mapping["alice"]["group"] == "kyrkiv1"
    assert mapping["ghost"]["status"] == "unresolved"


def test_resolve_users_partial_failure_discloses_not_fakes(nss, monkeypatch,
                                                           index):
    real = deps.user_groups

    def flaky(user):
        if user == "alice":
            raise deps.DirectoryError("sssd worker timeout")
        return real(user)

    monkeypatch.setattr(deps, "user_groups", flaky)
    mapping, coverage = org.resolve_users(
        USER_GROUPS_WORLD, conf(), index=index)
    assert coverage["failed"] == 1
    assert "alice" not in mapping          # in NO row, never faked
    assert mapping["carol"]["status"] == "dept"


def test_resolve_users_total_failure_is_502(nss, monkeypatch, index):
    def boom(user):
        raise deps.DirectoryError("sssd down")

    monkeypatch.setattr(deps, "user_groups", boom)
    with pytest.raises(deps.DirectoryError):
        org.resolve_users(["alice"], conf(), index=index)


def test_groups_cache_stores_the_raw_list(nss):
    # the RAW groups are cached, not the classification: a conf edit
    # must show on the next request without waiting out a per-user TTL
    calls = []
    real = deps.user_groups

    def counting(user):
        calls.append(user)
        return real(user)

    deps.user_groups = counting
    try:
        org._groups_cached("alice")
        org._groups_cached("alice")
    finally:
        deps.user_groups = real
    assert calls == ["alice"]


# ---- the NSS boundaries (deps.user_groups / deps.group_members) --------

class _FakePwd:
    """A pwd entry: getpwnam returns it, getgrouplist maps to gids."""

    def __init__(self, gids):
        self.pw_gid = gids[0]


def _patch_nss(monkeypatch, accounts):
    """accounts: {name: [gid, ...]}; getpwnam raises KeyError for names
    outside the map. Returns the recorded getpwnam names."""
    looked_up = []

    def getpwnam(username):
        looked_up.append(username)
        if username not in accounts:
            raise KeyError("getpwnam(): name not found: %r" % username)
        return _FakePwd(accounts[username])

    def getgrouplist(username, gid):
        return accounts[username]

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(os, "getgrouplist", getgrouplist)
    return looked_up


def test_user_groups_returns_sorted_unique_names(monkeypatch):
    monkeypatch.setattr(
        deps.grp, "getgrgid",
        lambda g: type("G", (), {"gr_name": {10: "osasto-t410",
                                             11: "laitos-t40106",
                                             12: "osasto-t410"}[g]})())
    _patch_nss(monkeypatch, {"hannuse2": [10, 11, 10, 12]})
    assert deps.user_groups("hannuse2") == ["laitos-t40106", "osasto-t410"]


def test_user_groups_unknown_user_is_none_not_error(monkeypatch):
    looked_up = _patch_nss(monkeypatch, {"alice": [10]})
    # An unknown user is an ANSWER (the unresolved status), not a failure.
    assert deps.user_groups("ghost") is None
    assert looked_up == ["ghost"]


def test_user_groups_oserror_raises_directory_error(monkeypatch):
    _patch_nss(monkeypatch, {"alice": [10]})

    def boom(username, gid):
        raise OSError("NSS status 3, sssd down")

    monkeypatch.setattr(os, "getgrouplist", boom)
    with pytest.raises(deps.DirectoryError, match="sssd down"):
        deps.user_groups("alice")


def test_user_groups_key_is_username_scoped():
    assert cache.user_groups_key("alice") == ("user_groups", "alice")
    assert cache.user_groups_key("bob") != cache.user_groups_key("alice")


def test_group_members_returns_sorted_unique_names(monkeypatch):
    monkeypatch.setattr(
        deps.grp, "getgrnam",
        lambda name: type("G", (), {"gr_mem": ["zoe", "alice", "zoe"]})())
    assert deps.group_members("laitos-t40106") == ["alice", "zoe"]


def test_group_members_unknown_group_is_none_not_error(monkeypatch):
    def missing(name):
        raise KeyError("getgrnam(): name not found: %r" % name)

    monkeypatch.setattr(deps.grp, "getgrnam", missing)
    # An unknown group is an ANSWER (that unit has no laitos group), not
    # a failure.
    assert deps.group_members("laitos-t99999") is None


def test_group_members_oserror_raises_directory_error(monkeypatch):
    def boom(name):
        raise OSError("NSS status 3, sssd down")

    monkeypatch.setattr(deps.grp, "getgrnam", boom)
    with pytest.raises(deps.DirectoryError, match="sssd down"):
        deps.group_members("laitos-t40106")


def test_group_members_key_is_group_scoped():
    assert cache.group_members_key("laitos-t40106") == (
        "group_members", "laitos-t40106")
    assert (cache.group_members_key("laitos-t40106")
            != cache.group_members_key("t40106-staff"))


# ---- the Groups roll-up -------------------------------------------------

def user_row(user, mean=50.0, samples=2, util_h=0.03, vram=11.0,
             running=1):
    """A row shaped like aggregate_users()' output (only the keys
    rollup_groups reads)."""
    return {
        "user": user, "jobs": 1, "running_jobs": running,
        "mean_util": mean, "util_gpu_hours": util_h,
        "_util_sum": mean * samples, "_util_samples": samples,
        "vram_avg": vram, "_vram_sum": vram * samples, "_vram_n": samples,
        "gpu_types": ["h100"],
    }


def test_rollup_group_level_rows(nss, index):
    conf_ = conf()
    mapping, _ = org.resolve_users(["alice", "hannuse2"], conf_, index)
    rows = org.rollup_groups(
        [user_row("alice"), user_row("hannuse2", mean=80.0, util_h=0.05)],
        mapping, [], 120, level="group", conf=conf_)
    by_id = {r["group_id"]: r for r in rows}
    kyrki = by_id["kyrkiv1"]
    assert kyrki["group_name"] == "Kyrki Ville"
    assert kyrki["leader"] == "kyrkiv1"
    assert kyrki["leader_name"] == "Kyrki Ville"
    assert kyrki["unit_codes"] == ["T40106"]
    assert kyrki["dept_code"] == "T410"
    assert kyrki["dept_name"] \
        == "Department of Electrical Engineering and Automation"
    assert kyrki["school_code"] == "ELEC"
    assert kyrki["users"] == 2 and kyrki["jobs"] == 2
    assert kyrki["members"][0]["group"] == "kyrkiv1"
    assert kyrki["members"][0]["membership"] in ("paid", "everyone")
    assert kyrki["members"][0]["own_dept"] in ("T410", "T411")
    # the always-present row exists even when empty
    assert by_id["unaffiliated"]["users"] == 0
    assert by_id["unaffiliated"]["mean_util"] == 0.0
    assert by_id["unaffiliated"]["vram_avg"] is None
    assert by_id["unaffiliated"]["leader"] is None
    assert by_id["unaffiliated"]["unit_codes"] == []


def test_rollup_department_only_naming_per_level(nss, index):
    conf_ = conf()
    mapping, _ = org.resolve_users(["carol"], conf_, index)
    rows = org.rollup_groups([user_row("carol")], mapping, [], 120,
                             level="group", conf=conf_)
    row = rows[0]
    assert row["group_id"] == "dept:T313"
    assert row["group_name"] \
        == "Department of Computer Science, no professor group"
    assert row["leader"] is None and row["unit_codes"] == []
    assert row["school_code"] == "SCI"
    # at department level the same bucket is the plain department
    rows = org.rollup_groups([user_row("carol")], mapping, [], 120,
                             level="department", conf=conf_)
    assert rows[0]["group_name"] == "Department of Computer Science"


def test_rollup_shared_unit_row(nss, index):
    # a [units] member's row: the unit: key is the group id, the name
    # carries the "(shared unit)" suffix, and there is no leader
    conf_ = conf()
    mapping, coverage = org.resolve_users(["unitmike"], conf_, index)
    assert coverage["in_prof_group"] == 1
    rows = org.rollup_groups([user_row("unitmike")], mapping, [], 120,
                             level="group", conf=conf_)
    by_id = {r["group_id"]: r for r in rows}
    unit = by_id["unit:T21204"]
    assert unit["group_name"] == "Mechatronics (shared unit)"
    assert unit["leader"] is None and unit["leader_name"] is None
    assert unit["unit_codes"] == ["T21204"]
    assert unit["dept_code"] == "T212"
    assert unit["dept_name"] == "Department of Mechanical Engineering"
    assert unit["school_code"] == "ENG"
    assert unit["users"] == 1
    member = unit["members"][0]
    assert member["group"] == "unit:T21204" and member["status"] == "group"
    # at department level the member collapses into the unit's department
    rows = org.rollup_groups([user_row("unitmike")], mapping, [], 120,
                             level="department", conf=conf_)
    by_id = {r["group_id"]: r for r in rows}
    assert "unit:T21204" not in by_id
    assert by_id["dept:T212"]["users"] == 1


def test_rollup_department_level_merges_group_and_dept_only(nss, index):
    conf_ = conf()
    # pete is in skaski1's group (dept T313); carol is dept-only T313:
    # one department row at department level, holding both.
    mapping, coverage = org.resolve_users(["pete", "carol"], conf_, index)
    assert coverage["in_prof_group"] == 1 and coverage["dept_only"] == 1
    rows = org.rollup_groups(
        [user_row("pete"), user_row("carol", mean=10.0, util_h=0.01)],
        mapping, [], 120, level="department", conf=conf_)
    by_id = {r["group_id"]: r for r in rows}
    assert set(by_id) == {"dept:T313", "unaffiliated"}
    t313 = by_id["dept:T313"]
    assert t313["users"] == 2 and t313["leader"] is None
    assert t313["members"][0]["group"] == "skaski1"
    assert t313["members"][1]["group"] is None
    # the members keep their own statuses for the drill-down
    assert [m["status"] for m in t313["members"]] == ["group", "dept"]


def test_rollup_orders_by_util_and_counts_low_eff(nss, index):
    conf_ = conf()
    mapping, _ = org.resolve_users(["alice", "carol", "dave"], conf_, index)
    jobs_view = [
        {"user": "alice", "mean_util": 10.0},    # < 30: low-eff
        {"user": "carol", "mean_util": 90.0},
        {"user": "dave", "mean_util": 85.0},
    ]
    rows = org.rollup_groups(
        [user_row("alice", util_h=0.03),
         user_row("carol", mean=92.0, util_h=0.06),
         user_row("dave", mean=85.0, util_h=0.06)],
        mapping, jobs_view, 120, level="group", conf=conf_)
    ids = [r["group_id"] for r in rows]
    assert ids == ["dept:T313", "unaffiliated", "kyrkiv1"]
    by_id = {r["group_id"]: r for r in rows}
    assert by_id["kyrkiv1"]["low_eff_jobs"] == 1
    assert by_id["kyrkiv1"]["top_users"] == [
        {"user": "alice", "util_gpu_hours": 0.03}]


def test_rollup_failed_user_is_in_no_row(nss, monkeypatch, index):
    real = deps.user_groups

    def flaky(user):
        if user == "alice":
            raise deps.DirectoryError("timeout")
        return real(user)

    monkeypatch.setattr(deps, "user_groups", flaky)
    conf_ = conf()
    mapping, coverage = org.resolve_users(["alice", "carol"], conf_, index)
    rows = org.rollup_groups(
        [user_row("alice"), user_row("carol")], mapping, [], 120,
        level="group", conf=conf_)
    assert coverage["failed"] == 1
    assert all("alice" not in [m["user"] for m in r["members"]]
               for r in rows)
    by_id = {r["group_id"]: r for r in rows}
    assert "kyrkiv1" not in by_id       # no members: no row at all
    assert by_id["dept:T313"]["users"] == 1
