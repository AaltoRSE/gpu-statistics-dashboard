"""Org-map loader and classification tests (domain/org.py).

The real org_units.conf is committed, so the loader's checks against it
run as unit tests; synthetic files in tmp_path cover the format corners.
The NSS boundary (deps.user_groups) is exercised with patched pwd/os —
never against the real directory.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pwd  # noqa: E402

import pytest  # noqa: E402

import cache  # noqa: E402
import deps  # noqa: E402
import domain.org as org  # noqa: E402

REAL_FILE = org.DEFAULT_FILE


@pytest.fixture(autouse=True)
def _fresh_org_cache():
    org.reset_org_cache()
    yield
    org.reset_org_cache()


@pytest.fixture()
def org_file(tmp_path):
    def write(content, name="org.conf"):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        return str(f)
    return write


def test_load_org_map_parses_sections(org_file):
    path = org_file(
        "[schools]\n"
        "T4 = ELEC | School of Electrical Engineering\n"
        "\n"
        "[departments]\n"
        "T410 = Electrical Engineering and Automation\n"
        "\n"
        "[units]\n"
        "T40106 = Kyrki Ville group | T410\n")
    m = org.load_org_map(path)
    assert m["schools"]["T4"] == {
        "short": "ELEC", "full": "School of Electrical Engineering"}
    assert m["departments"]["T410"] == "Electrical Engineering and Automation"
    assert m["units"]["T40106"] == {"name": "Kyrki Ville group", "dept": "T410"}


def test_load_org_map_keys_are_case_insensitive(org_file):
    path = org_file(
        "[units]\n"
        "t40106 = Kyrki Ville group | t410\n")
    m = org.load_org_map(path)
    assert "T40106" in m["units"]
    assert m["units"]["T40106"]["dept"] == "T410"


def test_load_org_map_empty_name_and_missing_dept(org_file):
    path = org_file(
        "[units]\n"
        "T31301 =  | T313\n"      # empty name: the UI shows the raw code
        "T99999 = No dept recorded\n")  # no pipe at all
    m = org.load_org_map(path)
    assert m["units"]["T31301"]["name"] == ""
    assert m["units"]["T31301"]["dept"] == "T313"
    assert m["units"]["T99999"]["name"] == "No dept recorded"
    assert m["units"]["T99999"]["dept"] == ""


def test_load_org_map_name_may_contain_a_pipe(org_file):
    path = org_file("[units]\nT313AA = A | B | T313\n")
    m = org.load_org_map(path)
    assert m["units"]["T313AA"]["name"] == "A | B"
    assert m["units"]["T313AA"]["dept"] == "T313"


def test_load_org_map_missing_sections_are_empty(org_file):
    path = org_file("[schools]\nT3 = SCI | School of Science\n")
    m = org.load_org_map(path)
    assert m["departments"] == {}
    assert m["units"] == {}


def test_load_org_map_reloads_when_mtime_changes(org_file):
    path = org_file("[departments]\nT410 = Old name\n")
    assert org.load_org_map(path)["departments"]["T410"] == "Old name"
    # mtime granularity can be coarser than the edit; force a fresh stamp.
    org_file("[departments]\nT410 = New name\n")
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    assert org.load_org_map(path)["departments"]["T410"] == "New name"


def test_load_org_file_env_override(org_file, monkeypatch):
    path = org_file("[departments]\nT410 = From override\n")
    monkeypatch.setenv("ORG_UNITS_FILE", path)
    assert org.load_org_map()["departments"]["T410"] == "From override"
    assert org.org_units_path() == path


def test_school_longest_prefix_wins(org_file):
    path = org_file(
        "[schools]\n"
        "T3 = SCI | School of Science\n"
        "T31 = SCI2 | A longer match\n"
        "[departments]\n"
        "T313 = Computer Science\n")
    m = org.load_org_map(path)
    # T31 matches the dept code more specifically than T3
    assert org.school_for_dept("T313", m) == "SCI2"
    assert org.school_for_dept("T313", m) is not None


def test_school_no_match_is_other(org_file):
    path = org_file("[schools]\nT3 = SCI | School of Science\n")
    m = org.load_org_map(path)
    assert org.school_for_dept("T900", m) == "Other"
    assert org.school_for_dept(None, m) is None


def test_unit_dept_falls_back_to_prefix(org_file):
    path = org_file("[units]\nT40106 = Kyrki Ville group | T410\n"
                    "T40299 = Unmapped parent | \n")
    m = org.load_org_map(path)
    assert org.unit_dept("T40106", m) == "T410"
    # a unit with no DEPT falls back to its own TNNN prefix
    assert org.unit_dept("T40299", m) == "T402"


# ---- checks against the committed org_units.conf ---------------------

def test_real_file_unit_cases():
    m = org.load_org_map(REAL_FILE)
    assert org.unit_name("T40106", m) == "Kyrki Ville group"
    assert org.unit_dept("T40106", m) == "T410"
    assert org.school_for_dept("T410", m) == "ELEC"
    assert org.unit_name("T313AA", m) == "Aledavood Talayeh group"
    assert org.unit_dept("T313AA", m) == "T313"
    assert org.school_for_dept("T313", m) == "SCI"


def test_real_file_unit_under_multiple_staff_parents():
    # T40714 sits under both T407 (its own prefix, legacy) and T412 (a
    # current department): the file's DEPT must be the current one.
    m = org.load_org_map(REAL_FILE)
    assert org.unit_dept("T40714", m) == "T412"
    # T30652: under T306 (its own prefix, legacy) and T313 (current).
    assert org.unit_dept("T30652", m) == "T313"


def test_real_file_departments_and_schools():
    m = org.load_org_map(REAL_FILE)
    assert org.dept_name("T410", m) == "Electrical Engineering and Automation"
    assert org.dept_name("T300", m) == "School services, SCI"
    schools = m["schools"]
    assert schools["T3"]["short"] == "SCI"
    assert schools["T5"]["short"] == "Other"


# ---- the NSS boundary (deps.user_groups) ------------------------------

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
    # An unknown user is an ANSWER (the Unresolved row), not a failure.
    assert deps.user_groups("ghost") is None
    assert looked_up == ["ghost"]


def test_user_groups_oserror_raises_directory_error(monkeypatch):
    _patch_nss(monkeypatch, {"alice": [10]})

    def boom(username, gid):
        raise OSError("NSS status 3, sssd down")

    monkeypatch.setattr(os, "getgrouplist", boom)
    with pytest.raises(deps.DirectoryError, match="sssd down"):
        deps.user_groups("alice")


def test_user_org_key_is_username_scoped():
    assert cache.user_org_key("alice") == ("user_org", "alice")
    assert cache.user_org_key("bob") != cache.user_org_key("alice")


def test_ttl_cache_set_then_peek():
    c = cache.TtlCache()
    assert c.peek("k") == (False, None)
    c.set("k", 60, {"v": 1})
    hit, value = c.peek("k")
    assert hit and value == {"v": 1}
    # a fresh peek after the TTL passes misses (monotonic, not wall clock)
    real = cache.time.monotonic
    cache.time.monotonic = lambda: real() + 120
    try:
        assert c.peek("k") == (False, None)
    finally:
        cache.time.monotonic = real
