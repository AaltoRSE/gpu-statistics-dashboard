"""Org-map loader and classification tests (domain/org.py).

The real org_units.conf is committed, so the loader's checks against it
run as unit tests; synthetic files in tmp_path cover the format corners.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

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
