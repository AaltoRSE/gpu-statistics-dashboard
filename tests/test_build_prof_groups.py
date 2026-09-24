"""tools/build_prof_groups.py tests — synthetic fixtures only.

The builder turns two `net ads search` dumps into prof_groups.conf. The
dumps carry real staff names, so they are never committed; these tests
feed synthetic AD text (the observed Kyrki/Bäckström shapes, plus the
collisions the rules must survive) and a fake NSS directory, never the
real directory or the real dumps.
"""

import configparser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import deps  # noqa: E402
import tools.build_prof_groups as bpg  # noqa: E402


def professor(user, name, sn, given, title="Professor", dept="",
              company="", division="", uac="512", dn="", extra=""):
    lines = [
        "dn: %s" % (dn or ("CN=%s,OU=Users,DC=example,DC=org" % name)),
        "sAMAccountName: %s" % user,
        "displayName: %s" % name,
        "sn: %s" % sn,
        "givenName: %s" % given,
        "title: %s" % title,
    ]
    if dept:
        lines.append("department: %s" % dept)
    if company:
        lines.append("company: %s" % company)
    if division:
        lines.append("division: %s" % division)
    lines.append("userAccountControl: %s" % uac)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def unit(cn, description="", managed_by="", dn=None):
    lines = ["dn: %s" % (dn or ("CN=%s,OU=Groups,DC=example,DC=org" % cn)),
             "cn: %s" % cn]
    if description:
        lines.append("description: %s" % description)
    if managed_by:
        lines.append("managedBy: %s" % managed_by)
    return "\n".join(lines)


# The synthetic world: two verified professors (Kyrki, Bäckström), a
# surname collision (Kaski Sami / Kaski Petteri), a managedBy-owned unit
# with an unhelpful description, a two-unit professor (Rousu), a
# no-unit professor and the skips (emeritus, disabled).
KYRKI_DN = r"CN=Kyrki Ville\, OU=Users,OU=T410,DC=example,DC=org"

PROFESSORS_DUMP = "\n\n".join([
    professor("kyrkiv1", "Kyrki Ville", "Kyrki", "Ville",
              dept="Department of Electrical Engineering and Automation",
              company="School of Electrical Engineering", dn=KYRKI_DN),
    professor("backstt1", "Bäckström Tom", "Bäckström", "Tom",
              dept="Department of Information and Communications "
                   "Engineering",
              company="School of Electrical Engineering"),
    professor("kaskis1", "Kaski Sami", "Kaski", "Sami",
              dept="Department of Computer Science",
              company="School of Science"),
    professor("kaskip1", "Kaski Petteri", "Kaski", "Petteri",
              dept="Department of Computer Science",
              company="School of Science"),
    professor("mallik1", "Mallik Virtanen", "Mallik", "Virtanen",
              dept="Department of Information and Communications "
                   "Engineering",
              company="School of Electrical Engineering"),
    professor("rousur1", "Rousu Juho", "Rousu", "Juho",
              dept="Department of Computer Science",
              company="School of Science"),
    professor("ghostp1", "Ghost Professor", "Ghost", "Pat",
              dept="Department of Computer Science",
              company="School of Science"),
    professor("emeritp1", "Old Emeritus", "Emerit", "Ollie",
              title="Emeritus Professor"),
    professor("disabp1", "Gone Disabled", "Disabled", "Di", uac="514"),
])

UNIT_GROUPS_DUMP = "\n\n".join([
    unit("laitos-t40106", description="Kyrki Ville group",
         managed_by=KYRKI_DN),
    unit("t40106-staff"),
    unit("laitos-t41000", description="EEA common"),
    unit("laitos-t40571", description="Bäckström group"),
    unit("t40571-staff"),
    # the surname collision: two Kaski units, given name disambiguates
    unit("laitos-t31301", description="Kaski Sami group"),
    unit("laitos-t31302", description="Kaski Petteri group"),
    # rule a without a usable description
    unit("laitos-t40555", description="Cold fusion project",
         managed_by="CN=Mallik Virtanen,OU=Users,DC=example,DC=org"),
    # rule b across scopes: only T31353 is a candidate; T30614 found on
    # the all-units pass (the legacy spelling of the same professor)
    unit("laitos-t31353", description="Rousu Juho group"),
    unit("t30614-staff", description="J. Rousu Professor"),
    # a lecturer-led group nobody in the professor list claims
    unit("laitos-t31399", description="Salo Marko group"),
])

# The professors' NSS groups (candidates + osasto). kyrkiv1's own laitos
# is the t41000 cost centre — not their unit.
NSS_GROUPS = {
    "kyrkiv1": ["laitos-t41000", "osasto-t410", "t40100-staff",
                "t41000-staff"],
    "backstt1": ["laitos-t41200", "osasto-t412", "t40521-staff",
                 "t40500-staff", "t41200-staff"],
    "kaskis1": ["laitos-t31301", "osasto-t313"],
    "kaskip1": ["laitos-t31302", "osasto-t313"],
    "mallik1": ["laitos-t40555", "osasto-t412"],
    "rousur1": ["laitos-t31353", "osasto-t313"],
    "ghostp1": ["osasto-t313"],
    "emeritp1": ["laitos-t31301", "osasto-t313"],
    "disabp1": ["laitos-t31301", "osasto-t313"],
}


@pytest.fixture()
def built(monkeypatch):
    monkeypatch.setattr(deps, "user_groups",
                        lambda user: NSS_GROUPS.get(user))
    lines, report = bpg.build(PROFESSORS_DUMP, UNIT_GROUPS_DUMP)
    return "\n".join(lines), report


def test_rules_resolve_the_verified_professors(built):
    conf, _ = built
    assert "kyrkiv1 = Kyrki Ville | T410 | T40106" in conf
    # diacritics stripped on the description match: Bäckström -> backstrom
    assert "backstt1 = Bäckström Tom | T412 | T40571" in conf
    # the cost-centre laitos-t41000 must NOT have become Kyrki's unit
    assert "T41000" not in conf
    assert "T40100" not in conf


def test_surname_collision_needs_the_given_name(built):
    conf, _ = built
    # Kaski Sami and Kaski Petteri must not swap (or merge) their units
    assert "kaskis1 = Kaski Sami | T313 | T31301" in conf
    assert "kaskip1 = Kaski Petteri | T313 | T31302" in conf


def test_managed_by_beats_description(built):
    conf, _ = built
    # T40555's description ("Cold fusion project") names nobody; the
    # managedBy DN is the only evidence, and it wins.
    assert "mallik1 = Mallik Virtanen | T412 | T40555" in conf


def test_two_units_join_one_group(built):
    conf, _ = built
    # Rousu: the candidate T31353 plus the legacy T30614 found on the
    # all-units pass — one group, two codes, candidates first.
    assert "rousur1 = Rousu Juho | T313 | T31353 T30614" in conf


def test_no_unit_professor_is_commented_and_reported(built):
    conf, report = built
    report_text = "\n".join(report)
    assert "# ghostp1 = Ghost Professor | T313 | (no own unit found" in conf
    assert "no own unit" in report_text
    assert "ghostp1" in report_text
    # emeritus and disabled professors are skipped entirely
    assert "emeritp1" not in conf
    assert "disabp1" not in conf
    assert "emeritp1" not in report_text


def test_report_lists_lecturer_led_units_and_multi_claims(built):
    _, report = built
    report_text = "\n".join(report)
    assert "Salo Marko group" in report_text   # led by nobody in the list
    assert "lecturer-led" in report_text
    # no unit is claimed twice in this world
    assert "claimed by two" not in report_text


def test_department_name_and_school_derivation(built):
    conf, _ = built
    # the departments section carries the majority AD department text and
    # the school key; the schools section the keyword map result
    lines = conf.splitlines()
    t410 = next(line for line in lines if line.startswith("T410 ="))
    assert t410 == ("T410 = Department of Electrical Engineering and "
                    "Automation | T4")
    t412 = next(line for line in lines if line.startswith("T412 ="))
    assert t412 == ("T412 = Department of Information and Communications "
                    "Engineering | T4")
    t4 = next(line for line in lines if line.startswith("T4 ="))
    assert t4 == "T4 = ELEC | School of Electrical Engineering"
    t3 = next(line for line in lines if line.startswith("T3 ="))
    assert t3 == "T3 = SCI | School of Science"


def test_conf_is_parseable_ini(built):
    conf, _ = built
    cp = configparser.RawConfigParser(delimiters=("=",))
    cp.optionxform = str
    cp.read_string(conf)
    assert cp.get("groups", "kyrkiv1") == "Kyrki Ville | T410 | T40106"
    # the commented no-unit line must not parse as a key
    assert not cp.has_option("groups", "# ghostp1")
    assert not cp.has_option("groups", "#ghostp1")


# ---- dump parsing corners -----------------------------------------------

def test_parse_dump_continuation_and_multi_value():
    text = ("Got 2 replies\n"
            "\n"
            "dn: CN=A,DC=x\n"
            "sAMAccountName: a1\n"
            "title: Professor of\n"
            " Computer Science\n"
            "title: Docent\n"
            "\n"
            "----------\n"
            "dn: CN=B,DC=x\n"
            "sAMAccountName:: YjE=\n")
    records = bpg.parse_dump(text)
    assert records[0]["samaccountname"] == ["a1"]
    # the folded line continues the first title; the repeat is multi-valued
    assert records[0]["title"] == ["Professor ofComputer Science", "Docent"]
    assert records[1]["samaccountname"] == ["b1"]  # base64 spelling


def test_parse_dump_separator_lines_are_boundaries():
    text = ("sAMAccountName: a1\n"
            "--------------------\n"
            "sAMAccountName: b1\n")
    records = bpg.parse_dump(text)
    assert [r["samaccountname"][0] for r in records] == ["a1", "b1"]


def test_unit_group_regex_shapes():
    for name, code in [("laitos-t40106", "T40106"),
                       ("t40106-staff", "T40106"),
                       ("t40106-everyone", "T40106"),
                       ("t40106", "T40106"),
                       ("laitos-u315ab", "U315AB")]:
        m = bpg.UNIT_GROUP_RE.match(name)
        assert m and m.group(1).upper() == code, name
    for name in ["osasto-t410", "laitos-t4010", "laitos-t410",
                 "triton-users", "t302-prof"]:
        assert not bpg.UNIT_GROUP_RE.match(name), name


def test_dn_cn_fallback_when_dump_has_no_dn():
    # a unit whose managedBy DN's CN equals the display name matches even
    # when the professor dump carries no distinguishedName attribute
    units = bpg.load_units(bpg.parse_dump(
        unit("laitos-t40555", managed_by="CN=Mallik Virtanen,OU=U,DC=x")))
    prof = {"dn": "", "name": "Mallik Virtanen", "sn": "Mallik",
            "given": "Virtanen"}
    assert bpg._managed_by_matches(units["T40555"], prof)
    other = dict(prof, name="Someone Else")
    assert not bpg._managed_by_matches(units["T40555"], other)


def test_school_keyword_order_elec_before_eng():
    # "Electrical Engineering" contains "Engineering" too: Electrical wins
    prof = {"company": "School of Electrical Engineering", "division": ""}
    assert bpg.school_of(prof, "T410") == ("ELEC", "School of Electrical "
                                           "Engineering")
    prof = {"company": "", "division": "School of Chemical Engineering"}
    assert bpg.school_of(prof, "T100")[0] == "CHEM"
    prof = {"company": "", "division": ""}
    assert bpg.school_of(prof, "T299") == ("T2", "")  # prefix fallback


def test_nss_outage_raises(monkeypatch):
    def boom(user):
        raise deps.DirectoryError("sssd down")

    monkeypatch.setattr(deps, "user_groups", boom)
    with pytest.raises(deps.DirectoryError):
        bpg.build(PROFESSORS_DUMP, UNIT_GROUPS_DUMP)


def test_main_writes_conf_and_report(tmp_path, monkeypatch, capsys):
    dump_dir = tmp_path / "ad_dump"
    dump_dir.mkdir()
    (dump_dir / "ad_professors.txt").write_text(PROFESSORS_DUMP,
                                                encoding="utf-8")
    (dump_dir / "ad_unit_groups.txt").write_text(UNIT_GROUPS_DUMP,
                                                 encoding="utf-8")
    monkeypatch.setattr(deps, "user_groups",
                        lambda user: NSS_GROUPS.get(user))
    out = tmp_path / "prof_groups.conf"
    rc = bpg.main(["--dump-dir", str(dump_dir), "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "[groups]" in text
    assert "kyrkiv1 = Kyrki Ville | T410 | T40106" in text
    assert "no own unit" in capsys.readouterr().out
    # missing dump: a clear error, not a traceback
    (dump_dir / "ad_professors.txt").unlink()
    with pytest.raises(SystemExit, match="missing dump"):
        bpg.main(["--dump-dir", str(dump_dir), "--out", str(out)])
