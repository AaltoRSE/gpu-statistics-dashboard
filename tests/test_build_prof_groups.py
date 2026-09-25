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


# ---- rule d (DN leaf OU) and [units] ------------------------------------
# A second synthetic world: professors whose DN leaf OU IS the unit (the
# shape AD uses when no managedBy/description names them), plus the
# rejects (shared leaf, no evidence, unit already claimed by rule a) and
# the shared-unit units (>= 2 leaf professors, not …00, not "common").

def _dnprof(user, name, sn, given, dept_attr, leaf):
    return professor(
        user, name, sn, given, dept=dept_attr,
        dn="CN=%s,OU=%s,OU=T499,OU=Staff,OU=users,OU=root,"
           "DC=org,DC=aalto,DC=fi" % (name, leaf))


DNOU_PROFESSORS_DUMP = "\n\n".join([
    _dnprof("dnou1", "Dnou One", "Dnou", "One", "Optics Group", "T49901"),
    _dnprof("dnou2", "Dnou Two", "Dnou", "Two", "Photonics", "T49902"),
    _dnprof("dnou3", "Dnou Three", "Dnou", "Three", "Shared Things",
            "T49903"),
    _dnprof("dnou4", "Dnou Four", "Dnou", "Four", "Shared Things",
            "T49903"),
    _dnprof("dnou5", "Dnou Five", "Dnou", "Five", "Nothing", "T49905"),
    _dnprof("dnou7", "Dnou Seven", "Dnou", "Seven", "Cold fusion",
            "T40555"),
    # rule b's match ("Eight Nina group") AND a rule-d-claimable leaf:
    # rule b must win
    professor("rulebw1", "Eight Nina", "Eight", "Nina",
              dept="Eight Nina group",
              dn="CN=Eight Nina,OU=T49909,OU=T499,OU=Staff,OU=users,"
                 "DC=org,DC=aalto,DC=fi"),
    # accepted via the unit's -staff NSS list
    _dnprof("dnou8", "Dnou Eight", "Dnou", "Eight", "Photonics", "T49904"),
    # two-professor leaves that must become (or stay out of) [units]
    _dnprof("dnou9", "Dnou Nine", "Dnou", "Nine", "Costly", "T49900"),
    _dnprof("dnou10", "Dnou Ten", "Dnou", "Ten", "Costly", "T49900"),
    _dnprof("dnou11", "Dnou Eleven", "Dnou", "Eleven", "Common", "T49910"),
    _dnprof("dnou12", "Dnou Twelve", "Dnou", "Twelve", "Common", "T49910"),
    # claims T40555 through rule a (managedBy), blocking dnou7's leaf
    professor("mallik1", "Mallik Virtanen", "Mallik", "Virtanen",
              dept="Department of Information and Communications "
                   "Engineering",
              company="School of Electrical Engineering"),
])

DNOU_UNIT_DUMP = "\n\n".join([
    unit("laitos-t49901", description="Optics Group"),
    unit("laitos-t49902", description="Laser Lab"),
    unit("laitos-t49903", description="Shared Things"),
    unit("laitos-t49904", description="Beamline"),
    unit("laitos-t49905", description="Unrelated Unit"),
    unit("laitos-t40555", description="Cold fusion project",
         managed_by="CN=Mallik Virtanen,OU=Users,DC=example,DC=org"),
    unit("laitos-t49900", description="Costly Centre"),
    unit("laitos-t49909", description="Eight Nina group"),
    unit("laitos-t49910", description="Common Facilities"),
])

DNOU_NSS_GROUPS = {
    "dnou1": ["osasto-t499"],
    "dnou2": ["osasto-t499"],
    "dnou3": ["osasto-t499"],
    "dnou4": ["osasto-t499"],
    "dnou5": ["osasto-t499"],
    "dnou7": ["osasto-t412"],
    "rulebw1": ["laitos-t49909", "osasto-t499"],
    "dnou8": ["osasto-t499"],
    "dnou9": ["osasto-t499"],
    "dnou10": ["osasto-t499"],
    "dnou11": ["osasto-t499"],
    "dnou12": ["osasto-t499"],
    "mallik1": ["laitos-t40555", "osasto-t412"],
}

DNOU_GROUP_MEMBERS = {
    "laitos-t49902": ["dnou2"],
    "t49904-staff": ["dnou8"],
}


@pytest.fixture()
def built_dn(monkeypatch):
    monkeypatch.setattr(deps, "user_groups",
                        lambda user: DNOU_NSS_GROUPS.get(user))
    monkeypatch.setattr(deps, "group_members",
                        lambda name: DNOU_GROUP_MEMBERS.get(name))
    lines, report = bpg.build(DNOU_PROFESSORS_DUMP, DNOU_UNIT_DUMP)
    return "\n".join(lines), report


def test_rule_d_accepts_on_department_match(built_dn):
    conf, report = built_dn
    # T49901's description names nobody; the professor's AD department
    # equals the description and their DN leaf OU is the unit
    assert "dnou1 = Dnou One | T499 | T49901" in conf
    assert "rule-d claims" in "\n".join(report)
    assert "dnou1" in "\n".join(report)


def test_rule_d_accepts_on_nss_membership(built_dn):
    conf, _ = built_dn
    # no department match ("Photonics" vs "Laser Lab"): the laitos list
    assert "dnou2 = Dnou Two | T499 | T49902" in conf
    # and the -staff list
    assert "dnou8 = Dnou Eight | T499 | T49904" in conf


def test_rule_d_rejects_shared_leaf_no_evidence_and_claimed(built_dn):
    conf, _ = built_dn
    # T49903 has TWO leaf professors: no single leader is derivable and
    # nobody claims it, even though both departments match the name
    assert "# dnou3 = Dnou Three | T499 | (no own unit found" in conf
    assert "# dnou4 = Dnou Four | T499 | (no own unit found" in conf
    # department != description and no laitos/-staff membership
    assert "# dnou5 = Dnou Five | T499 | (no own unit found" in conf
    # T40555 was claimed through rule a already
    assert "mallik1 = Mallik Virtanen | T412 | T40555" in conf
    assert "# dnou7 = Dnou Seven | T412 | (no own unit found" in conf


def test_rules_a_and_b_win_over_rule_d(built_dn):
    conf, report = built_dn
    # rule b's description match owns the row even though the DN leaf OU
    # claim would succeed too — the second pass only sees unit-less
    # professors, and the report stays silent about resolved ones
    assert "rulebw1 = Eight Nina | T499 | T49909" in conf
    assert "rulebw1" not in "\n".join(report)


def test_shared_units_go_to_the_units_section(built_dn):
    conf, _ = built_dn
    assert "T49903 = Shared Things | T499" in conf
    # cost centres (…00) and "common" units are never shared-unit rows
    assert "T49900" not in conf
    assert "T49910" not in conf


def test_report_sections_for_rule_d_and_shared_units(built_dn):
    _, report = built_dn
    text = "\n".join(report)
    assert "rule-d claims" in text
    for user in ("dnou1", "dnou2", "dnou8"):
        assert user in text, user
    assert "shared units" in text
    assert "T49903" in text


def test_main_preserves_hand_lines(tmp_path, monkeypatch, capsys):
    dump_dir = tmp_path / "ad_dump"
    dump_dir.mkdir()
    (dump_dir / "ad_professors.txt").write_text(PROFESSORS_DUMP,
                                                encoding="utf-8")
    (dump_dir / "ad_unit_groups.txt").write_text(UNIT_GROUPS_DUMP,
                                                 encoding="utf-8")
    monkeypatch.setattr(deps, "user_groups",
                        lambda user: NSS_GROUPS.get(user))
    monkeypatch.setattr(deps, "group_members", lambda name: None)
    # conftest's autouse fixture writes the runtime TEST_PROF_GROUPS to
    # tmp_path/"prof_groups.conf" — the built file needs its own name so
    # preservation never mistakes that for a previous build.
    out = tmp_path / "built.conf"
    bpg.main(["--dump-dir", str(dump_dir), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    # hand edits: a non-professor leader, a hand-filled unit, a resolved
    # no-unit professor given codes, and a line the build itself resolves.
    # Splice into the build's own (always-present, empty) [units] header —
    # a second [units] section would make the preservation parser fail.
    text = text.replace(
        "\n[units]\n\n[groups]\n",
        "\n[units]\nT39999 = Hand Unit | T313\n\n[groups]\n"
        "hellsa1 = Hellas Arto | T313 | T313AD\n"
        "ghostp1 = Ghost Professor | T313 | T31398\n")
    text = text.replace("kyrkiv1 = Kyrki Ville | T410 | T40106",
                        "kyrkiv1 = Hand Edited | T410 | T40106")
    out.write_text(text, encoding="utf-8")
    bpg.main(["--dump-dir", str(dump_dir), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    # the hand lines the new build cannot produce survive, sorted in
    assert "hellsa1 = Hellas Arto | T313 | T313AD" in text
    assert "ghostp1 = Ghost Professor | T313 | T31398" in text
    assert "# ghostp1" not in text      # the placeholder is dropped
    assert "T39999 = Hand Unit | T313" in text
    # a key the build resolves: the generated line wins
    assert "kyrkiv1 = Kyrki Ville | T410 | T40106" in text
    assert "Hand Edited" not in text
    # and the report lists every kept line
    assert "kept hand lines" in capsys.readouterr().out


def test_main_preserves_nothing_without_an_existing_conf(
        tmp_path, monkeypatch, capsys):
    dump_dir = tmp_path / "ad_dump"
    dump_dir.mkdir()
    (dump_dir / "ad_professors.txt").write_text(PROFESSORS_DUMP,
                                                encoding="utf-8")
    (dump_dir / "ad_unit_groups.txt").write_text(UNIT_GROUPS_DUMP,
                                                 encoding="utf-8")
    monkeypatch.setattr(deps, "user_groups",
                        lambda user: NSS_GROUPS.get(user))
    monkeypatch.setattr(deps, "group_members", lambda name: None)
    out = tmp_path / "built.conf"  # not conftest's prof_groups.conf
    assert bpg.main(["--dump-dir", str(dump_dir), "--out", str(out)]) == 0
    assert "kept hand lines" not in capsys.readouterr().out
    assert "hellsa1" not in out.read_text(encoding="utf-8")


def test_dn_leaf_unit_shapes():
    dn = "CN=Ala-Nissilä Tapio,OU=T30402,OU=T304,OU=Staff,OU=users"
    assert bpg.dn_leaf_unit(dn) == "T30402"
    # a 4-char leaf OU is a department, not a unit
    assert bpg.dn_leaf_unit("CN=X,OU=Users,OU=T410,DC=x") is None
    assert bpg.dn_leaf_unit("CN=X,OU=T304,DC=x") is None
    assert bpg.dn_leaf_unit("") is None
    assert bpg.dn_leaf_unit(None) is None


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


def test_word_boundary_surnames_do_not_substring_match():
    # 'Li' must claim 'Li Wei group' but never 'Lindqvist Johan group'
    # or 'Salmelin' — short surnames substring-matched dozens of units in
    # the real AD data until this was a whole-word rule.
    units = bpg.load_units(bpg.parse_dump("\n\n".join([
        unit("laitos-t31301", description="Li Wei group"),
        unit("laitos-t31302", description="Lindqvist Johan group"),
        unit("laitos-t31303", description="Salmelin Mikko group")])))
    li = {"sn": "Li", "given": "Wei", "dn": "", "name": "Li Wei"}
    matched, how = bpg.own_units(li, units, [], {"li": 1})
    assert matched == ["T31301"] and how == "description"


def test_hyphenated_compound_is_one_surname():
    # 'Laurila' must not claim 'Ala-Laurila Petri group' — in the real
    # AD data the hyphen is a word boundary to every regex but one name
    # to the org chart, so the compound's owner and the tail-surname
    # professor both claimed T31425 until '-' joined the boundary class.
    units = bpg.load_units(bpg.parse_dump("\n\n".join([
        unit("laitos-t31425", description="Ala-Laurila Petri group")])))
    compound = {"sn": "Ala-Laurila", "given": "Petri", "dn": "",
                "name": "Ala-Laurila Petri"}
    tail = {"sn": "Laurila", "given": "Timo", "dn": "",
            "name": "Laurila Timo"}
    matched, how = bpg.own_units(compound, units, [], {"ala-laurila": 1})
    assert matched == ["T31425"] and how == "description"
    matched, how = bpg.own_units(tail, units, [], {"laurila": 1})
    assert matched == [] and how == "none"


def test_department_falls_back_to_the_dn_ou(monkeypatch):
    # A professor without a Triton account (not in this host's NSS)
    # still carries their department in their DN: OU=<unit>,OU=<dept>.
    monkeypatch.setattr(deps, "user_groups", lambda user: None)
    profs_text = professor(
        "nssless", "Noacct Professor", "Noacct", "Pat",
        dept="Department of Testing",
        dn="CN=Noacct Professor,OU=T49999,OU=T499,OU=Staff,OU=users,"
           "OU=root,DC=org,DC=aalto,DC=fi")
    lines, _ = bpg.build(profs_text,
                         unit("laitos-t49999", description="Noacct group"))
    conf = "\n".join(lines)
    assert "nssless = Noacct Professor | T499 | T49999" in conf
    assert "T499 = Department of Testing | T4" in conf


def test_school_keyword_order_elec_before_eng():
    # "Electrical Engineering" contains "Engineering" too: Electrical wins
    prof = {"company": "School of Electrical Engineering", "division": ""}
    assert bpg.school_of(prof, "T410") == ("ELEC", "School of Electrical "
                                           "Engineering")
    prof = {"company": "", "division": "School of Chemical Engineering"}
    assert bpg.school_of(prof, "T100")[0] == "CHEM"
    # real AD company values are OU codes, so the prefix falls through
    # the static school table (the old org_units.conf school facts)
    prof = {"company": "T213", "division": ""}
    assert bpg.school_of(prof, "T213") == ("ENG", "School of Engineering")
    prof = {"company": "E706", "division": ""}
    assert bpg.school_of(prof, "E706") == ("BIZ", "School of Business")
    prof = {"company": "A803", "division": ""}
    assert bpg.school_of(prof, "A803") == ("ARTS", "School of Arts, Design "
                                           "and Architecture")


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
    out = tmp_path / "built.conf"
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
