"""Organisational classification: NSS groups -> professor group /
department / school.

The Groups tab's groups are professor research groups: each row is one
professor's group, defined by the AD unit their ``prof_groups.conf`` row
names. Membership is read from the NSS groups this host already resolves
(``groups <user>`` — no AD call at runtime): a unit's ``laitos-tNNNXX``
group holds the people paid there, ``tNNNXX-staff`` its staff and
``tNNNXX-everyone`` its affiliates; the professor leads the group. Names
come from ``prof_groups.conf`` — generated from AD dumps by
tools/build_prof_groups.py, hand-editable at the repo root
(``PROF_GROUPS_FILE`` overrides the location), reloaded whenever its
mtime changes so a fix never needs a restart.

Strengths order a user's memberships: leader (5) beats paid (4) beats
external (3) beats staff (2) beats everyone (1). The primary group is the
strongest membership (ties: the group whose department matches the
user's own osasto, then professor groups before shared-unit rows, then
the lowest key); the rest ride along as ``extra_groups``. A unit shared
by several professors (the conf's ``[units]`` section, group key
``unit:<CODE>``) has no single leader: it is seeded with no leader and
reads the same four member lists, and its row renders as
"<Unit> (shared unit)". A user in no configured group but with an
``osasto-t*`` group is department-only ("<Department>, no professor
group"); a user with neither still falls back to the department their
own unit-shaped groups encode (see own_dept_of). Users with neither —
including users the directory does not know (status unresolved) — land
in the Unaffiliated row, which is always shown and never merged into
another row (the same spirit as never rendering an unmeasured job 0%);
the unresolved status itself still drives the 1 h NSS re-ask and
coverage.unresolved.
"""

import configparser
import os
import re
import threading

import cache
import deps

# The user's own department: osasto-tNNN, else a legacy tNNN-staff role
# group (four characters — a unit's tNNNXX-staff is six and never
# matches), else — with neither — the department the unit code of the
# user's own unit-shaped groups encodes (UNIT_GROUP_RE below; the
# membership index is still what resolves GROUP membership — a user's
# own laitos-tNNNXX group is merely how they got into the index).
DEPT_GROUP_RE = re.compile(r"^osasto-([a-z]\d{3})$", re.IGNORECASE)
STAFF_GROUP_RE = re.compile(r"^([a-z]\d{3})-staff$", re.IGNORECASE)
# Unit-shaped groups in a user's own list: laitos-tNNNXX, tNNNXX-staff,
# tNNNXX-everyone, auto-ext-tNNNXX (external visitors) or the bare unit
# group. Six-character codes only — the 4-char tNNN-staff shape is a
# department role group and osasto-* always wins first. The unit code's
# first four characters are its department (T31371 -> T313).
UNIT_GROUP_RE = re.compile(
    r"^(?:laitos-|auto-ext-)?([a-z]\d{3}[0-9a-z]{2})(?:-staff|-everyone)?$",
    re.IGNORECASE)

DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prof_groups.conf")

# Statuses of one user's classification (see classify()).
STATUS_GROUP = "group"              # member (or leader) of a prof group
STATUS_DEPT = "dept"                # department only, no prof group
STATUS_UNAFFILIATED = "unaffiliated"  # known user, no relevant groups
STATUS_UNRESOLVED = "unresolved"    # the directory does not know the user
                                    # (rolls up under Unaffiliated; still
                                    # drives the 1 h NSS re-ask and
                                    # coverage.unresolved)

# How strongly a user belongs to a group (see membership_index()).
STRENGTH = {"leader": 5, "paid": 4, "external": 3, "staff": 2, "everyone": 1}
STRENGTH_LABEL = {v: k for k, v in STRENGTH.items()}

_LOCK = threading.Lock()
_cache = None  # ((path, mtime), parsed conf) or None


def _split_pipes(value, parts):
    """Split a config value on its LAST ``|`` into exactly ``parts``
    trimmed fields (a name may itself contain a pipe character)."""
    pieces = [p.strip() for p in value.rsplit("|", parts - 1)]
    return pieces + [""] * (parts - len(pieces))


def _parse_prof_file(path):
    cp = configparser.RawConfigParser(delimiters=("=",))
    cp.optionxform = str  # [groups] keys are usernames; keep their case
    cp.read(path, encoding="utf-8")

    def section(name):
        if not cp.has_section(name):
            return {}
        return {k.strip(): v for k, v in cp.items(name)}

    schools = {}
    for code, value in section("schools").items():
        short, full = _split_pipes(value, 2)
        schools[code.upper()] = {"short": short, "full": full}
    departments = {}
    for code, value in section("departments").items():
        name, school = _split_pipes(value, 2)
        departments[code.upper()] = {
            "name": name, "school": school.strip().upper() or None}
    groups = {}
    for user, value in section("groups").items():
        name, dept, codes = _split_pipes(value, 3)
        groups[user] = {
            "name": name,
            "dept": dept.strip().upper() or None,
            "unit_codes": [c.upper() for c in codes.split()],
        }
    units = {}
    for code, value in section("units").items():
        name, dept = _split_pipes(value, 2)
        units[code.upper()] = {"name": name,
                               "dept": dept.strip().upper() or None}
    return {"schools": schools, "departments": departments,
            "groups": groups, "units": units}


def prof_groups_path():
    """The prof_groups.conf location: ``PROF_GROUPS_FILE`` or the repo
    root."""
    return os.environ.get("PROF_GROUPS_FILE") or DEFAULT_FILE


def load_prof_groups(path=None):
    """The parsed prof_groups.conf, cached until the file's mtime
    changes.

    Returns ``{"schools": {PREFIX: {"short", "full"}},
    "departments": {CODE: {"name", "school"}},
    "groups": {LEADER: {"name", "dept", "unit_codes"}},
    "units": {CODE: {"name", "dept"}},
    "_source": (path, mtime)}`` with every key upper-cased (a
    hand-edited lower-case code still resolves). The cache key includes
    the path, so flipping ``PROF_GROUPS_FILE`` between calls re-reads
    instead of reusing the previous file's parse.

    Raises DirectoryError when the file is unreadable or unparseable:
    the Groups tab's whole classification depends on it, so a missing
    config must fail loudly (a 502) rather than silently classifying
    everyone as unaffiliated.
    """
    global _cache
    path = path or prof_groups_path()
    try:
        mtime = os.stat(path).st_mtime
        parsed = None
        with _LOCK:
            if _cache is not None and _cache[0] == (path, mtime):
                return _cache[1]
        parsed = _parse_prof_file(path)
    except (OSError, configparser.Error) as exc:
        raise deps.DirectoryError(
            "prof_groups file %r unavailable: %s" % (path, exc)) from exc
    parsed["_source"] = (path, mtime)
    with _LOCK:
        _cache = ((path, mtime), parsed)
    return parsed


def reset_prof_cache():
    """Drop the module-level parsed-file cache (test isolation)."""
    global _cache
    with _LOCK:
        _cache = None


# ---- conf lookups -------------------------------------------------------

# Group-key prefix of a shared unit's row ([units]); URL-safe like
# GROUP_DEPT_PREFIX, so it doubles as the row id and drill-down path.
GROUP_UNIT_PREFIX = "unit:"


def group_dept(leader, conf):
    """A group's configured department code (its professor's, or the
    shared unit's ``[units]`` line), or None."""
    if leader and leader.startswith(GROUP_UNIT_PREFIX):
        unit = (conf.get("units") or {}).get(
            leader[len(GROUP_UNIT_PREFIX):])
        return unit and unit["dept"]
    group = conf["groups"].get(leader)
    return group and group["dept"]


def leader_name(leader, conf):
    """A group's display name: the configured leader name, else the
    username (a hand-added row may carry no name yet); a shared unit's
    key names the unit with a "(shared unit)" suffix."""
    if leader and leader.startswith(GROUP_UNIT_PREFIX):
        unit = (conf.get("units") or {}).get(
            leader[len(GROUP_UNIT_PREFIX):])
        return ("%s (shared unit)" % unit["name"]) if unit else leader
    group = conf["groups"].get(leader)
    return (group and group["name"]) or leader


def dept_name(dept_code, conf):
    """A department's display name: the configured one, else the code."""
    dept = conf["departments"].get(dept_code)
    return (dept and dept["name"]) or dept_code


def school_pair(dept_code, conf):
    """``(short, full)`` for a department code: ``(None, None)`` without
    a department, the department's configured school key when it
    resolves, else the longest school prefix the code starts with,
    ``("Other", None)`` when nothing matches."""
    if not dept_code:
        return None, None
    dept = conf["departments"].get(dept_code)
    if dept and dept["school"] and dept["school"] in conf["schools"]:
        school = conf["schools"][dept["school"]]
        return school["short"], school["full"]
    best = None
    for prefix in conf["schools"]:
        if dept_code.startswith(prefix) and (
                best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        return "Other", None
    school = conf["schools"][best]
    return school["short"], school["full"]


def school_for_dept(dept_code, conf):
    """The school short name for a department code (see school_pair)."""
    return school_pair(dept_code, conf)[0]


# ---- the membership index ----------------------------------------------
# How long one directory answer is trusted: membership moves at reorg
# speed (months), but a user the directory does not know may simply not
# exist YET — a job's owner can appear in NSS between two runs — so the
# unknown answer is re-asked hourly, the known one daily.
CLASSIFIED_TTL = 24 * 3600
UNKNOWN_TTL = 3600

_UNRESOLVED = {
    "group": None, "membership": None, "dept_code": None,
    "school_code": None, "own_dept": None, "extra_groups": [],
    "status": STATUS_UNRESOLVED,
}
_UNAFFILIATED = {
    "group": None, "membership": None, "dept_code": None,
    "school_code": None, "own_dept": None, "extra_groups": [],
    "status": STATUS_UNAFFILIATED,
}


def _group_members_cached(group_name):
    """deps.group_members, cached per group: a configured group set is a
    few dozen NSS reads, shared by every user classification within a
    TTL instead of repeated per user. The TTL lives with the value (24 h
    for a group the directory knows, 1 h for one it does not — a group
    may be created after the conf names it), so entries are stored via
    TtlCache.set, not get_or_set."""
    key = cache.group_members_key(group_name)
    hit, members = deps.route_cache.peek(key)
    if hit:
        return members
    members = deps.group_members(group_name)
    deps.route_cache.set(
        key, CLASSIFIED_TTL if members is not None else UNKNOWN_TTL, members)
    return members


def membership_index(conf):
    """``{username: {group key: best strength}}`` — who belongs to which
    configured group, and how strongly. The key is a professor group's
    leader username, or ``unit:<CODE>`` for a shared unit.

    For every configured unit code — a professor group's own units and
    the ``[units]`` shared units — the four NSS member groups are read:
    ``laitos-<code>`` (paid there, strength 4), ``<code>-staff`` (2),
    ``<code>-everyone`` (1) and ``auto-ext-<code>`` (external visitors,
    3). A professor group's leader is seeded into their own group at
    strength 5 unconditionally — a leader who sits in nobody's member
    list (Bäckström is in Alku's tNNNXX-staff, not their own unit's)
    still leads; shared units have no leader to seed. A user in several
    lists of one group keeps only that group's strongest strength.
    """
    index = {}

    def add(user, key, strength):
        known = index.setdefault(user, {})
        if strength > known.get(key, 0):
            known[key] = strength

    def read_unit(code, key):
        code = code.lower()
        for name, kind in (("laitos-" + code, "paid"),
                           (code + "-staff", "staff"),
                           (code + "-everyone", "everyone"),
                           ("auto-ext-" + code, "external")):
            members = _group_members_cached(name)
            if members:
                for user in members:
                    add(user, key, STRENGTH[kind])

    for leader, spec in conf["groups"].items():
        add(leader, leader, STRENGTH["leader"])
        for code in spec["unit_codes"]:
            read_unit(code, leader)
    for code in conf.get("units") or {}:
        read_unit(code, GROUP_UNIT_PREFIX + code)
    return index


def _index_cached(conf):
    """The membership index, cached per conf file version: one build is
    one NSS read per (group x member list), so a day's worth of
    classifications shares it, and a conf edit (new mtime) addresses a
    different key — a hand-fix shows on the next request. A conf without
    a ``_source`` (hand-built in tests) is built uncached."""
    source = conf.get("_source")
    if source is None:
        return membership_index(conf)
    path, mtime = source
    return deps.route_cache.get_or_set(
        ("prof_groups_index", path, mtime), CLASSIFIED_TTL,
        lambda: membership_index(conf))


# ---- classification ----------------------------------------------------

def own_dept_of(groups):
    """The user's own department: their osasto-tNNN group, else a
    tNNN-staff role group; when neither exists, the first four
    characters of the lowest unit-shaped group code they carry
    (laitos-tNNNXX / tNNNXX-staff / -everyone / auto-ext-tNNNXX — the
    department the unit code itself encodes, T31371 -> T313). The
    acronym-style and resource groups are deliberately not clues. None
    with neither shape."""
    codes = set()
    for group in groups or ():
        m = DEPT_GROUP_RE.match(group) or STAFF_GROUP_RE.match(group)
        if m:
            codes.add(m.group(1).upper())
    if codes:
        return sorted(codes)[0]
    for group in groups or ():
        m = UNIT_GROUP_RE.match(group)
        if m:
            codes.add(m.group(1).upper())
    return sorted(codes)[0][:4] if codes else None


def classify(username, groups, index, conf):
    """One user's classification from their group names and the
    membership index (plan §3).

    Returns ``{"group", "membership", "dept_code", "school_code",
    "own_dept", "extra_groups", "status"}``. The primary group is the
    strongest membership, ties preferring the group whose configured
    department is the user's own osasto, then professor groups before
    shared-unit rows, then the lowest key; a group member's department
    is the GROUP's department (the professor's, or the shared unit's
    configured one) — the research group is the organizational home the
    row reports, not the member's own cost-centre osasto, which rides
    along as ``own_dept`` for the drill-down. ``membership`` names the
    strength of the primary membership (leader/paid/external/staff/
    everyone).
    """
    if groups is None:
        return dict(_UNRESOLVED)
    own_dept = own_dept_of(groups)
    memberships = index.get(username) or {}
    if memberships:
        leaders = sorted(
            memberships,
            key=lambda leader: (
                -memberships[leader],
                0 if (own_dept and group_dept(leader, conf) == own_dept)
                else 1,
                0 if not leader.startswith(GROUP_UNIT_PREFIX) else 1,
                leader))
        primary = leaders[0]
        dept = group_dept(primary, conf) or own_dept
        return {
            "group": primary,
            "membership": STRENGTH_LABEL[memberships[primary]],
            "dept_code": dept,
            "school_code": school_for_dept(dept, conf),
            "own_dept": own_dept,
            "extra_groups": leaders[1:],
            "status": STATUS_GROUP,
        }
    if own_dept:
        return {
            "group": None, "membership": None, "dept_code": own_dept,
            "school_code": school_for_dept(own_dept, conf),
            "own_dept": own_dept, "extra_groups": [],
            "status": STATUS_DEPT,
        }
    return dict(_UNAFFILIATED)


def _groups_cached(username):
    """One user's RAW NSS group list, cached per user (24 h / 1 h for a
    user the directory does not know).

    The raw groups are cached, not the classification built from them:
    the classification also depends on prof_groups.conf and the
    membership index, and a conf edit must show on the next request
    rather than waiting out a per-user TTL. Errors are never cached: a
    DirectoryError propagates and the next caller retries, while an
    unknown user IS cached (briefly) so one typo'd username in a window
    cannot re-hit NSS every request.
    """
    key = cache.user_groups_key(username)
    hit, groups = deps.route_cache.peek(key)
    if hit:
        return groups
    groups = deps.user_groups(username)
    deps.route_cache.set(
        key, CLASSIFIED_TTL if groups is not None else UNKNOWN_TTL, groups)
    return groups


def resolve_users(usernames, conf=None, index=None):
    """Classify many users, with per-user coverage.

    Returns ``(mapping, coverage)``: mapping is ``{user: classify()}``
    for every user whose lookup succeeded; coverage is
    ``{users, in_prof_group, dept_only, unaffiliated, unresolved,
    failed}`` — failed being the users whose NSS lookup raised (their
    activity is disclosed as unclassified, never folded into
    Unaffiliated). Raises DirectoryError only when EVERY lookup failed —
    a total directory outage is a 502, a partial one a served response
    with a coverage banner.
    """
    if conf is None:
        conf = load_prof_groups()
    if index is None:
        index = _index_cached(conf)
    mapping, failed = {}, 0
    users = ingroup = deptonly = unaffili = unresolv = 0
    for username in sorted(usernames):
        users += 1
        try:
            groups = _groups_cached(username)
        except deps.DirectoryError:
            failed += 1
            continue
        result = classify(username, groups, index, conf)
        mapping[username] = result
        status = result["status"]
        if status == STATUS_GROUP:
            ingroup += 1
        elif status == STATUS_DEPT:
            deptonly += 1
        elif status == STATUS_UNAFFILIATED:
            unaffili += 1
        else:
            unresolv += 1
    if users and failed == users:
        raise deps.DirectoryError(
            "every user lookup failed (%d of %d)" % (failed, users))
    coverage = {
        "users": users,
        "in_prof_group": ingroup,
        "dept_only": deptonly,
        "unaffiliated": unaffili,
        "unresolved": unresolv,
        "failed": failed,
    }
    return mapping, coverage


# ---- the Groups roll-up -------------------------------------------------

GROUP_DEPT_PREFIX = "dept:"
GROUP_UNAFFILIATED = "unaffiliated"

LOW_UTIL_THRESHOLD = 30.0


def group_id_for(result, level):
    """A classification's roll-up row identity at the given level.

    At group level a group member's row is their leader's username, or
    the shared unit's ``unit:<CODE>`` key; at department level every
    classified user collapses into their department row (``dept:TNNN``)
    — a group member their group's department, a department-only user
    their own osasto. A group member with no department anywhere keeps
    the group row at both levels (a department row needs a department).
    A non-group user without a department — unaffiliated, or unresolved
    (the directory does not know them) — shares the Unaffiliated row.
    """
    status = result["status"]
    if result["dept_code"]:
        if status != STATUS_GROUP or level == "department":
            return GROUP_DEPT_PREFIX + result["dept_code"]
        return result["group"]
    if status == STATUS_GROUP:
        return result["group"]
    return GROUP_UNAFFILIATED


def rollup_groups(user_rows, mapping, jobs_view, step, level="group",
                  conf=None):
    """Roll per-user aggregates up to Groups-tab rows.

    ``user_rows`` is aggregate_users()' output; ``mapping`` the
    classification map from resolve_users (users whose lookup failed are
    absent — their activity stays out of every row and is disclosed
    through coverage.failed, never folded into Unaffiliated);
    ``jobs_view`` the window's job view for the per-job low-utilization
    count; ``step`` the window's query step, from which a member's
    observed GPU-hours derive (samples x step). ``level`` is "group"
    (default) or "department".

    At group level a group member's row is named after their professor
    (or, for a shared unit's member, "<Unit> (shared unit)" with no
    leader); a department-only user lands in "<Department>, no professor
    group".
    At department level both collapse into one department row (a group
    member under their professor's department, everyone else under
    their own osasto) named after the department alone. Rows are ordered
    by util_gpu_hours descending (ties by name), with the Unaffiliated
    row ALWAYS present — even empty — so "no such users"
    can never be read as "everyone is classified" (the same spirit as
    the no-data gh200 row); a user the directory does not know (status
    unresolved) rolls up under it too. Members ride along under
    ``members`` for the drill-down; the response schema keeps only
    ``top_users``.
    """
    if conf is None:
        conf = load_prof_groups()
    rows = {}

    def row_for(gid, name, dept_code, leader=None):
        if gid.startswith(GROUP_UNIT_PREFIX):
            # a shared unit row: no leader, its own single unit code
            unit_codes = [gid[len(GROUP_UNIT_PREFIX):]]
        else:
            unit_codes = list(
                conf["groups"].get(leader, {}).get("unit_codes", [])) \
                if leader else []
        return rows.setdefault(gid, {
            "group_id": gid,
            "group_name": name,
            "leader": leader,
            "leader_name": leader_name(leader, conf) if leader else None,
            "unit_codes": unit_codes,
            "dept_code": dept_code,
            "dept_name": dept_name(dept_code, conf) if dept_code else None,
            "school_code": None, "school_name": None,
            "users": 0, "jobs": 0, "running_jobs": 0,
            "_util_sum": 0.0, "_util_samples": 0,
            "util_gpu_hours": 0.0, "gpu_hours": 0.0,
            "vram_sum": 0.0, "vram_n": 0,
            "low_eff_jobs": 0,
            "members": [],
        })

    # The always-present row is seeded before any member lands, so an
    # empty window still reports it as a genuine zero.
    row_for(GROUP_UNAFFILIATED, "Unaffiliated", None)

    for m in user_rows:
        result = mapping.get(m["user"])
        if result is None:
            continue  # failed lookup: coverage.failed, not a fake row
        status = result["status"]
        if status == STATUS_GROUP and level != "department":
            gid = result["group"]
            name = leader_name(gid, conf)
            dept = result["dept_code"]
            row = row_for(gid, name, dept,
                          leader=None if gid.startswith(GROUP_UNIT_PREFIX)
                          else gid)
        else:
            dept = result["dept_code"]
            if not dept:
                # no professor group and no department anywhere: the
                # Unaffiliated row. A user the directory does not know
                # (status unresolved) folds in here too — one honest
                # outside-every-row bucket; coverage.unresolved and the
                # member's own status still say which is which.
                gid = GROUP_UNAFFILIATED
                name = "Unaffiliated"
            else:
                gid = GROUP_DEPT_PREFIX + dept
                name = dept_name(dept, conf) + (
                    "" if level == "department"
                    else ", no professor group")
            row = row_for(gid, name, dept)
        short, full = school_pair(dept, conf)
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
            "group": result["group"],
            "membership": result["membership"],
            "dept_code": result["dept_code"],
            "school_code": result["school_code"],
            "own_dept": result["own_dept"],
            "extra_groups": result["extra_groups"],
            "status": status,
        })

    gids_of = {user: group_id_for(result, level)
               for user, result in mapping.items()}
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
