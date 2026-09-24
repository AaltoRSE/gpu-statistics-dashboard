"""Organisational classification: NSS groups -> group / department / school.

The Groups tab rolls per-user GPU activity up to the organisational unit
(laitos-tNNNXX, e.g. a research group) a user belongs to, its department
(osasto-tNNN) and school (OU=T<n>). Membership comes from the NSS groups
the dashboard host already resolves (``groups <user>`` — no AD call at
runtime); names come from ``org_units.conf``, a hand-editable file at the
repo root (``ORG_UNITS_FILE`` overrides the location), reloaded whenever
its mtime changes so a name fix never needs a restart.

A user's own ``osasto-t*`` group always wins over the file: after reorgs
a unit sits under several Staff parents, and the file's DEPT is only the
best static guess. Users the directory does not know land in the
Unresolved row; users with no laitos-*/osasto-*/tNNN-staff group land in
the Unaffiliated row — both rows are always shown and never merged into
another row (the same spirit as never rendering an unmeasured job 0%).
"""

import configparser
import os
import re
import threading

# NSS group spellings (case-insensitive; codes are upper-cased on parse).
# laitos-tNNNN legacy groups (4 digits) never match the unit regex.
UNIT_GROUP_RE = re.compile(r"^laitos-t(\d{3}[0-9a-z]{2})$", re.IGNORECASE)
DEPT_GROUP_RE = re.compile(r"^osasto-t(\d{3})$", re.IGNORECASE)
# A user with no laitos-t* unit can still carry a department staff-role
# group (tNNN-staff); it resolves to the department, never to a unit.
STAFF_GROUP_RE = re.compile(r"^t(\d{3})-staff$", re.IGNORECASE)

DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "org_units.conf")

# Statuses of one user's classification (see classify()).
STATUS_UNIT = "unit"                # has a laitos-t* unit group
STATUS_DEPT = "dept"                # department only (osasto / tNNN-staff)
STATUS_UNAFFILIATED = "unaffiliated"  # known user, no org groups
STATUS_UNRESOLVED = "unresolved"    # the directory does not know the user

_LOCK = threading.Lock()
_cache = None  # ((path, mtime), parsed map) or None


def _split_pipes(value, parts):
    """Split a config value on its LAST ``|`` into exactly ``parts``
    trimmed fields (a name may itself contain a pipe character)."""
    pieces = [p.strip() for p in value.rsplit("|", parts - 1)]
    return pieces + [""] * (parts - len(pieces))


def _parse_org_file(path):
    cp = configparser.RawConfigParser(delimiters=("=",))
    cp.optionxform = str  # keys are codes; keep their case before upper()
    cp.read(path, encoding="utf-8")

    def section(name):
        if not cp.has_section(name):
            return {}
        return {k.strip().upper(): v for k, v in cp.items(name)}

    schools = {}
    for code, value in section("schools").items():
        short, full = _split_pipes(value, 2)
        schools[code] = {"short": short, "full": full}
    departments = {}
    for code, value in section("departments").items():
        departments[code] = value.strip()
    units = {}
    for code, value in section("units").items():
        name, dept = _split_pipes(value, 2)
        units[code] = {"name": name, "dept": dept.strip().upper()}
    return {"schools": schools, "departments": departments, "units": units}


def org_units_path():
    """The org_units.conf location: ``ORG_UNITS_FILE`` or the repo root."""
    return os.environ.get("ORG_UNITS_FILE") or DEFAULT_FILE


def load_org_map(path=None):
    """The parsed org map, cached until the file's mtime changes.

    Returns ``{"schools": {PREFIX: {"short", "full"}},
    "departments": {CODE: name}, "units": {CODE: {"name", "dept"}}}``
    with every key upper-cased (a hand-edited lower-case code still
    resolves). The cache key includes the path, so flipping
    ``ORG_UNITS_FILE`` between calls re-reads instead of reusing the
    previous file's parse.
    """
    global _cache
    path = path or org_units_path()
    mtime = os.stat(path).st_mtime
    with _LOCK:
        if _cache is not None and _cache[0] == (path, mtime):
            return _cache[1]
    parsed = _parse_org_file(path)
    with _LOCK:
        _cache = ((path, mtime), parsed)
    return parsed


def reset_org_cache():
    """Drop the module-level parsed-file cache (test isolation)."""
    global _cache
    with _LOCK:
        _cache = None


def unit_dept(unit_code, org_map):
    """A unit's department: its configured DEPT, else its own TNNN prefix."""
    unit = org_map["units"].get(unit_code)
    if unit and unit["dept"]:
        return unit["dept"]
    return unit_code[:4]


def dept_name(dept_code, org_map):
    """A department's display name: the configured one, else the code."""
    return org_map["departments"].get(dept_code) or dept_code


def unit_name(unit_code, org_map):
    """A unit's display name: the configured one, else the raw code (a
    unit with no name anywhere in AD is shown by its code)."""
    unit = org_map["units"].get(unit_code)
    return (unit and unit["name"]) or unit_code


def school_for_dept(dept_code, org_map):
    """Longest-prefix match of the department code against [schools];
    ``None`` when there is no department, ``"Other"`` when nothing
    matches (T5/T6 legacy prefixes are themselves named Other)."""
    if not dept_code:
        return None
    best = None
    for prefix in org_map["schools"]:
        if dept_code.startswith(prefix) and (
                best is None or len(prefix) > len(best)):
            best = prefix
    return org_map["schools"][best]["short"] if best else "Other"
