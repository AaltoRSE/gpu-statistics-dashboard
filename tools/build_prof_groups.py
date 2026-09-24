#!/usr/bin/env python
"""Build prof_groups.conf from two AD dumps: a professor list and their units.

A Groups-tab group is one professor's research group. The professor's own
unit is NOT their laitos-t* group (that is a cost centre such as "EEA
common") and not every unit they belong to — it is the unit the AD data
ties to them personally:

  a. the unit whose AD group has managedBy = the professor, else
  b. the unit whose description contains the professor's surname
     (diacritics stripped, case-folded); when several professors share a
     surname, the given name must appear in the description too, else
  d. the professor's DN leaf OU (CN=Ala-Nissilä Tapio,OU=T30402,...):
     claimed in a second pass over the professors still unit-less, only
     when the unit is unclaimed by a/b, exactly ONE active professor has
     it as their leaf OU, and AD ties the professor to it another way —
     their `department` attribute equals one of the unit's descriptions,
     or they sit in the unit's laitos-/-staff NSS lists ("dn-ou" in the
     report, which reviews every such claim).

Units no rule resolves are written as commented [groups] lines. A unit
that stays unclaimed but has TWO OR MORE active professors as their DN
leaf OU is a shared unit: written to the [units] section (CODE =
description | department, the department from the DNs) — never codes
ending in 00 (cost centres) or descriptions containing "common". At
runtime a [units] row is the leaderless unit:<CODE> shared-unit row.

The members of every unit are read at RUNTIME from NSS (laitos-t<code>,
t<code>-staff, t<code>-everyone, auto-ext-t<code> for external
visitors); this builder only writes which codes
belong to which professor, each department's name (majority of the AD
`department` attribute among its professors) and school (keyword map over
the professors' AD `company`/`division`).

Rebuilding keeps hand edits: uncommented [groups]/[units] lines in an
existing --out file survive when the new build produces no codes for
that key (or does not know the key at all — non-professors such as a
lecturer-led group's leader), and every kept line is listed in the
report.

Inputs (never committed — staff names): the output of, on the AD server,

  net ads search '(&(objectCategory=person)(objectClass=user)(title=*rofessor*))' \
      sAMAccountName displayName sn givenName title department company division \
      physicalDeliveryOfficeName userAccountControl distinguishedName \
      > ad_professors.txt
  net ads search '(&(objectClass=group)(|(cn=laitos-*)(cn=t*-staff)))' \
      cn description managedBy info > ad_unit_groups.txt

copied to ~/ad_dump/ on the dashboard host (this host must also resolve
NSS, for the professors' own osasto-*/unit groups). If the professor
query comes back empty, the title may live elsewhere — try

  net ads search '(sAMAccountName=kyrkiv1)'

and pick the attribute that holds the title.

Run:  .venv/bin/python tools/build_prof_groups.py [--dump-dir ~/ad_dump]
      [--out prof_groups.conf]

The report it prints (professors with no unit, rule-d claims, shared
units, lecturer-led "X group" units, units claimed by two professors,
hand lines kept from a previous build) is meant to be reviewed before
the conf is committed.
"""

import argparse
import base64
import configparser
import os
import re
import sys
import unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deps  # noqa: E402  (the NSS boundary; the builder runs where NSS works)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFESSORS_DUMP = "ad_professors.txt"
UNIT_GROUPS_DUMP = "ad_unit_groups.txt"

# A unit-shaped NSS group: laitos-tNNNXX, tNNNXX, tNNNXX-staff/-everyone.
# The letter prefix is kept so ARTS/BIZ codes work too; dept (osasto-tNNN)
# and legacy 4-digit laitos-tNNNN groups never match.
UNIT_GROUP_RE = re.compile(
    r"^(?:laitos-)?([a-z]\d{3}[0-9a-z]{2})(?:-staff|-everyone)?$",
    re.IGNORECASE)
# Departments exist under several school letters (osasto-t410, -e706,
# -a803, -u902), not only T.
DEPT_GROUP_RE = re.compile(r"^osasto-([a-z]\d{3})$", re.IGNORECASE)

# Ordered: "Electrical" must win over "Engineering" in "School of
# Electrical Engineering", "Chemical" over "Engineering" likewise.
SCHOOL_KEYWORDS = [
    ("Chemical", "CHEM"),
    ("Electrical", "ELEC"),
    ("Engineering", "ENG"),
    ("Science", "SCI"),
    ("Arts", "ARTS"),
    ("Business", "BIZ"),
]

# In the real AD data the professors' `company` is their department OU
# code (T313, E706, A803), never "School of ..." text — so when no
# keyword matches, the department code's prefix resolves through this
# static table of Aalto's school letters (the same table the old
# org_units.conf carried, plus the A8/E7 letters the professor list
# actually uses).
SCHOOL_BY_PREFIX = {
    "T1": ("CHEM", "School of Chemical Engineering"),
    "T2": ("ENG", "School of Engineering"),
    "T3": ("SCI", "School of Science"),
    "T4": ("ELEC", "School of Electrical Engineering"),
    "T5": ("Other", "Legacy / university units"),
    "T6": ("Other", "Legacy / university units"),
    "A8": ("ARTS", "School of Arts, Design and Architecture"),
    "E7": ("BIZ", "School of Business"),
    "U9": ("Other", "University common units"),
}

ATTR_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)(::?)[ \t]?(.*)$")
SEPARATOR_RE = re.compile(r"^[-=+]{3,}\s*$")
# The department code inside a professor's distinguishedName:
# CN=Ekroos Ari,OU=T21302,OU=T213,OU=Staff — the first 4-char OU= is the
# unit, the second the department. The lookahead keeps T21302 from
# matching as T213.
DN_DEPT_RE = re.compile(r"OU=([A-Za-z]\d{3})(?=,|$)", re.IGNORECASE)
# A unit-shaped (six-character) OU component: T30402, A80301, ...
UNIT_CODE_RE = re.compile(r"^[A-Za-z]\d{3}[0-9a-z]{2}$", re.IGNORECASE)

CONF_HEADER = """\
# Professor research groups for the Groups tab. Generated by
# tools/build_prof_groups.py from the AD dumps in ~/ad_dump/ (never
# committed); hand-editable, reloaded whenever the file's mtime changes.
# Format:
#   [schools]      OSASTO-PREFIX = SHORT | Full school name
#   [departments]  CODE = Department name | school key
#   [units]        CODE = Unit name | DEPT — a unit shared by several
#                  professors: its row carries no single leader
#   [groups]       leader-user = Leader Name | DEPT | unit codes
# A [groups] line commented out is a professor with no own unit found in
# AD — fill the unit codes in by hand. Rebuilding KEEPS this file's
# uncommented [groups]/[units] lines when the new build produces no
# codes for that key (or does not know the key at all); every kept line
# is listed in the report.
"""


# ---- net ads search dump parsing ---------------------------------------

def parse_dump(text):
    """net ads search output -> list of ``{attribute: [values]}`` records.

    Records are separated by blank lines (or ---- separator lines);
    attribute names are lower-cased for lookup; a line starting with
    whitespace continues the previous value (LDAP folding); an attribute
    may repeat (multi-valued) or use the ``attr:: base64`` spelling.
    """
    records, current, last_attr = [], None, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or SEPARATOR_RE.match(stripped):
            if current:
                records.append(current)
            current, last_attr = None, None
            continue
        if line[:1] in (" ", "\t"):
            # A folded continuation: the leading space is the fold marker,
            # not part of the value.
            if current is not None and last_attr:
                current[last_attr][-1] += line[1:]
            continue
        m = ATTR_RE.match(line)
        if not m:
            continue
        attr, last_attr, value = m.group(1).lower(), m.group(1).lower(), \
            m.group(3)
        if m.group(2) == "::":
            try:
                value = base64.b64decode(
                    value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
            except Exception:
                pass  # keep the raw text; these dumps are textual
        current = current if current is not None else {}
        current.setdefault(attr, []).append(value)
    if current:
        records.append(current)
    return records


def _first(record, attr):
    values = record.get(attr)
    return values[0] if values else None


def _norm(text):
    """Comparison form for names: diacritics stripped, case-folded."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed
                   if not unicodedata.combining(c)).casefold().strip()


def _name_in_text(name, text):
    """Whole-word containment, on _norm()-ed strings: 'Li' must match
    'Li Wei group' but never 'Lindqvist Johan group' or 'Salmelin'. A
    hyphen is NOT a boundary — a compound surname is one name, so
    'Laurila' must not match inside 'Ala-Laurila Petri group' (a
    different professor's unit)."""
    if not name:
        return False
    return re.search(r"(?<![0-9a-z-])%s(?![0-9a-z-])" % re.escape(name),
                     text) is not None


def _unescape_dn(dn):
    return re.sub(r"\\(.)", r"\1", dn or "")


def _dn_cn(dn):
    """The CN RDN of a distinguishedName ('CN=Kyrki Ville,OU=...' ->
    'Kyrki Ville'), for dumps that carry no DN of their own."""
    first = _unescape_dn(dn).split(",")[0].strip()
    return first[3:] if first.upper().startswith("CN=") else first


def dn_leaf_unit(dn):
    """The unit code of a professor's DN leaf OU: the first OU component
    after the CN, when it is unit-shaped — CN=Ala-Nissilä
    Tapio,OU=T30402,OU=T304,... -> T30402. AD ties the professor to that
    unit even when no managedBy or description does (rule d). None when
    the DN carries no unit-shaped leaf OU (a 4-char OU is a department,
    not a unit)."""
    for part in _unescape_dn(dn or "").split(","):
        part = part.strip()
        if part.upper().startswith("OU="):
            code = part[3:].strip()
            if UNIT_CODE_RE.match(code):
                return code.upper()
            return None  # the leaf OU is not a unit-shaped code
    return None


# ---- the two dumps -----------------------------------------------------

def load_professors(records):
    """Professor records, minus disabled accounts and emeritus titles."""
    profs = []
    for rec in records:
        user = _first(rec, "samaccountname")
        if not user:
            continue
        uac = _first(rec, "useraccountcontrol")
        try:
            if uac and int(uac) & 0x2:
                continue  # disabled account
        except ValueError:
            pass
        titles = rec.get("title") or []
        if any("emerit" in (t or "").casefold() for t in titles):
            continue
        profs.append({
            "user": user,
            "name": _first(rec, "displayname") or user,
            "sn": _first(rec, "sn") or "",
            "given": _first(rec, "givenname") or "",
            "title": titles[0] if titles else "",
            "dept_attr": _first(rec, "department") or "",
            "company": _first(rec, "company") or "",
            "division": _first(rec, "division") or "",
            # `dn:` rides along on every record; the explicit attribute
            # is the fallback for dumps that separate it.
            "dn": _first(rec, "dn") or _first(rec, "distinguishedname") or "",
        })
    return profs


def load_units(records):
    """Group records -> ``{code: {"descriptions": [], "managed_bys": []}}``.

    Every cn spelling of a unit (laitos-tNNNXX, tNNNXX-staff) maps to the
    same code, and their descriptions/managedBy values merge: the
    description may live on the laitos group and the owner on the -staff
    group.
    """
    units = {}
    for rec in records:
        for cn in rec.get("cn", []):
            m = UNIT_GROUP_RE.match(cn.strip())
            if not m:
                continue
            unit = units.setdefault(m.group(1).upper(),
                                    {"descriptions": [], "managed_bys": []})
            for value in rec.get("description", []):
                value = value.strip()
                if value and value not in unit["descriptions"]:
                    unit["descriptions"].append(value)
            for value in rec.get("managedby", []):
                if value and value not in unit["managed_bys"]:
                    unit["managed_bys"].append(value)
    return units


# ---- the matching rules (a-b, then the rule-d second pass) --------------

def _managed_by_matches(unit, prof):
    """Rule a: one of the unit's AD groups is managedBy the professor."""
    if not unit["managed_bys"]:
        return False
    if prof["dn"]:
        want = _unescape_dn(prof["dn"]).casefold()
        return any(_unescape_dn(mb).casefold() == want
                   for mb in unit["managed_bys"])
    # No distinguishedName in the dump: compare the DN's CN to the
    # display name — weaker, but the report reviews every claim anyway.
    want = _norm(prof["name"])
    return any(_norm(_dn_cn(mb)) == want for mb in unit["managed_bys"])


def _description_matches(unit, prof, surname_counts):
    """Rule b: a unit description names the professor.

    The surname must appear as a whole word (diacritics stripped,
    case-folded). When several professors share the surname (Kaski,
    Hämäläinen, ...), the description must name the given name too, so
    Kaski Sami and Kaski Petteri cannot claim each other's units.
    """
    sn, given = _norm(prof["sn"]), _norm(prof["given"])
    if not sn:
        return False
    ambiguous = surname_counts.get(sn, 0) > 1
    for desc in unit["descriptions"]:
        text = _norm(desc)
        if not _name_in_text(sn, text):
            continue
        if ambiguous and given and not _name_in_text(given, text):
            continue
        return True
    return False


def own_units(prof, units, candidates, surname_counts):
    """The professor's own unit codes: rule a, else all rule-b matches
    (candidates first, then every unit), else none."""
    managed = sorted(code for code, unit in units.items()
                     if _managed_by_matches(unit, prof))
    if managed:
        return managed, "managedBy"
    matched, seen = [], set()
    for scope in (candidates, sorted(units)):
        for code in scope:
            if code in seen or code not in units:
                continue
            if _description_matches(units[code], prof, surname_counts):
                matched.append(code)
                seen.add(code)
    return matched, "description" if matched else "none"


# ---- schools and departments -------------------------------------------

def school_of(prof, dept_code):
    """``(short, full)`` for a professor: a keyword over their AD
    company/division text when it carries one, else the department
    code's prefix through the static school table, else the bare
    prefix."""
    text = prof["company"] or prof["division"]
    folded = (text or "").casefold()
    if folded:
        for keyword, short in SCHOOL_KEYWORDS:
            if keyword.casefold() in folded:
                return short, text
    prefix = dept_code[:2] if dept_code else ""
    if prefix in SCHOOL_BY_PREFIX:
        return SCHOOL_BY_PREFIX[prefix]
    return prefix, ""


def _majority(values):
    """The most common value (ties: alphabetical first), or ''."""
    counter = Counter(v for v in values if v)
    if not counter:
        return ""
    top = max(counter.values())
    return sorted(v for v, n in counter.items() if n == top)[0]


def derive_departments(profs, groups):
    """[departments]/[schools] rows from the professors' own data.

    A department is a professor's own osasto-tNNN group; its name is the
    majority `department` attribute among that department's professors,
    and its school the majority keyword map result (full name only from
    professors whose text actually matched a keyword).
    """
    by_dept = {}
    for prof in profs:
        dept = groups[prof["user"]]["dept"]
        if dept:
            by_dept.setdefault(dept, []).append(prof)
    departments, schools = {}, {}
    for code, members in sorted(by_dept.items()):
        prefix = code[:2]
        shorts = []
        fulls = []
        for prof in members:
            short, full = school_of(prof, code)
            shorts.append(short)
            if full:
                fulls.append(full)
        short = _majority(shorts) or prefix
        full = _majority(fulls)
        departments[code] = {"name": _majority(
            [p["dept_attr"] for p in members]) or code, "school": prefix}
        if prefix in schools:
            schools[prefix]["shorts"].append(short)
            if full:
                schools[prefix]["fulls"].append(full)
        else:
            schools[prefix] = {"shorts": [short], "fulls": [full] if full
                               else []}
    return departments, {
        prefix: {"short": _majority(v["shorts"]) or prefix,
                 "full": _majority(v["fulls"])}
        for prefix, v in sorted(schools.items())}


# ---- assembly -----------------------------------------------------------

def build(professors_text, unit_groups_text, nss=None):
    """Run the whole pipeline over two dump texts.

    ``nss`` overrides the user->groups boundary (tests inject a fake
    directory; the default resolves at call time so monkeypatching
    ``deps.user_groups`` works). Rule d additionally reads the unit
    member lists through ``deps.group_members`` (patch that too in
    tests). Returns ``(conf_lines, report_lines)`` — conf_lines is the
    rendered prof_groups.conf, report_lines the review report.
    """
    if nss is None:
        nss = deps.user_groups
    profs = load_professors(parse_dump(professors_text))
    units = load_units(parse_dump(unit_groups_text))
    surname_counts = Counter(_norm(p["sn"]) for p in profs if _norm(p["sn"]))

    groups = {}
    for prof in profs:
        nss_groups = nss(prof["user"])
        candidates = sorted({
            m.group(1).upper() for g in (nss_groups or ())
            for m in [UNIT_GROUP_RE.match(g)] if m})
        codes, how = own_units(prof, units, candidates, surname_counts)
        dept = ""
        for g in nss_groups or ():
            m = DEPT_GROUP_RE.match(g)
            if m:
                dept = m.group(1).upper()
                break
        if not dept and prof["dn"]:
            # Professors without a Triton account still carry their
            # department in their DN: CN=...,OU=T21302,OU=T213,OU=Staff.
            m = DN_DEPT_RE.search(prof["dn"])
            if m:
                dept = m.group(1).upper()
        groups[prof["user"]] = {
            "leader_name": prof["name"], "dept": dept, "codes": codes,
            "how": how, "in_nss": nss_groups is not None, "prof": prof,
        }

    # Rule d, the second pass: professors still unit-less claim their DN
    # leaf OU when AD ties them to it another way and no other active
    # professor carries the same leaf (a shared leaf becomes a [units]
    # row instead — no single leader is derivable there).
    leaf_of = {}
    for prof in profs:
        code = dn_leaf_unit(prof["dn"])
        if code:
            leaf_of.setdefault(code, []).append(prof["user"])
    claimed_ab = {code for g in groups.values() for code in g["codes"]}
    rule_d = []
    for prof in profs:
        g = groups[prof["user"]]
        if g["codes"]:
            continue  # rules a/b already resolved this professor
        code = dn_leaf_unit(prof["dn"])
        if (not code or code not in units or code in claimed_ab
                or len(leaf_of.get(code, ())) != 1):
            continue
        unit = units[code]
        by_dept = any(_norm(prof["dept_attr"]) == _norm(desc)
                      for desc in unit["descriptions"])
        low = code.lower()
        members = list(deps.group_members("laitos-" + low) or [])
        members += list(deps.group_members(low + "-staff") or [])
        if not (by_dept or prof["user"] in members):
            continue
        g["codes"] = [code]
        g["how"] = "dn-ou"
        rule_d.append((prof["user"], prof["name"], code, unit))

    departments, schools = derive_departments(profs, groups)

    claimed = Counter(code for g in groups.values() for code in g["codes"])

    # Shared units: unclaimed after rules a-d, but two or more active
    # professors carry the unit as their DN leaf OU. Cost centres (…00)
    # and "common" units are excluded — those are not research groups.
    shared_units = {}
    for code, members in sorted(leaf_of.items()):
        if (claimed.get(code) or len(members) < 2 or code.endswith("00")):
            continue
        unit = units.get(code)
        if not unit or any("common" in (d or "").casefold()
                           for d in unit["descriptions"]):
            continue
        dn_depts = []
        for user in members:
            m = DN_DEPT_RE.search(groups[user]["prof"]["dn"])
            if m:
                dn_depts.append(m.group(1).upper())
        desc = unit["descriptions"][0] if unit["descriptions"] else code
        shared_units[code] = (desc, _majority(dn_depts))

    report = _report(profs, groups, units, claimed, rule_d, shared_units)

    lines = [CONF_HEADER, "[schools]"]
    for prefix, school in sorted(schools.items()):
        value = school["short"] if not school["full"] else \
            "%s | %s" % (school["short"], school["full"])
        lines.append("%s = %s" % (prefix, value))
    lines += ["", "[departments]"]
    for code, dept in sorted(departments.items()):
        lines.append("%s = %s | %s" % (code, dept["name"], dept["school"]))
    # The [units] header is emitted even when empty: hand-edited shared
    # units from a previous build are spliced back in here (main()).
    lines += ["", "[units]"]
    for code, (desc, dept) in sorted(shared_units.items()):
        lines.append("%s = %s | %s" % (code, desc, dept))
    lines += ["", "[groups]"]
    for user, g in sorted(groups.items()):
        if g["codes"]:
            lines.append("%s = %s | %s | %s" % (
                user, g["leader_name"], g["dept"], " ".join(g["codes"])))
        else:
            lines.append("# %s = %s | %s | (no own unit found — fill in)"
                         % (user, g["leader_name"], g["dept"]))
    return lines, report


def _report(profs, groups, units, claimed, rule_d=(), shared_units=None):
    lines = []
    no_unit = [(u, g) for u, g in sorted(groups.items()) if not g["codes"]]
    lines.append("professors: %d (%d with a unit, %d without)"
                 % (len(profs), len(profs) - len(no_unit), len(no_unit)))
    if no_unit:
        lines.append("\nno own unit (add by hand after review):")
        for user, g in no_unit:
            extra = "" if g["in_nss"] else "  [not in NSS]"
            lines.append("  %-12s %-30s %s%s" % (
                user, g["leader_name"], g["dept"] or "-", extra))
    if rule_d:
        lines.append("\nrule-d claims (DN leaf OU — review these):")
        for user, name, code, unit in sorted(rule_d):
            desc = unit["descriptions"][0] if unit["descriptions"] else ""
            lines.append("  %-12s %-30s %s  %s" % (user, name, code, desc))
    if shared_units:
        lines.append("\nshared units (no single leader — written to "
                     "[units]):")
        for code, (desc, dept) in sorted(shared_units.items()):
            lines.append("  %-8s %s  (dept %s)" % (code, desc, dept or "-"))
    no_nss = [u for u, g in groups.items() if not g["in_nss"] and g["codes"]]
    if no_nss:
        lines.append("\nprofessors not in NSS (department unknown): %s"
                     % ", ".join(no_nss))
    not_led = []
    for code, unit in sorted(units.items()):
        if claimed.get(code):
            continue
        for desc in unit["descriptions"]:
            if re.match(r"^.+\S\s+group$", desc.strip(), re.IGNORECASE):
                not_led.append((code, desc.strip()))
                break
    if not_led:
        lines.append("\n\"X group\" units led by nobody in the professor "
                     "list (lecturer-led? add by hand if wanted):")
        for code, desc in not_led:
            lines.append("  %s  %s" % (code, desc))
    multi = sorted(code for code, n in claimed.items() if n > 1)
    if multi:
        lines.append("\nunits claimed by two professors (review!):")
        for code in multi:
            owners = sorted(u for u, g in groups.items() if code in g["codes"])
            lines.append("  %s: %s" % (code, ", ".join(owners)))
    return lines


# ---- hand-line preservation --------------------------------------------

def _section_entries(lines, header):
    """One conf section's entries as ``{key: (line, has_codes)}``:
    uncommented ``key = ...`` lines carry codes, ``# key = ...`` lines
    are the build's no-unit placeholders."""
    try:
        start = lines.index(header) + 1
    except ValueError:
        return {}
    entries = {}
    for line in lines[start:]:
        if line.startswith("["):
            break
        m = re.match(r"^#?\s*(\S+)\s*=", line)
        if m:
            entries[m.group(1)] = (line, not line.startswith("#"))
    return entries


def _preserve_hand_lines(old_path, lines):
    """Hand edits survive a rebuild: uncommented [groups]/[units] lines
    in the existing --out file are kept when the new build produces no
    codes for that key (its commented placeholder is dropped) or does
    not know the key at all (non-professors such as a lecturer-led
    group's leader, hand-filled units). Returns ``(kept_line_texts,
    lines_with_kept_spliced_in)``; a missing or unparseable old file
    keeps nothing."""
    cp = configparser.RawConfigParser(delimiters=("=",))
    cp.optionxform = str
    try:
        cp.read(old_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return [], lines
    kept, out = [], list(lines)
    for section in ("units", "groups"):
        if not cp.has_section(section):
            continue
        old = {k: "%s = %s" % (k, cp.get(section, k))
               for k in cp.options(section)}
        gen = _section_entries(out, "[%s]" % section)
        keep = sorted(k for k in old if k not in gen or not gen[k][1])
        if not keep:
            continue
        kept.extend(old[k] for k in keep)
        # Rebuild the section body sorted by key: the generated lines the
        # new build still owns, plus the kept hand lines in their place.
        merged = {k: line for k, (line, _) in gen.items() if k not in keep}
        for k in keep:
            merged[k] = old[k]
        start = out.index("[%s]" % section) + 1
        end = start
        while end < len(out) and not out[end].startswith("["):
            end += 1
        out[start:end] = [merged[k] for k in sorted(merged)]
    return kept, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dump-dir", default=os.path.join("~", "ad_dump"))
    ap.add_argument("--out", default=os.path.join(REPO_ROOT,
                                                  "prof_groups.conf"))
    args = ap.parse_args(argv)
    dump_dir = os.path.expanduser(args.dump_dir)
    paths = [os.path.join(dump_dir, PROFESSORS_DUMP),
             os.path.join(dump_dir, UNIT_GROUPS_DUMP)]
    for path in paths:
        if not os.path.exists(path):
            sys.exit("missing dump %s — run the net ads search commands "
                     "on the AD server and copy both files to %s"
                     % (path, dump_dir))
    with open(paths[0], encoding="utf-8", errors="replace") as fh:
        professors_text = fh.read()
    with open(paths[1], encoding="utf-8", errors="replace") as fh:
        unit_groups_text = fh.read()

    lines, report = build(professors_text, unit_groups_text)
    kept, lines = _preserve_hand_lines(args.out, lines)
    if kept:
        report.append("\nkept hand lines from the previous build (no "
                      "codes for those keys):")
        for line in kept:
            report.append("  " + line)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    in_groups = lines.index("[groups]")
    n_groups = sum(1 for line in lines[in_groups:]
                   if line and not line.startswith(("#", "[")))
    print("wrote %s (%d group lines)" % (args.out, n_groups))
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
