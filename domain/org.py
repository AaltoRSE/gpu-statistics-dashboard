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

import cache
import deps

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

    Raises DirectoryError when the file is unreadable or unparseable:
    the Groups tab's whole classification depends on it, so a missing
    config must fail loudly (a 502) rather than silently classifying
    everyone as unaffiliated.
    """
    global _cache
    path = path or org_units_path()
    try:
        mtime = os.stat(path).st_mtime
        parsed = None
        with _LOCK:
            if _cache is not None and _cache[0] == (path, mtime):
                return _cache[1]
        parsed = _parse_org_file(path)
    except (OSError, configparser.Error) as exc:
        raise deps.DirectoryError(
            "org file %r unavailable: %s" % (path, exc)) from exc
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


def school_pair(dept_code, org_map):
    """``(short, full)`` for a department code: ``(None, None)`` without
    a department, ``("Other", None)`` when no configured prefix matches
    (legacy T5/T6 prefixes carry their own Other full name)."""
    if not dept_code:
        return None, None
    best = None
    for prefix in org_map["schools"]:
        if dept_code.startswith(prefix) and (
                best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        return "Other", None
    school = org_map["schools"][best]
    return school["short"], school["full"]


def school_for_dept(dept_code, org_map):
    """Longest-prefix match of the department code against [schools];
    ``None`` when there is no department, ``"Other"`` when nothing
    matches (T5/T6 legacy prefixes are themselves named Other)."""
    return school_pair(dept_code, org_map)[0]


# ---- classification ----------------------------------------------------
# How long one user's classification is trusted: org membership moves at
# reorg speed (months), but a user the directory does not know may simply
# not exist YET — a job's owner can appear in NSS between two runs — so
# the unresolved answer is re-asked hourly, the classified one daily.
CLASSIFIED_TTL = 24 * 3600
UNKNOWN_TTL = 3600

_UNRESOLVED = {
    "unit_code": None, "dept_code": None, "school_code": None,
    "extra_units": [], "status": STATUS_UNRESOLVED,
}
_UNAFFILIATED = {
    "unit_code": None, "dept_code": None, "school_code": None,
    "extra_units": [], "status": STATUS_UNAFFILIATED,
}


def classify(groups, org_map=None):
    """One user's org classification from their group names (§2 rules).

    Returns ``{"unit_code", "dept_code", "school_code", "extra_units",
    "status"}``. Unit codes are upper-cased ``T`` + the laitos-tNNNXX
    digits (``T313AA``); 4-digit legacy laitos-tNNNN groups never match.
    The department is the user's own osasto group when they have one
    (lowest code wins, for determinism), else the unit's configured
    DEPT, else the unit's own prefix. With several units, the one whose
    configured department is in the user's osasto set is preferred, ties
    broken by the lowest code; the rest are ``extra_units``.
    """
    if org_map is None:
        org_map = load_org_map()
    unit_codes, dept_codes = set(), set()
    for group in groups or ():
        m = UNIT_GROUP_RE.match(group)
        if m:
            unit_codes.add("T" + m.group(1).upper())
            continue
        m = DEPT_GROUP_RE.match(group) or STAFF_GROUP_RE.match(group)
        if m:
            dept_codes.add("T" + m.group(1))
    if unit_codes:
        ordered = sorted(unit_codes)
        if dept_codes:
            # Prefer the unit whose configured department is one of the
            # user's own osasto groups; ties (and no-match) by lowest code.
            preferred = [u for u in ordered
                         if unit_dept(u, org_map) in dept_codes]
            chosen = preferred[0] if preferred else ordered[0]
        else:
            chosen = ordered[0]
        dept = sorted(dept_codes)[0] if dept_codes \
            else unit_dept(chosen, org_map)
        return {
            "unit_code": chosen,
            "dept_code": dept,
            "school_code": school_for_dept(dept, org_map),
            "extra_units": [u for u in ordered if u != chosen],
            "status": STATUS_UNIT,
        }
    if dept_codes:
        dept = sorted(dept_codes)[0]
        return {
            "unit_code": None, "dept_code": dept,
            "school_code": school_for_dept(dept, org_map),
            "extra_units": [], "status": STATUS_DEPT,
        }
    return dict(_UNAFFILIATED)


def _resolve_one(username, org_map):
    """One user's classification, cached per user (24 h / 1 h).

    Errors are never cached: a DirectoryError propagates and the next
    caller retries, while an unknown user IS cached (briefly) so one
    typo'd username in a window cannot re-hit NSS every request.
    """
    key = cache.user_org_key(username)
    hit, cached = deps.route_cache.peek(key)
    if hit:
        return cached
    groups = deps.user_groups(username)
    if groups is None:
        result = dict(_UNRESOLVED)
        ttl = UNKNOWN_TTL
    else:
        result = classify(groups, org_map)
        ttl = CLASSIFIED_TTL
    deps.route_cache.set(key, ttl, result)
    return result


def resolve_users(usernames, org_map=None):
    """Classify many users, with per-user coverage (§ commit 4).

    Returns ``(mapping, coverage)``: mapping is ``{user: classify()}``
    for every user whose lookup succeeded; coverage is
    ``{users, affiliated, unaffiliated, unresolved, failed,
    unmapped_codes}`` — failed being the users whose NSS lookup raised
    (their activity is disclosed as unclassified, never folded into
    Unaffiliated), unmapped_codes the unit codes the config has no (or
    an empty) name for. Raises DirectoryError only when EVERY lookup
    failed — a total directory outage is a 502, a partial one a served
    response with a coverage banner.
    """
    if org_map is None:
        org_map = load_org_map()
    mapping, failed = {}, 0
    users = affili = unaffili = unresolv = 0
    unmapped = set()
    for username in sorted(usernames):
        users += 1
        try:
            result = _resolve_one(username, org_map)
        except deps.DirectoryError:
            failed += 1
            continue
        mapping[username] = result
        status = result["status"]
        if status in (STATUS_UNIT, STATUS_DEPT):
            affili += 1
        elif status == STATUS_UNAFFILIATED:
            unaffili += 1
        else:
            unresolv += 1
        code = result["unit_code"]
        if code is not None:
            unit = org_map["units"].get(code)
            if not unit or not unit["name"]:
                unmapped.add(code)
    if users and failed == users:
        raise deps.DirectoryError(
            "every user lookup failed (%d of %d)" % (failed, users))
    coverage = {
        "users": users,
        "affiliated": affili,
        "unaffiliated": unaffili,
        "unresolved": unresolv,
        "failed": failed,
        "unmapped_codes": sorted(unmapped),
    }
    return mapping, coverage


# ---- the Groups roll-up (plan commit 6 f) ------------------------------

GROUP_UNIT_PREFIX = "unit:"
GROUP_DEPT_PREFIX = "dept:"
GROUP_UNAFFILIATED = "unaffiliated"
GROUP_UNRESOLVED = "unresolved"

LOW_UTIL_THRESHOLD = 30.0


def group_id_for(result, level):
    """A classification's roll-up row identity at the given level.

    At unit level a unit user's own group and a unit-less user a
    department row (``dept:T410``); at department level both statuses
    collapse into the user's department (their own osasto wins over the
    unit's configured DEPT). The two special rows are their own ids.
    """
    status = result["status"]
    if status == STATUS_UNIT and level != "department":
        return GROUP_UNIT_PREFIX + result["unit_code"]
    if result["dept_code"]:
        return GROUP_DEPT_PREFIX + result["dept_code"]
    return status


def rollup_groups(user_rows, mapping, jobs_view, step, level="unit",
                  org_map=None):
    """Roll per-user aggregates up to Groups-tab rows (commit 6 f).

    ``user_rows`` is aggregate_users()' output; ``mapping`` the
    classification map from resolve_users (users whose lookup failed are
    absent — their activity stays out of every row and is disclosed
    through coverage.failed, never folded into Unaffiliated);
    ``jobs_view`` the window's job view for the per-job low-utilization
    count; ``step`` the window's query step, from which a member's
    observed GPU-hours derive (samples x step). ``level`` is "unit" or
    "department".

    Rows are ordered by util_gpu_hours descending (ties by name), with
    the Unaffiliated and Unresolved rows ALWAYS present — even empty —
    so "no such users" can never be read as "everyone is classified"
    (the same spirit as the no-data gh200 row). Members ride along under
    ``members`` for the drill-down; the response schema keeps only
    ``top_users``.
    """
    if org_map is None:
        org_map = load_org_map()
    rows = {}

    def row_for(gid, name, dept_code):
        return rows.setdefault(gid, {
            "group_id": gid,
            "group_name": name,
            "dept_code": dept_code,
            "dept_name": dept_name(dept_code, org_map) if dept_code else None,
            "school_code": None, "school_name": None,
            "users": 0, "jobs": 0, "running_jobs": 0,
            "_util_sum": 0.0, "_util_samples": 0,
            "util_gpu_hours": 0.0, "gpu_hours": 0.0,
            "vram_sum": 0.0, "vram_n": 0,
            "low_eff_jobs": 0,
            "members": [],
        })

    # The two always-present rows are seeded before any member lands, so
    # an empty window still reports them as genuine zeros.
    row_for(GROUP_UNAFFILIATED, "Unaffiliated", None)
    row_for(GROUP_UNRESOLVED, "Unresolved", None)

    for m in user_rows:
        result = mapping.get(m["user"])
        if result is None:
            continue  # failed lookup: coverage.failed, not a fake row
        status = result["status"]
        if status == STATUS_UNIT and level != "department":
            gid = GROUP_UNIT_PREFIX + result["unit_code"]
            code = result["unit_code"]
            name = unit_name(code, org_map)
            dept = unit_dept(code, org_map)
        else:
            dept = result["dept_code"]
            if not dept:
                gid = status  # unaffiliated / unresolved
                name = "Unaffiliated" if status == STATUS_UNAFFILIATED \
                    else "Unresolved"
                code = None
            else:
                gid = GROUP_DEPT_PREFIX + dept
                code = None
                name = dept_name(dept, org_map) + (
                    "" if level == "department" else " (no unit)")
        row = row_for(gid, name, dept)
        short, full = school_pair(dept, org_map)
        row["school_code"] = short
        row["school_name"] = full
        row["users"] += 1
        row["jobs"] += m["jobs"]
        row["running_jobs"] += m["running_jobs"]
        row["_util_sum"] += m["_util_sum"]
        row["_util_samples"] += m["_util_samples"]
        row["util_gpu_hours"] += m["util_gpu_hours"]
        # Observed GPU-hours: the window GPU time the member series
        # covered — the denominator of the utilization weighting, and
        # the only allocation figure the Prometheus-only pipeline has.
        row["gpu_hours"] += m["_util_samples"] * step / 3600.0
        row["vram_sum"] += m.get("_vram_sum", 0.0)
        row["vram_n"] += m.get("_vram_n", 0)
        row["members"].append({
            "user": m["user"],
            "jobs": m["jobs"],
            "running_jobs": m["running_jobs"],
            "mean_util": m["mean_util"],
            "util_gpu_hours": m["util_gpu_hours"],
            "vram_avg": m["vram_avg"],
            "gpu_types": m["gpu_types"],
            "unit_code": result["unit_code"],
            "dept_code": result["dept_code"],
            "school_code": result["school_code"],
            "extra_units": result["extra_units"],
            "status": status,
        })

    gids_of = {}
    for user, result in mapping.items():
        status = result["status"]
        if status == STATUS_UNIT and level != "department":
            gids_of[user] = GROUP_UNIT_PREFIX + result["unit_code"]
        elif result["dept_code"]:
            gids_of[user] = GROUP_DEPT_PREFIX + result["dept_code"]
        else:
            gids_of[user] = status
    for j in jobs_view:
        gid = gids_of.get(j["user"])
        row = rows.get(gid)
        if row is not None and (j.get("mean_util") or 0.0) < LOW_UTIL_THRESHOLD:
            row["low_eff_jobs"] += 1

    out = []
    for row in rows.values():
        row["mean_util"] = (
            round(row["_util_sum"] / row["_util_samples"], 2)
            if row["_util_samples"] else 0.0)
        row["vram_avg"] = (
            round(row["vram_sum"] / row["vram_n"], 1) if row["vram_n"] else None)
        row["util_gpu_hours"] = round(row["util_gpu_hours"], 2)
        row["gpu_hours"] = round(row["gpu_hours"], 2)
        row["top_users"] = [
            {"user": m["user"], "util_gpu_hours": m["util_gpu_hours"]}
            for m in sorted(row["members"],
                            key=lambda m: (-m["util_gpu_hours"], m["user"]))[:5]
        ]
        out.append(row)
    out.sort(key=lambda r: (-r["util_gpu_hours"],
                            (r["group_name"] or "").lower(), r["group_id"]))
    return out
